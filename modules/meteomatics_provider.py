"""
VECTOR CHECK AERIAL GROUP INC. — Meteomatics API Provider

Translates Meteomatics' weather API into the same response shape that the
rest of ARMS expects from Open-Meteo. The goal is provider transparency:
downstream code (the dashboard, the sounding, the impact matrix, etc.) sees
a uniform `{"hourly": {...}, "elevation": ..., "_run_info": {...}}` dict
regardless of which provider actually served the data.

PROVIDER NOTES:
  Meteomatics auth:        HTTP Basic (user + password from secrets.toml)
  URL structure:           https://api.meteomatics.com/{validdate}/{params}/{lat,lon}/json?model=X
  Parameter naming:        explicit units, e.g. t_2m:C, wind_speed_10m:kn
  Response shape:          per-parameter array of (lat, lon) → list of {date, value}
                           — flattened here into Open-Meteo's parallel-arrays format
  Time range syntax:       2026-05-29T00:00:00Z--2026-06-01T00:00:00Z:PT1H

UNIT DISCIPLINE:
  All wind queried in `:kn` (knots), all temps in `:C` (Celsius), all
  pressures in `:hPa`. No conversion needed downstream.

CREDENTIALS:
  Required in secrets.toml:
    [meteomatics]
    user = "your_username"
    password = "your_password"
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import streamlit as st

from modules.http_client import fetch_json, HttpFetchError

logger = logging.getLogger("arms.meteomatics")

METEOMATICS_BASE = "https://api.meteomatics.com"
DEFAULT_TIMEOUT_S = 20.0     # Meteomatics can be slower than Open-Meteo on complex queries

# =============================================================================
# MODEL CATALOG
# =============================================================================
# Each entry: Meteomatics `model=` value
METEOMATICS_MODELS = {
    "mix":            "mix",            # Meteomatics proprietary blend (best 0-24h)
    "ecmwf-ifs":      "ecmwf-ifs",      # ECMWF IFS deterministic
    "ecmwf-aifs":     "ecmwf-aifs",     # ECMWF AI-driven model
    "ecmwf-ens":      "ecmwf-ens",      # ECMWF ensemble (mean)
    "ncep-gfs":       "ncep-gfs",       # NCEP GFS global
    "dwd-icon":       "dwd-icon",       # DWD ICON global
    "dwd-icon-eu":    "dwd-icon-eu",    # DWD ICON-EU regional
    "dwd-icon-d2":    "dwd-icon-d2",    # DWD ICON-D2 (Germany 2km)
    "ncep-hrrr":      "ncep-hrrr",      # NCEP HRRR (CONUS 3km)
    "ukmo-euro4":     "ukmo-euro4",     # UKMO Euro4 (Europe regional)
    "mf-arome":       "mf-arome",       # Meteo-France AROME (France 1.3km)
}


# =============================================================================
# PARAMETER MAPPING
# =============================================================================
# Open-Meteo name → Meteomatics parameter expression

# Surface variables — these mirror the fetch_mission_data hourly_vars set in
# data_ingest.py so downstream code can treat the response identically.
_SURFACE_PARAMS = {
    "temperature_2m":             "t_2m:C",
    "relative_humidity_2m":       "relative_humidity_2m:p",
    "wind_speed_10m":             "wind_speed_10m:kn",
    "wind_direction_10m":         "wind_dir_10m:d",
    "wind_gusts_10m":             "wind_gusts_10m_1h:kn",
    "wind_speed_80m":             "wind_speed_80m:kn",
    "wind_speed_120m":            "wind_speed_120m:kn",
    "wind_speed_180m":            "wind_speed_180m:kn",
    "wind_direction_80m":         "wind_dir_80m:d",
    "wind_direction_120m":        "wind_dir_120m:d",
    "wind_direction_180m":        "wind_dir_180m:d",
    "weather_code":               "weather_symbol_1h:idx",
    "visibility":                 "visibility:m",
    "freezing_level_height":      "freezing_level:m",
    "precipitation_probability":  "prob_precip_1h:p",
    "precipitation":              "precip_1h:mm",
    "cape":                       "cape:Jkg",
    "boundary_layer_height":      "boundary_layer_height:m",
    "surface_pressure":           "sfc_pressure:hPa",
    "snow_depth":                 "snow_depth:m",
}

# Pressure levels (matches ALL_P_LEVELS from modules/physics.py)
_PRESSURE_LEVELS = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500,
                    400, 300, 250, 200, 150]


def _build_param_list() -> list[tuple[str, str]]:
    """Builds the full list of (open_meteo_name, meteomatics_param) pairs."""
    pairs = list(_SURFACE_PARAMS.items())
    for p in _PRESSURE_LEVELS:
        pairs.append((f"temperature_{p}hPa",       f"t_{p}hPa:C"))
        pairs.append((f"relative_humidity_{p}hPa", f"relative_humidity_{p}hPa:p"))
        pairs.append((f"geopotential_height_{p}hPa", f"gh_{p}hPa:m"))
        pairs.append((f"wind_speed_{p}hPa",        f"wind_speed_{p}hPa:kn"))
        pairs.append((f"wind_direction_{p}hPa",    f"wind_dir_{p}hPa:d"))
    return pairs


# =============================================================================
# WEATHER SYMBOL TRANSLATION
# =============================================================================
# Meteomatics weather_symbol_1h:idx returns a 1-99 code that differs from
# WMO. ARMS' hazard logic in modules/hazard_logic.py is written against WMO
# codes (0/45/61/95/etc.), so we translate.
#
# Meteomatics symbol catalog (from their public docs, simplified):
#   1  = clear (day),       101 = clear (night)
#   2  = mostly clear,      102 = mostly clear (night)
#   3  = partly cloudy,     103 = partly cloudy (night)
#   4  = mostly cloudy,     104 = mostly cloudy (night)
#   5  = overcast
#   6  = fog
#   7  = light rain
#   8  = rain
#   9  = heavy rain
#  10  = freezing rain
#  11  = mixed precip / sleet
#  12  = light snow
#  13  = snow
#  14  = heavy snow
#  15  = rain shower
#  16  = snow shower
#  17  = mixed shower
#  18  = thunderstorm
#  19  = light hail
#  20  = heavy hail / severe thunderstorm
# Some implementations also include drizzle (~21), freezing drizzle (~22).
_METEOMATICS_TO_WMO = {
    1:   0,    # Clear
    2:   1,    # Mostly clear → "few clouds"
    3:   2,    # Partly cloudy
    4:   3,    # Mostly cloudy / overcast
    5:   3,    # Overcast
    6:   45,   # Fog
    7:   61,   # Light rain
    8:   63,   # Rain
    9:   65,   # Heavy rain
    10:  66,   # Light freezing rain → WMO 66
    11:  68,   # Mixed precip / sleet
    12:  71,   # Light snow
    13:  73,   # Snow
    14:  75,   # Heavy snow
    15:  80,   # Rain shower
    16:  85,   # Snow shower
    17:  82,   # Mixed shower (treat as heavy rain shower)
    18:  95,   # Thunderstorm
    19:  96,   # Thunderstorm with hail
    20:  99,   # Severe thunderstorm
    21:  51,   # Drizzle
    22:  56,   # Freezing drizzle
    # Night-equivalent codes — add 100 to day codes, map to same WMO
    101: 0, 102: 1, 103: 2, 104: 3, 105: 3,
}


def _meteomatics_symbol_to_wmo(idx: Optional[float]) -> int:
    """Translates a Meteomatics weather_symbol_1h:idx value into a WMO code
    that ARMS' downstream hazard logic understands. Unknown / null values
    return 0 (no significant weather)."""
    if idx is None:
        return 0
    try:
        return _METEOMATICS_TO_WMO.get(int(idx), 0)
    except (TypeError, ValueError):
        return 0


# =============================================================================
# CREDENTIALS
# =============================================================================

def _get_credentials() -> Optional[tuple]:
    """Returns (user, password) tuple from secrets.toml, or None if not set."""
    try:
        user = st.secrets["meteomatics"]["user"]
        pwd = st.secrets["meteomatics"]["password"]
        if user and pwd:
            return (user, pwd)
    except (KeyError, FileNotFoundError, AttributeError):
        return None
    return None


def has_credentials() -> bool:
    """Quick check for sidebar coverage gating."""
    return _get_credentials() is not None


# =============================================================================
# FETCH
# =============================================================================

def fetch_meteomatics_forecast(
    lat: float,
    lon: float,
    model: str = "mix",
    hours_ahead: int = 72,
) -> dict:
    """Fetches a forecast from Meteomatics and returns it in Open-Meteo response shape.

    Args:
        lat, lon:     site coordinates
        model:        Meteomatics model identifier (key from METEOMATICS_MODELS)
        hours_ahead:  forecast horizon in hours

    Returns:
        dict with keys:
            "hourly":     {"time": [...], "temperature_2m": [...], ...}
            "hourly_units": {"wind_speed_10m": "kn", "temperature_2m": "°C", ...}
            "elevation":  station elevation in metres
            "_run_info":  {run_cycle_z, run_date, age_hours} or {}
            "_provider":  "meteomatics"
            "_model":     the model id used
        On failure returns {"error": True, "message": str, "_provider": "meteomatics"}.
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": True,
            "message": "Meteomatics credentials not configured in secrets.toml",
            "_provider": "meteomatics",
        }

    if model not in METEOMATICS_MODELS:
        return {
            "error": True,
            "message": f"Unknown Meteomatics model: {model}",
            "_provider": "meteomatics",
        }

    # Build validdate range — hourly steps. Round start to top of current hour
    # so cache keys are stable within a clock hour.
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = now + timedelta(hours=hours_ahead)
    validdate = f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')}--{end.strftime('%Y-%m-%dT%H:%M:%SZ')}:PT1H"

    # Build parameter list
    pairs = _build_param_list()
    om_names = [p[0] for p in pairs]
    mm_params = [p[1] for p in pairs]

    # Construct URL. Meteomatics encodes parameters comma-separated in path.
    param_str = ",".join(mm_params)
    url = f"{METEOMATICS_BASE}/{validdate}/{param_str}/{lat:.4f},{lon:.4f}/json?model={METEOMATICS_MODELS[model]}"

    try:
        payload = fetch_json(
            url,
            timeout=DEFAULT_TIMEOUT_S,
            retries=2,
            basic_auth=creds,
        )
    except HttpFetchError as e:
        msg = e.message
        if e.status == 401:
            msg = "Meteomatics authentication failed — credentials may be invalid or expired."
        elif e.status == 402:
            msg = "Meteomatics quota exceeded — daily parameter-locations budget hit."
        elif e.status == 429:
            msg = "Meteomatics rate limited — too many requests per minute."
        elif e.status and 500 <= e.status < 600:
            msg = f"Meteomatics server error (HTTP {e.status}) — service may be degraded."
        logger.warning("Meteomatics fetch failed: %s", msg)
        return {
            "error": True,
            "message": msg,
            "status": e.status,
            "_provider": "meteomatics",
        }

    # Translate response shape
    try:
        translated = _translate_to_open_meteo_shape(payload, om_names)
    except Exception as e:
        logger.exception("Meteomatics translation failed")
        return {
            "error": True,
            "message": f"Meteomatics response translation failed: {e}",
            "_provider": "meteomatics",
        }

    # Augment with metadata
    translated["_provider"] = "meteomatics"
    translated["_model"] = model
    translated["_run_info"] = _extract_run_info(payload)
    return translated


def _translate_to_open_meteo_shape(payload: dict, om_names: list[str]) -> dict:
    """Converts Meteomatics' per-parameter-array response into Open-Meteo's
    flat parallel-arrays shape.

    Meteomatics:
        {
          "data": [
            {"parameter": "t_2m:C", "coordinates": [
                {"lat": ..., "lon": ..., "dates": [{"date": "...", "value": ...}, ...]}
            ]},
            ...
          ]
        }

    Open-Meteo:
        {
          "hourly": {
            "time": ["2026-05-29T00:00", ...],
            "temperature_2m": [14.3, 13.8, ...],
            ...
          },
          "hourly_units": {"temperature_2m": "°C", "wind_speed_10m": "kn", ...}
        }
    """
    data = payload.get("data") or []
    if not data:
        raise ValueError("Empty data array from Meteomatics")

    # Build a lookup from Meteomatics parameter expression → its date/value list
    # Order matters: we expect data[i].parameter to match the requested list,
    # but Meteomatics doesn't guarantee order, so we use a dict.
    mm_pairs = dict(_build_param_list())   # om_name → mm_param
    om_to_mm = {om: mm for om, mm in mm_pairs.items() if om in om_names}

    # Build reverse map for the actual response
    response_by_param: dict[str, list] = {}
    times_source: list[str] = []
    for block in data:
        mm_param = block.get("parameter")
        coords = block.get("coordinates") or []
        if not mm_param or not coords:
            continue
        # We requested a single coordinate so coords[0] is our point
        dates_arr = coords[0].get("dates") or []
        if not dates_arr:
            continue
        response_by_param[mm_param] = dates_arr
        if not times_source:
            times_source = [d["date"] for d in dates_arr]

    if not times_source:
        raise ValueError("No time series data in Meteomatics response")

    # Open-Meteo emits times like "2026-05-29T00:00" (no seconds, no Z) when
    # &timezone=UTC. Normalize Meteomatics' "2026-05-29T00:00:00Z" to the
    # same format so downstream string-based time matching keeps working.
    def _normalize_time(t: str) -> str:
        # Strip trailing Z and seconds — keep "YYYY-MM-DDTHH:MM"
        if t.endswith("Z"):
            t = t[:-1]
        if len(t) >= 16:
            return t[:16]
        return t

    times_normalized = [_normalize_time(t) for t in times_source]

    # Build the hourly dict, filling in lists in the order of the requested om_names
    hourly: dict = {"time": times_normalized}
    hourly_units: dict = {}

    # Unit map for the synthesized hourly_units dict (matches Open-Meteo's
    # display labels so the wind-unit detector at app.py treats things uniformly)
    UNIT_LABELS = {
        ":C": "°C", ":K": "K", ":F": "°F",
        ":p": "%", ":hPa": "hPa", ":Pa": "Pa",
        ":m": "m", ":km": "km", ":ft": "ft",
        ":mm": "mm", ":kn": "kn", ":ms": "m/s", ":kmh": "km/h", ":mph": "mph",
        ":d": "°", ":idx": "wmo", ":Jkg": "J/kg",
    }

    for om_name in om_names:
        mm_param = om_to_mm.get(om_name)
        if mm_param is None:
            continue
        dates_arr = response_by_param.get(mm_param) or []
        if not dates_arr:
            # Meteomatics may legitimately skip a parameter when the model
            # doesn't carry it — fill with Nones aligned to the time grid
            hourly[om_name] = [None] * len(times_normalized)
        else:
            # Map dates to indices in our master time list. They should be
            # parallel by construction but be defensive.
            values_by_time = {_normalize_time(d["date"]): d.get("value") for d in dates_arr}
            hourly[om_name] = [values_by_time.get(t) for t in times_normalized]

            # Translate weather symbol to WMO
            if om_name == "weather_code":
                hourly[om_name] = [_meteomatics_symbol_to_wmo(v) for v in hourly[om_name]]

        # Unit label — find the suffix after the last ':'
        if ":" in mm_param:
            suffix = ":" + mm_param.split(":")[-1]
            hourly_units[om_name] = UNIT_LABELS.get(suffix, suffix.lstrip(":"))
        else:
            hourly_units[om_name] = ""

    # Elevation is not returned by Meteomatics in this endpoint — defer to
    # caller's geographic lookup (the dashboard uses Open-Meteo's elevation
    # for the active site anyway via its own static computation).
    return {
        "hourly": hourly,
        "hourly_units": hourly_units,
        "elevation": None,    # caller resolves separately
    }


def _extract_run_info(payload: dict) -> dict:
    """Extracts run cycle metadata from a Meteomatics response.

    Meteomatics returns a `dateGenerated` ISO timestamp at the top level.
    That's the time the response was generated, not the model initialization
    time. The model init time isn't exposed in the standard timeseries
    endpoint — we'd need a separate metadata call.

    For now we use `dateGenerated` as a proxy. This is close enough for the
    "Xh ago" display but doesn't precisely tell you the cycle (e.g. 00Z/12Z).
    For most operational use that's acceptable since Meteomatics ingests
    continuously and the response always reflects the freshest data.

    A future enhancement could call the /init_date endpoint per model.
    """
    ts = payload.get("dateGenerated")
    if not ts:
        return {}
    try:
        # Format: "2026-05-29T12:34:56Z"
        if ts.endswith("Z"):
            ts = ts[:-1]
        run_dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        age_hours = int((now - run_dt).total_seconds() / 3600)
        return {
            "run_cycle_z": f"{run_dt.hour:02d}Z",
            "run_date": run_dt.strftime("%Y-%m-%d"),
            "run_datetime_utc": run_dt,
            "age_hours": age_hours,
        }
    except (ValueError, TypeError) as e:
        logger.debug("Meteomatics run-info parse failed: %s", e)
        return {}
