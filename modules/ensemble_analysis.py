"""
VECTOR CHECK AERIAL GROUP INC. — Model Analysis Engine

Fetches deterministic forecasts from 4 independent NWP models via Open-Meteo,
computes per-variable ensemble statistics (mean, spread, min, max) across
12-hour blocks, and generates a structured atmospheric intelligence briefing
using deterministic template logic.

MODELS QUERIED:
    1. GEM HRDPS  — Environment Canada, 2.5 km, 48h
    2. GFS        — NOAA, 25 km, 16 days
    3. ECMWF IFS  — European Centre, 9 km, 10 days
    4. ICON       — DWD Germany, 11 km, 7.5 days

COST: $0 — all data sources are free and keyless.
"""

import urllib.request
import json
import math
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("arms.ensemble")

# =============================================================================
# CONFIGURATION
# =============================================================================

from modules.open_meteo_endpoints import build_url as _om_url


def _mm_credentials_present() -> bool:
    """Lazy check for Meteomatics credentials. Returns False if module
    can't be imported or credentials missing."""
    try:
        from modules.meteomatics_provider import has_credentials
        return has_credentials()
    except ImportError:
        return False


# =============================================================================
# MODEL ROUTING TABLE
# =============================================================================
# Each model has a SOURCE PREFERENCE: Meteomatics when it carries the model
# AND we have credentials, otherwise Open-Meteo.
#
# Each entry is (source, target):
#   source = "meteomatics" → target is the Meteomatics model id (e.g. "mix")
#   source = "open-meteo"  → target is the full Open-Meteo URL
#
# Verified against the live Meteomatics API on 2026-05-29 (vectorcheck account):
#   Available: mix, ecmwf-ifs, ecmwf-aifs, ncep-gfs, ncep-hrrr
#   NOT available on this subscription: dwd-icon, dwd-icon-eu
#   Never available (Meteomatics doesn't carry): HRDPS, NAM, ACCESS-G

def _build_model_routes() -> dict:
    """Rebuilds the routing table. Called once at import; if Meteomatics
    credentials are added later they take effect on next dashboard restart."""
    mm = _mm_credentials_present()
    routes = {}

    # Meteomatics MIX — proprietary blend, only available from Meteomatics
    if mm:
        routes["MIX"] = ("meteomatics", "mix")

    # ECMWF AIFS — AI-driven model, only available from Meteomatics
    if mm:
        routes["AIFS"] = ("meteomatics", "ecmwf-aifs")

    # ECMWF IFS — prefer Meteomatics (bias-corrected, higher cadence),
    # fall back to Open-Meteo's raw ECMWF
    routes["ECMWF"] = ("meteomatics", "ecmwf-ifs") if mm else ("open-meteo", _om_url("ecmwf"))

    # GFS — route to Open-Meteo unconditionally, even when Meteomatics
    # credentials exist. Rationale: GFS is raw NOAA data identical on both
    # providers (no MIX-style bias correction), so Meteomatics adds zero
    # quality. Open-Meteo additionally exposes derived VISIBILITY for GFS,
    # while Meteomatics blocklists visibility:m for ncep-gfs (it 404s). Routing
    # here gains a visibility row in the comparison matrix and saves quota.
    routes["GFS"] = ("open-meteo", _om_url("gfs"))

    # HRRR — route to Open-Meteo unconditionally. Same reasoning as GFS: HRRR
    # is raw NOAA data, no Meteomatics quality benefit, and Open-Meteo exposes
    # derived VISIBILITY while Meteomatics blocklists visibility:m for ncep-hrrr.
    #
    # IMPORTANT: Open-Meteo's HRRR (ncep_hrrr_conus) is CONUS-only and only
    # carries ~2 days of forecast. When a requested hour has no HRRR data,
    # Open-Meteo's seamless model nesting SILENTLY substitutes GFS (a 25km
    # global model) for that hour. Mixing 3km HRRR and 25km GFS in one series
    # produces inconsistent winds. We add &cell_selection=nearest to pin the
    # grid cell; the model_performance wind sanitizer also catches any spikes.
    routes["HRRR"] = ("open-meteo",
                       _om_url("gfs?models=ncep_hrrr_conus&cell_selection=nearest"))

    # ICON Global — Open-Meteo only (vectorcheck subscription doesn't include
    # DWD ICON via Meteomatics)
    routes["ICON"] = ("open-meteo", _om_url("dwd-icon"))

    # HRDPS — Open-Meteo only (Meteomatics doesn't carry Canadian regional)
    routes["HRDPS"] = ("open-meteo", _om_url("gem"))

    # NAM CONUS — Open-Meteo only
    routes["NAM"] = ("open-meteo", _om_url("gfs?models=ncep_nam_conus"))

    # MOS (Meteomatics Model Output Statistics) — DISABLED. Diagnostic on
    # 2026-05-29 confirmed find_station?source=mm-mos returns zero stations
    # near our VCAG sites (CYBN, CYOD, CYTA, CYBG, CYYZ). MOS coverage is
    # concentrated in Europe with limited North American footprint. Re-enable
    # if Meteomatics adds North American MOS coverage or if VCAG deploys to
    # a European site.
    #
    # if mm:
    #     routes["MOS"] = ("meteomatics-mos", "mm-mos")

    return routes


MODEL_ROUTES = _build_model_routes()

# Approximate native horizontal grid resolution (km) for each model, used to
# order the comparison matrix from highest-resolution (finest) to lowest.
# Finer-resolution mesoscale models resolve terrain and convection better and
# generally lead near-term; operators expect them at the top of the stack.
# MIX is a multi-model blend with no single native grid; we place it just
# below the true mesoscale models since its effective skill is mesoscale-class.
MODEL_RESOLUTION_KM = {
    "HRRR":   3.0,    # NOAA HRRR CONUS 3 km
    "NAM":    3.0,    # NAM CONUS nest 3 km
    "HRDPS":  2.5,    # ECCC HRDPS 2.5 km (finest)
    "HARMONIE": 2.0,
    "AROME":  1.3,
    "MIX":    4.0,    # Meteomatics blend — mesoscale-class effective skill
    "ICON-EU": 7.0,   # DWD ICON-EU regional
    "ICON":   11.0,   # DWD ICON global ~11 km
    "GFS":    13.0,   # NOAA GFS ~13 km
    "AIFS":   28.0,   # ECMWF AIFS ~0.25deg
    "ECMWF":  9.0,    # ECMWF IFS HRES ~9 km
    "GEM":    15.0,
    "ACCESS-G": 12.0,
}
# Default for any model not in the table (treat as coarse so it sorts last).
_DEFAULT_RES_KM = 25.0


def _resolution_rank(model_name: str) -> float:
    """Returns the sort key (km) for a model name. Lower = finer = sorts first.
    Tolerates name variants (e.g. 'HRDPS (regional)') by prefix match."""
    if model_name in MODEL_RESOLUTION_KM:
        return MODEL_RESOLUTION_KM[model_name]
    # Prefix / contains match for decorated names
    up = model_name.upper()
    for key, val in MODEL_RESOLUTION_KM.items():
        if key.upper() in up:
            return val
    return _DEFAULT_RES_KM


# Backward-compat for any external code that still imports MODEL_ENDPOINTS.
# Resolves to the URL portion of routes that go through Open-Meteo (for the
# meteomatics:// scheme tag) so legacy callers can still construct fetches.
def _route_to_endpoint(source: str, target: str) -> str:
    """Encodes a (source, target) route into a single URL/marker string for
    backward-compat with code that imported MODEL_ENDPOINTS. The model
    performance scorecard reads this dict and dispatches based on scheme:
        http(s)://...        → Open-Meteo fetcher
        meteomatics://...    → Meteomatics forecast fetcher
        meteomatics-mos://...→ Meteomatics MOS fetcher (station-keyed)
    """
    if source == "open-meteo":
        return target
    if source == "meteomatics-mos":
        return f"meteomatics-mos://{target}"
    return f"meteomatics://{target}"


MODEL_ENDPOINTS = {
    name: _route_to_endpoint(source, target)
    for name, (source, target) in MODEL_ROUTES.items()
}

# Regional high-resolution model swap-ins when outside primary coverage.
# These replace HRDPS in the ensemble when the query point is outside Canada.
REGIONAL_MODELS = {
    # Europe — DWD ICON-EU (~7 km)
    "icon_eu":   _om_url("dwd-icon"),
    # Pacific / Australia — BOM ACCESS-G at 12 km
    "access_g":  _om_url("bom"),
    # Generic global best-match
    "best":      _om_url("forecast"),
}


def _is_hrdps_coverage(lat: float, lon: float) -> bool:
    """HRDPS covers Canada + a thin US strip. Approx 40-75N, -145 to -50W."""
    return (40.0 <= lat <= 75.0) and (-145.0 <= lon <= -50.0)


def _is_europe_coverage(lat: float, lon: float) -> bool:
    """ICON-EU covers roughly 29-71N, -23 to 45E."""
    return (29.0 <= lat <= 71.0) and (-23.0 <= lon <= 45.0)


def _is_oceania_coverage(lat: float, lon: float) -> bool:
    """BOM ACCESS-G is strongest over Australia/Pacific."""
    return (-50.0 <= lat <= 10.0) and (100.0 <= lon <= 180.0)


def _is_conus_coverage(lat: float, lon: float) -> bool:
    """HRRR / NAM 3km CONUS nest covers ~21-50N, -134 to -60W (incl. Alaska partial)."""
    return (21.0 <= lat <= 50.0) and (-134.0 <= lon <= -60.0)


def _select_regional_model(lat: float, lon: float) -> tuple:
    """Picks the best regional high-res model for the location.

    Returns (model_name, endpoint_url). If no regional model applies,
    returns the Open-Meteo best-match endpoint which auto-selects.
    """
    if _is_hrdps_coverage(lat, lon):
        return ("HRDPS", MODEL_ENDPOINTS["HRDPS"])
    if _is_europe_coverage(lat, lon):
        return ("ICON-EU", REGIONAL_MODELS["icon_eu"])
    if _is_oceania_coverage(lat, lon):
        return ("ACCESS-G", REGIONAL_MODELS["access_g"])
    # Default: use Open-Meteo's Best Match endpoint
    return ("Best Match", REGIONAL_MODELS["best"])

_HOURLY_VARS = (
    "temperature_2m,relative_humidity_2m,wind_speed_10m,"
    "wind_direction_10m,wind_gusts_10m,surface_pressure,"
    "precipitation_probability,weather_code,visibility"
)

# Operational thresholds for flagging model divergence
WIND_SPREAD_WARN_KT = 6
WIND_SPREAD_ALERT_KT = 10
TEMP_SPREAD_WARN_C = 4
GUST_SPREAD_WARN_KT = 8
KMH_TO_KT = 0.539957
REQUEST_TIMEOUT_S = 12
BLOCK_HOURS = 12   # analysis blocks

USER_AGENT = "VectorCheck-ARMS/2.1"


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class ModelForecast:
    """Parsed hourly forecast from one NWP model."""
    name: str = ""
    times: list = field(default_factory=list)       # ISO strings
    wind_kt: list = field(default_factory=list)
    wind_dir: list = field(default_factory=list)
    gust_kt: list = field(default_factory=list)
    temp_c: list = field(default_factory=list)
    rh: list = field(default_factory=list)
    pressure_hpa: list = field(default_factory=list)
    precip_prob: list = field(default_factory=list)
    wx_code: list = field(default_factory=list)
    visibility_sm: list = field(default_factory=list)
    valid: bool = False


@dataclass
class BlockStats:
    """Ensemble statistics for one 12-hour block."""
    block_label: str = ""       # "18 Apr 00-12Z"
    start_hour: int = 0         # offset from T+0

    # Wind speed (kt)
    wind_mean: float = 0.0
    wind_min: float = 0.0
    wind_max: float = 0.0
    wind_spread: float = 0.0   # max - min across models

    # Wind direction — circular mean and max arc spread
    wind_dir_mean: float = 0.0
    wind_dir_spread: float = 0.0

    # Gusts (kt)
    gust_mean: float = 0.0
    gust_max: float = 0.0
    gust_spread: float = 0.0

    # Temperature (°C)
    temp_mean: float = 0.0
    temp_min: float = 0.0
    temp_max: float = 0.0
    temp_spread: float = 0.0

    # Pressure (hPa)
    pressure_mean: float = 0.0
    pressure_spread: float = 0.0

    # Precipitation probability (%) — max across models
    precip_prob_max: float = 0.0

    # Model count for this block
    model_count: int = 0

    # Confidence tag
    confidence: str = "HIGH"    # HIGH / MODERATE / LOW


@dataclass
class RiskWindow:
    """A time period where model divergence exceeds operational thresholds."""
    start_label: str = ""
    end_label: str = ""
    variable: str = ""          # "Wind Speed", "Temperature", etc.
    spread: float = 0.0
    detail: str = ""
    severity: str = "WARN"      # WARN / ALERT


@dataclass
class EnsembleBriefing:
    """Complete model analysis output."""
    generated_utc: datetime = None
    model_count: int = 0
    models_used: list = field(default_factory=list)
    models_failed: list = field(default_factory=list)

    blocks: list = field(default_factory=list)       # list[BlockStats]
    risk_windows: list = field(default_factory=list)  # list[RiskWindow]
    anomaly_flags: list = field(default_factory=list) # list[str]

    # Pre-rendered text sections
    consensus_summary: str = ""
    confidence_summary: str = ""
    wind_summary: str = ""
    precip_summary: str = ""

    overall_confidence: str = "HIGH"  # HIGH / MODERATE / LOW


# =============================================================================
# FETCH
# =============================================================================

def _fetch_model(name: str, url: str, lat: float, lon: float) -> ModelForecast:
    """Fetches one model's hourly forecast. Returns ModelForecast (valid=False on failure)."""
    mf = ModelForecast(name=name)

    # Explicit wind_speed_unit=kn — see notes in data_ingest.py. Eliminates
    # the silent-conversion failure mode for wind speeds.
    sep = "&" if "?" in url else "?"
    full_url = (
        f"{url}{sep}latitude={lat}&longitude={lon}"
        f"&hourly={_HOURLY_VARS}&timezone=UTC&forecast_days=4"
        f"&wind_speed_unit=kn"
    )

    try:
        from modules.http_client import fetch_json as _fetch_json, HttpFetchError as _HttpFetchError
        data = _fetch_json(full_url, timeout=REQUEST_TIMEOUT_S, retries=2)
    except _HttpFetchError as e:
        logger.warning("Ensemble fetch failed for %s: %s", name, e)
        return mf

    h = data.get("hourly")
    if not h or "time" not in h:
        return mf

    # Robust wind-unit detection from the response (in case Open-Meteo ignores
    # the unit hint for a particular endpoint, or a future provider responds
    # in its native unit despite the request).
    _wu = data.get("hourly_units", {}).get("wind_speed_10m", "kn").lower()
    if "km/h" in _wu:   _wind_scale = 0.539957
    elif "m/s" in _wu:  _wind_scale = 1.943844
    elif "mph" in _wu:  _wind_scale = 0.868976
    else:               _wind_scale = 1.0

    n = len(h["time"])
    mf.times = h["time"][:72]

    def _safe_list(key, scale=1.0):
        raw = h.get(key, [])
        out = []
        for v in raw[:72]:
            if v is not None:
                try:
                    out.append(float(v) * scale)
                except (TypeError, ValueError):
                    out.append(None)
            else:
                out.append(None)
        return out

    mf.wind_kt = _safe_list("wind_speed_10m", _wind_scale)
    mf.wind_dir = _safe_list("wind_direction_10m")
    mf.gust_kt = _safe_list("wind_gusts_10m", _wind_scale)
    mf.temp_c = _safe_list("temperature_2m")
    mf.rh = _safe_list("relative_humidity_2m")
    mf.pressure_hpa = _safe_list("surface_pressure")
    mf.precip_prob = _safe_list("precipitation_probability")
    mf.wx_code = _safe_list("weather_code")
    # Open-Meteo visibility is in meters; convert to statute miles for METAR
    # comparability and aviation use.
    mf.visibility_sm = _safe_list("visibility", 1.0 / 1609.344)
    mf.valid = len(mf.wind_kt) >= 24

    return mf


def _fetch_model_meteomatics(lat: float, lon: float, model: str = "mix",
                              display_name: str = "MM-MIX") -> "ModelForecast":
    """Fetches one Meteomatics model and returns a ModelForecast.

    Used to add Meteomatics MIX (and optionally other Meteomatics models) as
    additional ensemble members alongside the raw Open-Meteo models. The
    quota cost is roughly 1 batch x 10 quota units per call.

    Returns an invalid ModelForecast on any failure (silently — caller sees
    valid=False and skips it from the ensemble).
    """
    mf = ModelForecast(name=display_name)
    try:
        from modules.meteomatics_provider import (
            fetch_meteomatics_forecast,
            has_credentials,
        )
    except ImportError:
        return mf
    if not has_credentials():
        return mf

    # 96h forecast horizon to match Open-Meteo's forecast_days=4
    result = fetch_meteomatics_forecast(lat, lon, model=model, hours_ahead=96)
    if result.get("error"):
        logger.warning("Meteomatics ensemble fetch failed for %s: %s",
                       model, result.get("message"))
        return mf

    h = result.get("hourly")
    if not h or "time" not in h:
        return mf

    def _truncate(key: str) -> list:
        """Take the first 72 hours and pad with None for any missing values."""
        raw = h.get(key) or []
        out = []
        for v in raw[:72]:
            if v is None:
                out.append(None)
            else:
                try:
                    out.append(float(v))
                except (TypeError, ValueError):
                    out.append(None)
        return out

    mf.times = h["time"][:72]
    # Meteomatics is requested in knots already (:kn), no scaling needed
    mf.wind_kt = _truncate("wind_speed_10m")
    mf.wind_dir = _truncate("wind_direction_10m")
    mf.gust_kt = _truncate("wind_gusts_10m")
    mf.temp_c = _truncate("temperature_2m")
    mf.rh = _truncate("relative_humidity_2m")
    mf.pressure_hpa = _truncate("surface_pressure")
    mf.precip_prob = _truncate("precipitation_probability")
    mf.wx_code = _truncate("weather_code")
    # Meteomatics visibility:m → statute miles. May be absent for some models.
    _vis_m = _truncate("visibility")
    mf.visibility_sm = [(v / 1609.344 if v is not None else None) for v in _vis_m]
    mf.valid = len(mf.wind_kt) >= 24
    return mf


def fetch_all_models(lat: float, lon: float) -> list:
    """Fetches the active ensemble for this location. Returns list of valid
    ModelForecast objects.

    Each model uses its best available source — Meteomatics where the
    subscription includes it (better bias correction, higher cadence),
    Open-Meteo otherwise. Source routing is declared in MODEL_ROUTES.

    Ensemble composition (typical CONUS site with Meteomatics credentials):
      - 1 regional high-res (HRDPS or regional swap)
      - 3 global models (ECMWF, GFS, ICON)
      - 2 CONUS mesoscale (HRRR, NAM) when in CONUS
      - MIX (Meteomatics-only proprietary blend)
      - AIFS (Meteomatics-only AI-driven forecast)

    Sites in Canada outside CONUS get ~5 models; CONUS sites with Meteomatics
    get ~8.
    """
    regional_name, regional_url = _select_regional_model(lat, lon)

    # Build the active model list. Use MODEL_ROUTES wherever possible to get
    # the best source automatically. Regional model is handled separately
    # since it depends on the query location.
    active_routes: dict = {}

    # Regional — use the same routing if it matches a known model name,
    # else treat as a raw Open-Meteo URL
    if regional_name in MODEL_ROUTES:
        active_routes[regional_name] = MODEL_ROUTES[regional_name]
    else:
        # Regional swap (icon_eu, access_g, best) — Open-Meteo only
        active_routes[regional_name] = ("open-meteo", regional_url)

    # Global models — always include
    for name in ("ECMWF", "GFS", "ICON"):
        if name in MODEL_ROUTES and name != regional_name:
            active_routes[name] = MODEL_ROUTES[name]

    # CONUS mesoscale models — only when in CONUS coverage
    if _is_conus_coverage(lat, lon):
        for name in ("HRRR", "NAM"):
            if name in MODEL_ROUTES and name != regional_name:
                active_routes[name] = MODEL_ROUTES[name]

    # Meteomatics-only models (MIX, AIFS) — include when credentials exist
    for name in ("MIX", "AIFS"):
        if name in MODEL_ROUTES:
            active_routes[name] = MODEL_ROUTES[name]

    # MOS — only include if we can find a nearby ICAO station to key it on.
    # The lookup is cheap (one bbox call to AviationWeather.gov, cached at
    # session-state level). If no station within ~50km, skip MOS for this
    # site rather than guessing.
    mos_station_id = None
    if "MOS" in MODEL_ROUTES:
        icao = _nearest_icao_for_mos(lat, lon)
        if icao:
            mos_station_id = f"metar_{icao}"
            active_routes["MOS"] = MODEL_ROUTES["MOS"]

    # Parallel fetch dispatcher — each model routes to its source's fetcher
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _dispatch(name: str, source: str, target: str) -> "ModelForecast":
        if source == "meteomatics":
            return _fetch_model_meteomatics(lat, lon, model=target, display_name=name)
        if source == "meteomatics-mos":
            return _fetch_model_mos(mos_station_id, display_name=name)
        return _fetch_model(name, target, lat, lon)

    results = []
    with ThreadPoolExecutor(max_workers=max(2, len(active_routes))) as ex:
        futures = {
            ex.submit(_dispatch, name, source, target): name
            for name, (source, target) in active_routes.items()
        }
        for fut in as_completed(futures):
            try:
                mf = fut.result()
            except Exception as e:
                logger.warning("Ensemble worker crashed for %s: %s",
                               futures[fut], e)
                continue
            if mf.valid:
                results.append(mf)
    return results


# =============================================================================
# MODEL COMPARISON MATRIX — per-hour, per-model side-by-side
# =============================================================================
# Builds an aligned matrix for the side-by-side comparison view: each model is
# a row, each forecast hour is a column. All models are interpolated onto a
# single common time axis (the union of hours, hourly cadence) so a column
# always represents the same valid time across every model, even if providers
# return slightly different start hours or horizons.

def build_model_matrix(models: list, n_hours: int = 24,
                       start_offset: int = 0, tz_str: str = None) -> dict:
    """Aligns all models onto a common hourly axis for side-by-side display.

    Args:
        models:        list of valid ModelForecast objects (from fetch_all_models)
        n_hours:       number of hourly columns to produce
        start_offset:  hours from the first common timestamp to begin (0 = now)
        tz_str:        IANA timezone name (e.g. "America/Toronto"). When given,
                       the matrix also produces local-time column labels
                       alongside the Zulu labels.

    Returns a dict:
        {
          "times":  [ISO strings, len n_hours],     # the common column axis
          "hour_labels": ["00Z","01Z",...],          # short labels for columns
          "models": [
              {
                "name": "ECMWF",
                "wind_kt":   [...], "wind_dir": [...], "gust_kt": [...],
                "temp_c":    [...], "rh": [...], "visibility_sm": [...],
              }, ...
          ],
          "consensus": {                              # per-hour cross-model stats
              "wind_spread": [...], "temp_spread": [...],
              "dir_spread":  [...], "vis_min": [...],
          }
        }
    Values are None where a model has no data for that hour.
    """
    from datetime import datetime, timedelta

    valid = [m for m in models if m.valid and m.times]
    if not valid:
        return {"times": [], "hour_labels": [], "local_labels": [], "local_tz_abbr": "", "models": [], "consensus": {}}

    # Establish the common axis from the LATEST first-timestamp across models
    # (so every model has data at the start) parsed to datetimes.
    def _parse(ts):
        try:
            return datetime.fromisoformat(ts.replace("Z", ""))
        except (ValueError, AttributeError):
            return None

    first_times = []
    for m in valid:
        t0 = _parse(m.times[0])
        if t0 is not None:
            first_times.append(t0)
    if not first_times:
        return {"times": [], "hour_labels": [], "local_labels": [], "local_tz_abbr": "", "models": [], "consensus": {}}

    axis_start = max(first_times) + timedelta(hours=start_offset)
    # Snap to the top of the hour
    axis_start = axis_start.replace(minute=0, second=0, microsecond=0)
    axis = [axis_start + timedelta(hours=i) for i in range(n_hours)]
    axis_iso = [t.strftime("%Y-%m-%dT%H:%M") for t in axis]
    hour_labels = [t.strftime("%HZ") for t in axis]

    # Local-time labels alongside Zulu, when a timezone is supplied. The axis
    # datetimes are UTC (Open-Meteo is requested with timezone=UTC), so we
    # attach UTC then convert to the local zone for display.
    local_labels = []
    local_tz_abbr = ""
    if tz_str:
        try:
            import pytz
            _ltz = pytz.timezone(tz_str)
            for t in axis:
                t_utc = t.replace(tzinfo=timezone.utc)
                t_loc = t_utc.astimezone(_ltz)
                local_labels.append(t_loc.strftime("%H"))
            # Abbreviation from the first axis hour (handles DST correctly)
            local_tz_abbr = axis[0].replace(tzinfo=timezone.utc).astimezone(_ltz).strftime("%Z")
        except Exception:
            local_labels = []
            local_tz_abbr = ""

    # For each model, build a lookup from its timestamps to index, then sample
    # onto the common axis.
    def _index_map(m):
        out = {}
        for i, ts in enumerate(m.times):
            dt = _parse(ts)
            if dt is not None:
                out[dt.replace(minute=0, second=0, microsecond=0)] = i
        return out

    matrix_models = []
    # Order rows by native resolution, finest (smallest km) first, so the
    # mesoscale models lead the stack and the global models follow.
    valid_sorted = sorted(valid, key=lambda m: _resolution_rank(m.name))
    for m in valid_sorted:
        imap = _index_map(m)

        def _sample(arr):
            row = []
            for t in axis:
                idx = imap.get(t)
                if idx is not None and idx < len(arr) and arr[idx] is not None:
                    row.append(arr[idx])
                else:
                    row.append(None)
            return row

        matrix_models.append({
            "name": m.name,
            "wind_kt": _sample(m.wind_kt),
            "wind_dir": _sample(m.wind_dir),
            "gust_kt": _sample(m.gust_kt),
            "temp_c": _sample(m.temp_c),
            "rh": _sample(m.rh),
            "visibility_sm": _sample(m.visibility_sm) if m.visibility_sm else [None] * n_hours,
        })

    # Per-hour consensus stats across models (for highlighting agreement /
    # divergence in the UI without re-deriving in app code).
    def _col_values(key, hour_i):
        vals = [mm[key][hour_i] for mm in matrix_models
                if mm[key][hour_i] is not None]
        return vals

    def _circ_spread(dirs):
        """Max pairwise angular separation (0-180) among a set of bearings."""
        if len(dirs) < 2:
            return 0.0
        max_sep = 0.0
        for i in range(len(dirs)):
            for j in range(i + 1, len(dirs)):
                d = abs(dirs[i] - dirs[j]) % 360
                d = min(d, 360 - d)
                max_sep = max(max_sep, d)
        return max_sep

    wind_spread, temp_spread, dir_spread, vis_min = [], [], [], []
    for hi in range(n_hours):
        wv = _col_values("wind_kt", hi)
        tv = _col_values("temp_c", hi)
        dv = _col_values("wind_dir", hi)
        vv = _col_values("visibility_sm", hi)
        wind_spread.append(round(max(wv) - min(wv), 1) if len(wv) >= 2 else 0.0)
        temp_spread.append(round(max(tv) - min(tv), 1) if len(tv) >= 2 else 0.0)
        dir_spread.append(round(_circ_spread(dv), 0) if len(dv) >= 2 else 0.0)
        vis_min.append(round(min(vv), 1) if vv else None)

    return {
        "times": axis_iso,
        "hour_labels": hour_labels,
        "local_labels": local_labels,
        "local_tz_abbr": local_tz_abbr,
        "models": matrix_models,
        "consensus": {
            "wind_spread": wind_spread,
            "temp_spread": temp_spread,
            "dir_spread": dir_spread,
            "vis_min": vis_min,
        },
    }


def summarize_matrix(matrix: dict) -> list:
    """Produces a short list of notable agreement/divergence callouts for the
    side-by-side view. Returns a list of (severity, text) tuples — kept terse
    so the UI shows only a couple of points, per design.

    severity is one of: "alert" (large divergence), "info" (notable agreement
    or moderate divergence).
    """
    if not matrix.get("models"):
        return []

    cons = matrix["consensus"]
    labels = matrix["hour_labels"]
    notes = []

    # Find the worst wind divergence hour
    ws = cons.get("wind_spread", [])
    if ws:
        max_w = max(ws)
        max_w_i = ws.index(max_w)
        if max_w >= 10:
            notes.append(("alert",
                f"Large wind disagreement at {labels[max_w_i]} "
                f"(\u00b1{max_w:.0f} kt across models) \u2014 low confidence, "
                f"recheck before committing."))
        elif max_w >= 6:
            notes.append(("info",
                f"Moderate wind spread peaks at {labels[max_w_i]} "
                f"(\u00b1{max_w:.0f} kt)."))

    # Direction divergence
    ds = cons.get("dir_spread", [])
    if ds:
        max_d = max(ds)
        max_d_i = ds.index(max_d)
        if max_d >= 90:
            notes.append(("alert",
                f"Wind direction splits badly at {labels[max_d_i]} "
                f"({max_d:.0f}\u00b0 spread) \u2014 models disagree on flow regime."))

    # Temperature divergence
    ts = cons.get("temp_spread", [])
    if ts:
        max_t = max(ts)
        max_t_i = ts.index(max_t)
        if max_t >= 5:
            notes.append(("info",
                f"Temperature spread reaches {max_t:.0f}\u00b0C at {labels[max_t_i]}."))

    # Strong agreement window (first 12h with consistently low spread)
    if ws and len(ws) >= 6:
        early = ws[:12]
        if early and max(early) <= 3:
            notes.append(("info",
                "Strong model agreement through the first 12 h "
                "(wind within \u00b13 kt) \u2014 high confidence near-term."))

    # Visibility concern
    vm = cons.get("vis_min", [])
    vm_valid = [(i, v) for i, v in enumerate(vm) if v is not None]
    if vm_valid:
        worst_i, worst_v = min(vm_valid, key=lambda x: x[1])
        if worst_v < 3.0:
            notes.append(("alert",
                f"At least one model drops visibility to {worst_v:.1f} SM "
                f"at {labels[worst_i]}."))

    return notes[:4]   # keep it to a few points, per design


# =============================================================================
# MOS support — ICAO resolver + ModelForecast adapter
# =============================================================================
# MOS is keyed by station ID, not lat/lon. We resolve nearest ICAO via
# AviationWeather.gov's bbox query, cached at module level for the process
# lifetime (ICAO locations don't change).

_ICAO_CACHE: dict = {}


def _nearest_icao_for_mos(lat: float, lon: float, max_distance_km: float = 50.0) -> str:
    """Returns nearest ICAO airport code within max_distance_km, or empty
    string if none found. Cached at module level by (lat, lon) at 1-decimal
    precision (~11 km grid).
    """
    key = (round(lat, 1), round(lon, 1))
    if key in _ICAO_CACHE:
        return _ICAO_CACHE[key]

    import math, urllib.request, json as _json
    min_lat, max_lat = lat - 1.0, lat + 1.0
    min_lon, max_lon = lon - 1.0, lon + 1.0
    url = (f"https://aviationweather.gov/api/data/taf"
           f"?bbox={min_lat},{min_lon},{max_lat},{max_lon}&format=json")
    best_icao = ""
    best_dist = float("inf")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "VectorCheck-ARMS/2.7"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        seen = set()
        for taf in data:
            icao_code = taf.get("icaoId")
            if not icao_code or icao_code in seen:
                continue
            seen.add(icao_code)
            try:
                s_lat = float(taf.get("lat"))
                s_lon = float(taf.get("lon"))
            except (TypeError, ValueError):
                continue
            R = 6371.0
            lat1, lat2 = math.radians(lat), math.radians(s_lat)
            dlat = lat2 - lat1
            dlon = math.radians(s_lon - lon)
            a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
            d_km = 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))
            if d_km <= max_distance_km and d_km < best_dist:
                best_dist = d_km
                best_icao = icao_code
    except Exception as e:
        logger.info("ICAO lookup failed for (%s, %s): %s", lat, lon, e)
    _ICAO_CACHE[key] = best_icao
    return best_icao


def _fetch_model_mos(station_id: str, display_name: str = "MOS") -> "ModelForecast":
    """Fetches MOS forecast for an ICAO station and returns it as a
    ModelForecast for ensemble inclusion. Returns invalid ModelForecast on
    any failure (station has no MOS coverage, credentials missing, etc).
    """
    mf = ModelForecast(name=display_name)
    if not station_id:
        return mf
    try:
        from modules.meteomatics_provider import fetch_meteomatics_mos
    except ImportError:
        return mf

    result = fetch_meteomatics_mos(station_id, hours_ahead=96)
    if result.get("error"):
        logger.info("MOS unavailable for %s: %s", station_id, result.get("message"))
        return mf

    h = result.get("hourly")
    if not h or "time" not in h:
        return mf

    def _truncate(key: str) -> list:
        raw = h.get(key) or []
        out = []
        for v in raw[:72]:
            if v is None:
                out.append(None)
            else:
                try:
                    out.append(float(v))
                except (TypeError, ValueError):
                    out.append(None)
        return out

    mf.times = h["time"][:72]
    mf.wind_kt = _truncate("wind_speed_10m")
    mf.wind_dir = _truncate("wind_direction_10m")
    mf.gust_kt = _truncate("wind_gusts_10m")
    mf.temp_c = _truncate("temperature_2m")
    mf.rh = _truncate("relative_humidity_2m")
    mf.pressure_hpa = _truncate("surface_pressure")
    # MOS doesn't return precip_prob or wx_code on the same parameter list —
    # fall back to None lists so downstream code doesn't choke.
    mf.precip_prob = [None] * len(mf.times)
    mf.wx_code = _truncate("weather_code")
    mf.valid = len(mf.wind_kt) >= 24
    return mf


# =============================================================================
# ANALYSIS
# =============================================================================

def _safe_mean(values: list) -> float:
    clean = [v for v in values if v is not None]
    return round(sum(clean) / len(clean), 1) if clean else 0.0


def _safe_min(values: list) -> float:
    clean = [v for v in values if v is not None]
    return round(min(clean), 1) if clean else 0.0


def _safe_max(values: list) -> float:
    clean = [v for v in values if v is not None]
    return round(max(clean), 1) if clean else 0.0


def _circular_mean(angles: list) -> float:
    """Vector-averaged wind direction."""
    clean = [a for a in angles if a is not None]
    if not clean:
        return 0.0
    sin_sum = sum(math.sin(math.radians(a)) for a in clean)
    cos_sum = sum(math.cos(math.radians(a)) for a in clean)
    return round(math.degrees(math.atan2(sin_sum, cos_sum)) % 360, 0)


def _circular_spread(angles: list) -> float:
    """Maximum angular distance between any two values in the list."""
    clean = [a for a in angles if a is not None]
    if len(clean) < 2:
        return 0.0
    max_spread = 0.0
    for i in range(len(clean)):
        for j in range(i + 1, len(clean)):
            diff = abs(((clean[i] - clean[j] + 180) % 360) - 180)
            if diff > max_spread:
                max_spread = diff
    return round(max_spread, 0)


def _get_block_label(base_time: datetime, offset_hours: int) -> str:
    """Returns a label like '18 Apr 00-12Z' for a 12-hour block."""
    start = base_time + timedelta(hours=offset_hours)
    end = base_time + timedelta(hours=offset_hours + BLOCK_HOURS)
    if start.day == end.day:
        return f"{start.strftime('%d %b')} {start.strftime('%H')}-{end.strftime('%H')}Z"
    else:
        return f"{start.strftime('%d %b %H')}Z-{end.strftime('%d %b %H')}Z"


def compute_ensemble_blocks(models: list) -> list:
    """Computes 12-hour block statistics across the model ensemble.

    For each block, computes the mean of each model's block-average,
    then measures the spread (max - min) across models.
    """
    if not models:
        return []

    # Determine the common time range
    max_hours = min(len(m.wind_kt) for m in models)
    max_hours = min(max_hours, 72)
    n_blocks = max_hours // BLOCK_HOURS

    try:
        base_time = datetime.fromisoformat(models[0].times[0]).replace(tzinfo=timezone.utc)
    except (ValueError, IndexError):
        base_time = datetime.now(timezone.utc)

    blocks = []

    for b in range(n_blocks):
        start_h = b * BLOCK_HOURS
        end_h = start_h + BLOCK_HOURS
        bs = BlockStats(
            block_label=_get_block_label(base_time, start_h),
            start_hour=start_h,
            model_count=len(models),
        )

        # Collect each model's block-average for each variable
        model_wind_avgs = []
        model_dir_avgs = []
        model_gust_maxes = []
        model_temp_avgs = []
        model_pressure_avgs = []
        model_precip_maxes = []

        for m in models:
            w_slice = m.wind_kt[start_h:end_h]
            d_slice = m.wind_dir[start_h:end_h]
            g_slice = m.gust_kt[start_h:end_h]
            t_slice = m.temp_c[start_h:end_h]
            p_slice = m.pressure_hpa[start_h:end_h]
            pp_slice = m.precip_prob[start_h:end_h]

            model_wind_avgs.append(_safe_mean(w_slice))
            model_dir_avgs.append(_circular_mean(d_slice))
            model_gust_maxes.append(_safe_max(g_slice))
            model_temp_avgs.append(_safe_mean(t_slice))
            model_pressure_avgs.append(_safe_mean(p_slice))
            model_precip_maxes.append(_safe_max(pp_slice))

        bs.wind_mean = _safe_mean(model_wind_avgs)
        bs.wind_min = _safe_min(model_wind_avgs)
        bs.wind_max = _safe_max(model_wind_avgs)
        bs.wind_spread = round(bs.wind_max - bs.wind_min, 1)

        bs.wind_dir_mean = _circular_mean(model_dir_avgs)
        bs.wind_dir_spread = _circular_spread(model_dir_avgs)

        bs.gust_mean = _safe_mean(model_gust_maxes)
        bs.gust_max = _safe_max(model_gust_maxes)
        bs.gust_spread = round(_safe_max(model_gust_maxes) - _safe_min(model_gust_maxes), 1)

        bs.temp_mean = _safe_mean(model_temp_avgs)
        bs.temp_min = _safe_min(model_temp_avgs)
        bs.temp_max = _safe_max(model_temp_avgs)
        bs.temp_spread = round(bs.temp_max - bs.temp_min, 1)

        bs.pressure_mean = _safe_mean(model_pressure_avgs)
        bs.pressure_spread = round(_safe_max(model_pressure_avgs) - _safe_min(model_pressure_avgs), 1)

        bs.precip_prob_max = _safe_max(model_precip_maxes)

        # Confidence for this block
        if bs.wind_spread >= WIND_SPREAD_ALERT_KT or bs.temp_spread >= TEMP_SPREAD_WARN_C:
            bs.confidence = "LOW"
        elif bs.wind_spread >= WIND_SPREAD_WARN_KT or bs.gust_spread >= GUST_SPREAD_WARN_KT:
            bs.confidence = "MODERATE"
        else:
            bs.confidence = "HIGH"

        blocks.append(bs)

    return blocks


def identify_risk_windows(blocks: list) -> list:
    """Scans blocks for periods where model divergence exceeds thresholds."""
    risks = []

    for bs in blocks:
        if bs.wind_spread >= WIND_SPREAD_ALERT_KT:
            risks.append(RiskWindow(
                start_label=bs.block_label,
                end_label="",
                variable="Wind Speed",
                spread=bs.wind_spread,
                detail=f"Models range {bs.wind_min:.0f}-{bs.wind_max:.0f} kt (spread {bs.wind_spread:.0f} kt)",
                severity="ALERT",
            ))
        elif bs.wind_spread >= WIND_SPREAD_WARN_KT:
            risks.append(RiskWindow(
                start_label=bs.block_label,
                end_label="",
                variable="Wind Speed",
                spread=bs.wind_spread,
                detail=f"Models range {bs.wind_min:.0f}-{bs.wind_max:.0f} kt (spread {bs.wind_spread:.0f} kt)",
                severity="WARN",
            ))

        if bs.gust_spread >= GUST_SPREAD_WARN_KT:
            risks.append(RiskWindow(
                start_label=bs.block_label,
                end_label="",
                variable="Gusts",
                spread=bs.gust_spread,
                detail=f"Gust forecasts range up to {bs.gust_max:.0f} kt (spread {bs.gust_spread:.0f} kt)",
                severity="WARN",
            ))

        if bs.temp_spread >= TEMP_SPREAD_WARN_C:
            risks.append(RiskWindow(
                start_label=bs.block_label,
                end_label="",
                variable="Temperature",
                spread=bs.temp_spread,
                detail=f"Models range {bs.temp_min:.0f}-{bs.temp_max:.0f}\u00b0C (spread {bs.temp_spread:.0f}\u00b0C)",
                severity="WARN",
            ))

        if bs.wind_dir_spread >= 60:
            risks.append(RiskWindow(
                start_label=bs.block_label,
                end_label="",
                variable="Wind Direction",
                spread=bs.wind_dir_spread,
                detail=f"Direction spread {bs.wind_dir_spread:.0f}\u00b0 across models",
                severity="WARN",
            ))

    return risks


# =============================================================================
# BRIEFING GENERATION
# =============================================================================

def generate_briefing(
    models: list,
    blocks: list,
    risk_windows: list,
    climate_ctx: dict = None,
) -> EnsembleBriefing:
    """Generates a complete deterministic atmospheric intelligence briefing.

    Args:
        models: list of valid ModelForecast objects
        blocks: list of BlockStats from compute_ensemble_blocks
        risk_windows: list of RiskWindow from identify_risk_windows
        climate_ctx: dict from fetch_climate_context_cached (optional)
    """
    # Expected ensemble is always 4 models: 1 regional + GFS + ECMWF + ICON
    # Missing models are inferred from expected names vs models_used
    _expected = {"GFS", "ECMWF", "ICON"}
    _used_names = {m.name for m in models}
    _missing_global = [n for n in _expected if n not in _used_names]
    # Regional model is implicit — if we have 3 globals but no 4th model, regional failed
    _regional_used = [n for n in _used_names if n not in _expected]
    if not _regional_used:
        _missing_global.append("Regional")

    brief = EnsembleBriefing(
        generated_utc=datetime.now(timezone.utc),
        model_count=len(models),
        models_used=[m.name for m in models],
        models_failed=_missing_global,
        blocks=blocks,
        risk_windows=risk_windows,
    )

    if not blocks:
        brief.consensus_summary = "Insufficient model data for analysis."
        brief.overall_confidence = "LOW"
        return brief

    # --- Overall confidence ---
    low_blocks = sum(1 for b in blocks if b.confidence == "LOW")
    mod_blocks = sum(1 for b in blocks if b.confidence == "MODERATE")
    total = len(blocks)

    if low_blocks >= total * 0.3:
        brief.overall_confidence = "LOW"
    elif (low_blocks + mod_blocks) >= total * 0.4:
        brief.overall_confidence = "MODERATE"
    else:
        brief.overall_confidence = "HIGH"

    # --- Consensus summary ---
    first_12 = blocks[0] if blocks else None
    parts = []

    if first_12 and first_12.wind_spread <= WIND_SPREAD_WARN_KT:
        dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
        dir_idx = int(round(first_12.wind_dir_mean / 45.0)) % 8
        dir_name = dirs[dir_idx]
        parts.append(
            f"All {len(models)} models agree on {dir_name} flow "
            f"{first_12.wind_min:.0f}-{first_12.wind_max:.0f} kt "
            f"for the first 12 hours."
        )
    elif first_12:
        parts.append(
            f"Models diverge on wind speed in the first 12 hours: "
            f"range {first_12.wind_min:.0f}-{first_12.wind_max:.0f} kt "
            f"(spread {first_12.wind_spread:.0f} kt)."
        )

    # Check for convergence/divergence trend across blocks
    if len(blocks) >= 4:
        early_spread = _safe_mean([b.wind_spread for b in blocks[:2]])
        late_spread = _safe_mean([b.wind_spread for b in blocks[-2:]])
        if late_spread > early_spread + 3:
            parts.append(
                f"Model agreement degrades after +{blocks[2].start_hour}h "
                f"(wind spread increases from {early_spread:.0f} to {late_spread:.0f} kt)."
            )
        elif early_spread > late_spread + 3:
            parts.append(
                "Models converge toward end of forecast period — "
                "later windows may be more reliable than mid-range."
            )

    brief.consensus_summary = " ".join(parts) if parts else "Model agreement within normal parameters across all blocks."

    # --- Wind summary ---
    max_wind_block = max(blocks, key=lambda b: b.wind_mean)
    max_gust_block = max(blocks, key=lambda b: b.gust_max)
    brief.wind_summary = (
        f"Peak ensemble mean wind: {max_wind_block.wind_mean:.0f} kt "
        f"({max_wind_block.block_label}). "
        f"Peak gust forecast: {max_gust_block.gust_max:.0f} kt "
        f"({max_gust_block.block_label})."
    )

    # --- Precip summary ---
    precip_blocks = [b for b in blocks if b.precip_prob_max >= 30]
    if precip_blocks:
        labels = ", ".join(b.block_label for b in precip_blocks)
        max_pp = max(b.precip_prob_max for b in precip_blocks)
        brief.precip_summary = (
            f"Precipitation probability \u226530% in: {labels}. "
            f"Peak probability: {max_pp:.0f}%."
        )
    else:
        brief.precip_summary = "No significant precipitation expected across the 72h window."

    # --- Anomaly flags from climate context ---
    if climate_ctx and climate_ctx.get("wind", {}).get("n", 0) > 0:
        w_p50 = climate_ctx["wind"]["p50"]
        w_p90 = climate_ctx["wind"]["p90"]
        t_p10 = climate_ctx["temp"]["p10"]
        t_p90 = climate_ctx["temp"]["p90"]

        for b in blocks:
            if b.wind_mean > w_p90:
                brief.anomaly_flags.append(
                    f"{b.block_label}: ensemble mean wind {b.wind_mean:.0f} kt "
                    f"exceeds P90 climate normal ({w_p90:.0f} kt)"
                )
            if b.temp_mean < t_p10:
                brief.anomaly_flags.append(
                    f"{b.block_label}: ensemble mean temp {b.temp_mean:.1f}\u00b0C "
                    f"below P10 climate normal ({t_p10:.1f}\u00b0C)"
                )
            if b.temp_mean > t_p90:
                brief.anomaly_flags.append(
                    f"{b.block_label}: ensemble mean temp {b.temp_mean:.1f}\u00b0C "
                    f"exceeds P90 climate normal ({t_p90:.1f}\u00b0C)"
                )

    # --- Confidence summary ---
    conf_parts = []
    conf_parts.append(f"Overall confidence: {brief.overall_confidence}.")
    conf_parts.append(f"{len(models)}/4 models reporting.")

    if brief.models_failed:
        conf_parts.append(f"Missing: {', '.join(brief.models_failed)}.")

    high_conf = [b for b in blocks if b.confidence == "HIGH"]
    if high_conf:
        conf_parts.append(
            f"High-confidence windows: "
            f"{', '.join(b.block_label for b in high_conf[:3])}."
        )

    low_conf = [b for b in blocks if b.confidence == "LOW"]
    if low_conf:
        conf_parts.append(
            f"Low-confidence windows: "
            f"{', '.join(b.block_label for b in low_conf[:3])}."
        )

    brief.confidence_summary = " ".join(conf_parts)

    return brief
