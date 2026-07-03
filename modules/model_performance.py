"""
VECTOR CHECK AERIAL GROUP INC. — Model Performance Scorecard

Computes trailing 24-hour forecast performance per NWP model by comparing
each model's historical predictions (via Open-Meteo Previous Runs API)
against ground-truth observations from the nearest METAR station and any
Kestrel 5500 uploads within the window.

DATA FLOW:
    For each model in the active ensemble:
      1. Fetch past_days=1 from the model's Open-Meteo endpoint
         → yields hourly predictions for the trailing 24h
      2. Fetch METAR history from AviationWeather.gov
         → yields hourly observed conditions for the same window
      3. Optionally fetch Kestrel sessions from Supabase
         → adds operator ground truth at the launch site

    For each paired (forecast hour, observation hour):
      - Compute absolute error per variable (wind, gust, temp, pressure)

    Aggregate across all paired hours:
      - MAE = mean absolute error per model per variable

OUTPUT:
    dict keyed by model name, each containing:
      - wind_mae_kt, gust_mae_kt, temp_mae_c, pressure_mae_hpa
      - sample_count (how many paired hours contributed)
      - best_performer flag (lowest weighted composite error)

COST: $0 — Open-Meteo Previous Runs API uses the same quota as forecast calls.
"""

import urllib.request
import urllib.parse
import json
import logging
import math
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("arms.model_performance")

# Shared constants with ensemble_analysis
from modules.ensemble_analysis import (
    MODEL_ENDPOINTS,
    REGIONAL_MODELS,
    _select_regional_model,
    _is_conus_coverage,
    KMH_TO_KT,
    REQUEST_TIMEOUT_S,
    USER_AGENT,
)

# Variables needed for the scorecard. Visibility is returned by all 4 ensemble
# endpoints (HRDPS, GFS, ECMWF, ICON) — Open-Meteo serves visibility for the
# standard 10m wind/2m temp endpoints uniformly across these models.
_PERF_VARS = (
    "wind_speed_10m,wind_direction_10m,wind_gusts_10m,"
    "temperature_2m,pressure_msl,relative_humidity_2m,visibility"
)
# Note: pressure_msl (mean sea level pressure) is used for verification —
# METAR reports altimeter setting which is also sea-level-adjusted. The main
# dashboard separately uses surface_pressure for density altitude / takeoff
# computations at the actual elevation; this is the scorecard-only choice.

# MAE tolerance thresholds (green / amber / red)
WIND_MAE_GOOD_KT = 2.0     # green if MAE below this
WIND_MAE_WARN_KT = 4.0     # amber up to this, red above
GUST_MAE_GOOD_KT = 3.0
GUST_MAE_WARN_KT = 5.0
TEMP_MAE_GOOD_C = 1.5
TEMP_MAE_WARN_C = 3.0
PRESSURE_MAE_GOOD_HPA = 1.5
PRESSURE_MAE_WARN_HPA = 3.0
RH_MAE_GOOD_PCT = 5.0      # RH errors are typically small
RH_MAE_WARN_PCT = 12.0
DIR_MAE_GOOD_DEG = 15.0    # within a typical wind direction sector
DIR_MAE_WARN_DEG = 30.0
VIS_MAE_GOOD_SM = 1.0      # visibility error tolerances (statute miles)
VIS_MAE_WARN_SM = 3.0


# =============================================================================
# HISTORICAL FORECAST FETCH (Open-Meteo Previous Runs)
# =============================================================================

def _sanitize_model_wind(ws_list: list, gust_list: list) -> tuple:
    """Physical-plausibility filter for model surface winds.

    Nulls out non-physical samples rather than letting them poison the MAE:
      - Surface (10 m) sustained wind outside [0, 120] kt → unit error / corrupt cell
      - Gust outside [0, 180] kt → same
      - Gust reported below its own sustained wind (by > 0.5 kt) → bad data

    Primary motivation: Open-Meteo's seamless model nesting silently backfills
    HRRR gaps with GFS, which produced erroneous fallback wind speeds. A unit
    mismatch (m/s read as kn, etc.) also surfaces as absurdly high values this
    catches. Operates in place and also returns the lists for chaining.
    """
    n = len(ws_list)
    for j in range(n):
        w = ws_list[j]
        if w is not None and (w < 0 or w > 120):
            ws_list[j] = None
        g = gust_list[j] if j < len(gust_list) else None
        if g is not None and (g < 0 or g > 180):
            gust_list[j] = None
        elif (g is not None and ws_list[j] is not None and g < ws_list[j] - 0.5):
            gust_list[j] = None
    return ws_list, gust_list


def _fetch_model_history_meteomatics_mos(station_id: str, lat: float, lon: float,
                                          display_name: str = "MOS") -> dict:
    """Fetches 24h of historical MOS forecast data for the scorecard.

    MOS is statistically-tuned per-station forecast — when scored against
    METAR observations at the same station it often beats raw NWP by
    10-30% on surface variables.

    Costs 1 batch (10 quota units) per call. Returns the same dict shape
    as _fetch_model_history or None on failure (e.g. station has no MOS).
    """
    if not station_id:
        return None
    try:
        from modules.meteomatics_provider import (
            METEOMATICS_BASE, _get_credentials,
        )
    except ImportError:
        return None

    creds = _get_credentials()
    if creds is None:
        return None

    # Backward 24h + forward 6h
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(hours=24)
    end = now + timedelta(hours=6)
    validdate = f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}--{end.strftime('%Y-%m-%dT%H:%M:%SZ')}:PT1H"

    mm_params = [
        "t_2m:C", "relative_humidity_2m:p",
        "wind_speed_10m:kn", "wind_dir_10m:d", "wind_gusts_10m_1h:kn",
        "msl_pressure:hPa",
    ]
    param_str = ",".join(mm_params)
    url = (f"{METEOMATICS_BASE}/{validdate}/{param_str}/{station_id}"
           f"/json?source=mm-mos")

    try:
        from modules.http_client import fetch_json as _fetch_json, HttpFetchError as _HttpFetchError
        from modules.meteomatics_provider import _mm_fetch_json as _mmfj
        payload = _mmfj(url, timeout=REQUEST_TIMEOUT_S, retries=1, basic_auth=creds)
    except _HttpFetchError as e:
        logger.info("MOS history fetch failed for %s: %s", display_name, e)
        return None

    # Index by parameter, then filter to past hours only (same logic as
    # _fetch_model_history)
    by_param: dict = {}
    times_iso: list = []
    for block in (payload.get("data") or []):
        coords = block.get("coordinates") or []
        if not coords:
            continue
        dates = coords[0].get("dates") or []
        if not dates:
            continue
        by_param[block.get("parameter")] = dates
        if not times_iso:
            times_iso = [(d["date"][:-1] if d["date"].endswith("Z") else d["date"])[:16]
                         for d in dates]

    if not times_iso:
        return None

    kept_indices = []
    now_filter = datetime.now(timezone.utc)
    for i, t_str in enumerate(times_iso):
        try:
            t = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        age_hours = (now_filter - t).total_seconds() / 3600.0
        if 0 <= age_hours <= 24:
            kept_indices.append(i)

    if not kept_indices:
        return None

    def _pick(mm_key: str) -> list:
        dates = by_param.get(mm_key) or []
        out = []
        for idx in kept_indices:
            if idx < len(dates):
                val = dates[idx].get("value")
                if val is None:
                    out.append(None)
                else:
                    try:
                        f = float(val)
                        out.append(None if f <= -998 else f)
                    except (TypeError, ValueError):
                        out.append(None)
            else:
                out.append(None)
        return out

    return {
        "times": [times_iso[i] for i in kept_indices],
        "wind_kt":      _pick("wind_speed_10m:kn"),
        "wind_dir":     _pick("wind_dir_10m:d"),
        "gust_kt":      _pick("wind_gusts_10m_1h:kn"),
        "temp_c":       _pick("t_2m:C"),
        "pressure_hpa": _pick("msl_pressure:hPa"),
        "rh":           _pick("relative_humidity_2m:p"),
        "visibility_sm": [None] * len(kept_indices),
    }


def _fetch_model_history_meteomatics(model: str, lat: float, lon: float,
                                       display_name: str = "MIX") -> dict:
    """Fetches 24h of historical Meteomatics forecast data for the scorecard.

    Different from _fetch_model_history in two ways:
      1. Uses Meteomatics' API (HTTP Basic Auth, different URL format) rather
         than Open-Meteo
      2. Only fetches the 8 surface variables the scorecard scores against —
         that's a single 10-param batch (10 quota units) instead of the
         95-param full ARMS request (100 units). Big quota saving since
         pressure-level data isn't used in MAE scoring.

    Returns the same dict shape as _fetch_model_history, or None on failure.
    """
    try:
        from modules.meteomatics_provider import (
            METEOMATICS_BASE, METEOMATICS_MODELS, _get_credentials,
        )
    except ImportError:
        return None

    creds = _get_credentials()
    if creds is None or model not in METEOMATICS_MODELS:
        return None

    # Backward 24h + forward 6h to capture the past day of forecast data
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(hours=24)
    end = now + timedelta(hours=6)
    validdate = f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}--{end.strftime('%Y-%m-%dT%H:%M:%SZ')}:PT1H"

    # 6 scorecard-essential parameters. The MAE scoring against METAR uses
    # wind speed/dir/gust, temperature, RH, and pressure — nothing else.
    # Earlier versions included visibility:m and weather_symbol_1h:idx but
    # those are not available on raw NWP models (ecmwf-ifs, ncep-gfs,
    # ncep-hrrr, ecmwf-aifs) — only on the MIX bias-corrected blend. Since
    # Meteomatics returns 404 for the whole request when any single param
    # is unavailable, including those broke every model except MIX. They're
    # not used in scoring anyway, so they're gone.
    #
    # Pressure uses msl_pressure:hPa (sea-level-adjusted), not sfc_pressure,
    # so it's directly comparable to METAR altimeter setting. The main
    # forecast path keeps surface_pressure for density altitude work.
    # Gust parameter is model-dependent. The bias-corrected MIX blend carries
    # the statistically-aggregated wind_gusts_10m_1h:kn, but raw NWP models
    # (HRRR, AIFS, GFS, ECMWF-IFS) on the vectorcheck subscription do NOT all
    # expose the _1h aggregation — requesting it 404s the whole batch (all-or-
    # nothing semantics). Raw models carry the instantaneous wind_gusts_10m:kn
    # instead. We pick per-model and record which gust param we used so the
    # parser reads the right block back.
    _RAW_MODELS_INSTANT_GUST = {"ncep-hrrr", "ncep-gfs", "ecmwf-ifs", "ecmwf-aifs"}
    model_id = METEOMATICS_MODELS[model]
    if model_id in _RAW_MODELS_INSTANT_GUST:
        _gust_param = "wind_gusts_10m:kn"
    else:
        _gust_param = "wind_gusts_10m_1h:kn"

    mm_params = [
        "t_2m:C", "relative_humidity_2m:p",
        "wind_speed_10m:kn", "wind_dir_10m:d", _gust_param,
        "msl_pressure:hPa",
    ]
    # Additionally filter against the per-model blocklist (in case future
    # additions to mm_params accidentally include a blend-only param).
    try:
        from modules.meteomatics_provider import _MODEL_PARAM_BLOCKLIST
        block = _MODEL_PARAM_BLOCKLIST.get(METEOMATICS_MODELS[model], set())
        mm_params = [p for p in mm_params if p not in block]
    except (ImportError, KeyError):
        pass
    param_str = ",".join(mm_params)
    url = f"{METEOMATICS_BASE}/{validdate}/{param_str}/{lat:.4f},{lon:.4f}/json?model={model_id}"

    try:
        from modules.http_client import fetch_json as _fetch_json, HttpFetchError as _HttpFetchError
        from modules.meteomatics_provider import _mm_fetch_json as _mmfj
        payload = _mmfj(url, timeout=REQUEST_TIMEOUT_S, retries=1, basic_auth=creds)
    except _HttpFetchError as e:
        logger.warning("Meteomatics history fetch failed for %s: %s", display_name, e)
        return None

    # Index Meteomatics response by parameter name
    data_blocks = payload.get("data") or []
    by_param: dict = {}
    times_iso: list = []
    for block in data_blocks:
        coords = block.get("coordinates") or []
        if not coords:
            continue
        dates = coords[0].get("dates") or []
        if not dates:
            continue
        by_param[block.get("parameter")] = dates
        if not times_iso:
            # Normalize to Open-Meteo format ("YYYY-MM-DDTHH:MM", no Z, no seconds)
            times_iso = [(d["date"][:-1] if d["date"].endswith("Z") else d["date"])[:16]
                         for d in dates]

    if not times_iso:
        return None

    # Keep only past hours (matching _fetch_model_history's filter)
    kept_indices = []
    now_filter = datetime.now(timezone.utc)
    for i, t_str in enumerate(times_iso):
        try:
            t = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        age_hours = (now_filter - t).total_seconds() / 3600.0
        if 0 <= age_hours <= 24:
            kept_indices.append(i)

    if not kept_indices:
        return None

    def _pick(mm_key: str, scale: float = 1.0) -> list:
        dates = by_param.get(mm_key) or []
        out = []
        for idx in kept_indices:
            if idx < len(dates):
                val = dates[idx].get("value")
                if val is None:
                    out.append(None)
                else:
                    try:
                        out.append(float(val) * scale)
                    except (TypeError, ValueError):
                        out.append(None)
            else:
                out.append(None)
        return out

    # Gust block name depends on which variant we requested for this model
    # (see _RAW_MODELS_INSTANT_GUST above). Read whichever one came back so
    # raw models (instantaneous gust) and the MIX blend (_1h gust) both parse.
    if "wind_gusts_10m_1h:kn" in by_param:
        _gust_key = "wind_gusts_10m_1h:kn"
    elif "wind_gusts_10m:kn" in by_param:
        _gust_key = "wind_gusts_10m:kn"
    else:
        _gust_key = "wind_gusts_10m_1h:kn"   # absent → _pick returns all-None

    _mm_wind = _pick("wind_speed_10m:kn")
    _mm_gust = _pick(_gust_key)
    _mm_wind, _mm_gust = _sanitize_model_wind(_mm_wind, _mm_gust)

    return {
        "times": [times_iso[i] for i in kept_indices],
        "wind_kt":      _mm_wind,
        "wind_dir":     _pick("wind_dir_10m:d"),
        "gust_kt":      _mm_gust,
        "temp_c":       _pick("t_2m:C"),
        "pressure_hpa": _pick("msl_pressure:hPa"),
        "rh":           _pick("relative_humidity_2m:p"),
        "visibility_sm": _pick("visibility:m", 1.0 / 1609.344),
    }


def _fetch_model_history(model_name: str, endpoint_url: str, lat: float, lon: float) -> dict:
    """Fetches 24-hour historical forecast from one model.

    Returns dict with 'times', 'wind_kt', 'gust_kt', 'wind_dir', 'temp_c',
    'pressure_hpa', 'rh' lists, or None on failure.
    """
    # If the endpoint URL already contains a query string (e.g. CONUS-specific
    # endpoints with "?models=ncep_hrrr_conus"), append our params with &
    sep = "&" if "?" in endpoint_url else "?"
    url = (
        f"{endpoint_url}{sep}latitude={lat}&longitude={lon}"
        f"&hourly={_PERF_VARS}"
        f"&past_days=1&forecast_days=1"
        f"&timezone=UTC"
        f"&wind_speed_unit=kn"
    )

    try:
        from modules.http_client import fetch_json as _fetch_json, HttpFetchError as _HttpFetchError
        data = _fetch_json(url, timeout=REQUEST_TIMEOUT_S, retries=2)
    except _HttpFetchError as e:
        logger.warning("Model history fetch failed for %s: %s", model_name, e)
        return None

    h = data.get("hourly")
    if not h or "time" not in h:
        return None

    # Keep only hours that are in the past (already occurred) — these are
    # the only hours we can compare against observations
    now = datetime.now(timezone.utc)
    times_iso = h["time"]

    kept_indices = []
    for i, t_str in enumerate(times_iso):
        try:
            t = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        # Only hours within the last 24 hours, and not in the future
        age_hours = (now - t).total_seconds() / 3600.0
        if 0 <= age_hours <= 24:
            kept_indices.append(i)

    if not kept_indices:
        return None

    # Robust wind-unit detection — same pattern as ensemble_analysis fetcher.
    _wu = data.get("hourly_units", {}).get("wind_speed_10m", "kn").lower()
    if "km/h" in _wu:   _wind_scale = 0.539957
    elif "m/s" in _wu:  _wind_scale = 1.943844
    elif "mph" in _wu:  _wind_scale = 0.868976
    else:               _wind_scale = 1.0

    def _pick(key, scale=1.0):
        raw = h.get(key, [])
        out = []
        for idx in kept_indices:
            if idx < len(raw) and raw[idx] is not None:
                try:
                    out.append(float(raw[idx]) * scale)
                except (TypeError, ValueError):
                    out.append(None)
            else:
                out.append(None)
        return out

    _wind = _pick("wind_speed_10m", _wind_scale)
    _gust = _pick("wind_gusts_10m", _wind_scale)
    _dir = _pick("wind_direction_10m")

    # Physical-plausibility sanity filter (shared with the Meteomatics parser).
    # Catches unit errors, corrupt cells, and the seamless-nesting backfill
    # that mixed HRRR with GFS and produced erroneous fallback winds.
    _wind, _gust = _sanitize_model_wind(_wind, _gust)

    return {
        "times": [times_iso[i] for i in kept_indices],
        "wind_kt": _wind,
        "wind_dir": _dir,
        "gust_kt": _gust,
        "temp_c": _pick("temperature_2m"),
        "pressure_hpa": _pick("pressure_msl"),
        "rh": _pick("relative_humidity_2m"),
        # Open-Meteo returns visibility in meters; convert to statute miles
        # to match METAR's vsby field. Some endpoints don't include this.
        "visibility_sm": _pick("visibility", 1.0 / 1609.344),
    }


# =============================================================================
# DISTANCE-WEIGHTED VERIFICATION (the statistically professional aggregator)
# =============================================================================
# Methodology: for each METAR station inside the verification radius, fetch
# the model's forecast at THAT station's coordinates. Score each station's
# forecast against that station's observations independently. Aggregate the
# per-station MAEs into a single model MAE using a top-hat plateau (all
# stations within 10 km weighted equally) plus exponential decay beyond:
#     w(d) = 1.0                          for d <= 10 km
#     w(d) = exp(-(d - 10) / 25)          for d >  10 km
# A station at 35 km from site (one decay length past plateau) gets ~37%
# weight relative to a plateau-region station; at 60 km it's ~14%.
#
# This eliminates the spatial mismatch in the simpler "forecast at site vs
# obs at nearby stations" approach by making the forecast and the obs share
# a coordinate per pairing. Cost: N additional model fetches per scorecard
# refresh, where N is the number of in-radius stations beyond the first.


_WEIGHT_PLATEAU_KM = 10.0
_WEIGHT_DECAY_KM = 25.0
# A station must have at least this many hours of valid observations within
# the past 24h to be included in scoring at all. Drops stations with sparse
# obs that would otherwise produce statistically meaningless MAEs.
_MIN_OBS_HOURS_FOR_INCLUSION = 12


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two coordinates, in km."""
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = p2 - p1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _distance_weight(d_km: float) -> float:
    """Top-hat plateau within 10 km + exponential decay beyond.
    See module-level methodology docstring."""
    if d_km <= _WEIGHT_PLATEAU_KM:
        return 1.0
    return math.exp(-(d_km - _WEIGHT_PLATEAU_KM) / _WEIGHT_DECAY_KM)


def _group_observations_by_station(observations: list) -> dict:
    """Returns {station_id: [obs, obs, ...]} for obs that have a station_id
    AND attached _lat/_lon. Obs without coordinates are dropped from
    weighted scoring (they can't be distance-weighted).
    """
    by_station: dict = {}
    for o in observations:
        sid = o.get("station_id")
        if not sid:
            continue
        if o.get("_lat") is None or o.get("_lon") is None:
            continue
        by_station.setdefault(sid, []).append(o)
    return by_station


# Module-level cache for per-station model forecasts. Key = (model_name,
# round(lat,3), round(lon,3), hour_bucket). Forecasts at the same station
# don't need to be re-fetched across operators with overlapping station
# footprints (e.g. CYTR serves both Belleville and Petawawa scorecards).
# Cached values live for 15 minutes; we put the hour_bucket in the key so
# stale values naturally expire on the hour rotation.
_STATION_FORECAST_CACHE: dict = {}


def _cache_key(model_name: str, lat: float, lon: float) -> tuple:
    """Build a stable cache key. Hour bucket means cache entries auto-expire
    on the hour rotation without needing a TTL sweep."""
    hour_bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    return (model_name, round(lat, 3), round(lon, 3), hour_bucket)


def _fetch_model_history_at_stations(
    model_name: str,
    endpoint_url: str,
    stations: list,
) -> dict:
    """Fetches the model's history at each station's coordinates in parallel.

    Args:
        model_name:    display label (used for cache key + dispatch)
        endpoint_url:  the URL or marker (meteomatics:// or http(s)://)
        stations:      list of (station_id, lat, lon) tuples

    Returns:
        dict {station_id: history_dict_or_None}.
    """
    if not stations:
        return {}

    # Dispatch helper — picks the right per-coordinate fetcher based on URL
    def _fetch_one_station(sid: str, slat: float, slon: float):
        key = _cache_key(model_name, slat, slon)
        if key in _STATION_FORECAST_CACHE:
            return sid, _STATION_FORECAST_CACHE[key]
        try:
            if endpoint_url.startswith("meteomatics-mos://"):
                # MOS is station-keyed natively. Use station_id directly.
                history = _fetch_model_history_meteomatics_mos(
                    f"metar_{sid}", slat, slon, display_name=f"{model_name}@{sid}"
                )
            elif endpoint_url.startswith("meteomatics://"):
                model_id = endpoint_url.replace("meteomatics://", "")
                history = _fetch_model_history_meteomatics(
                    model_id, slat, slon, display_name=f"{model_name}@{sid}"
                )
            else:
                history = _fetch_model_history(
                    f"{model_name}@{sid}", endpoint_url, slat, slon
                )
        except Exception as e:
            logger.info("Per-station fetch failed for %s at %s: %s", model_name, sid, e)
            history = None
        _STATION_FORECAST_CACHE[key] = history
        return sid, history

    from concurrent.futures import ThreadPoolExecutor, as_completed
    results: dict = {}
    with ThreadPoolExecutor(max_workers=max(2, len(stations))) as ex:
        futures = [ex.submit(_fetch_one_station, sid, slat, slon)
                   for (sid, slat, slon) in stations]
        for fut in as_completed(futures):
            try:
                sid, history = fut.result()
                results[sid] = history
            except Exception as e:
                logger.warning("Per-station worker crashed for %s: %s", model_name, e)
    return results


def compute_weighted_model_mae(
    per_station_histories: dict,
    obs_by_station: dict,
    site_lat: float,
    site_lon: float,
) -> dict:
    """Distance-weighted aggregation of per-station MAEs.

    Args:
        per_station_histories: {station_id: history_dict_or_None}
        obs_by_station:        {station_id: [obs, obs, ...]}
        site_lat, site_lon:    user's site coordinates (for distance computation)

    Returns:
        dict with same shape as compute_model_mae output:
          wind_mae_kt, dir_mae_deg, gust_mae_kt, temp_mae_c, pressure_mae_hpa,
          rh_mae_pct, vis_mae_sm, sample_count, *_n counts, earliest/latest_obs_time
        Aggregation:
          - per-station MAE computed via the existing compute_model_mae
          - per-station weight from _distance_weight()
          - station dropped if it has fewer than _MIN_OBS_HOURS_FOR_INCLUSION
            valid observation hours
          - reported MAE = weighted mean of per-station MAEs (per variable)
          - sample_count, *_n counts = sum across contributing stations
    """
    # Build (station_id, distance, per-station MAE) records
    per_station_records: list = []
    for sid, history in per_station_histories.items():
        if history is None:
            continue
        obs = obs_by_station.get(sid) or []
        # Apply the completeness filter — drop sparsely-reporting stations
        if len(obs) < _MIN_OBS_HOURS_FOR_INCLUSION:
            continue
        # Need station coords to compute distance — pull from the first obs
        first = obs[0]
        slat, slon = first.get("_lat"), first.get("_lon")
        if slat is None or slon is None:
            continue
        d_km = _haversine_km(site_lat, site_lon, slat, slon)
        # Combined weight = distance × quality. METAR observations don't
        # set _quality_weight (default 1.0). Non-METAR stations (synoptic,
        # marine, coop) have quality < 1.0 set by find_station discovery.
        # See _quality_weight_for_category in meteomatics_provider.
        quality_w = float(first.get("_quality_weight", 1.0))
        w = _distance_weight(d_km) * quality_w
        mae = compute_model_mae(history, obs)
        per_station_records.append({
            "sid": sid,
            "distance_km": d_km,
            "quality_weight": quality_w,
            "weight": w,
            "mae": mae,
        })

    # If no stations qualified, return empty record with the standard shape
    if not per_station_records:
        return {
            "wind_mae_kt": None, "dir_mae_deg": None, "gust_mae_kt": None,
            "temp_mae_c": None, "pressure_mae_hpa": None, "rh_mae_pct": None,
            "vis_mae_sm": None,
            "sample_count": 0,
            "wind_n": 0, "dir_n": 0, "gust_n": 0, "temp_n": 0,
            "pressure_n": 0, "rh_n": 0, "vis_n": 0,
            "earliest_obs_time": None, "latest_obs_time": None,
            "_stations_used": [],
        }

    # Weighted mean per variable. Only include stations that have a non-None
    # MAE for that variable (a station with no temperature obs shouldn't drag
    # the temperature aggregate).
    def _weighted_mean(key: str) -> tuple:
        """Returns (weighted_mean_or_None, sum_of_n_across_stations)."""
        weighted_sum = 0.0
        weight_total = 0.0
        n_total = 0
        n_key = key.replace("_mae_kt", "_n").replace("_mae_deg", "_n") \
                   .replace("_mae_c", "_n").replace("_mae_hpa", "_n") \
                   .replace("_mae_pct", "_n").replace("_mae_sm", "_n")
        for r in per_station_records:
            v = r["mae"].get(key)
            n = r["mae"].get(n_key, 0) or 0
            if v is None or n == 0:
                continue
            weighted_sum += v * r["weight"]
            weight_total += r["weight"]
            n_total += n
        if weight_total == 0.0:
            return None, n_total
        return round(weighted_sum / weight_total, 1), n_total

    wind_mae, wind_n = _weighted_mean("wind_mae_kt")
    dir_mae, dir_n   = _weighted_mean("dir_mae_deg")
    gust_mae, gust_n = _weighted_mean("gust_mae_kt")
    temp_mae, temp_n = _weighted_mean("temp_mae_c")
    pres_mae, pres_n = _weighted_mean("pressure_mae_hpa")
    rh_mae, rh_n     = _weighted_mean("rh_mae_pct")
    vis_mae, vis_n   = _weighted_mean("vis_mae_sm")

    # Time bounds — earliest and latest observation across contributing stations
    earliest = None
    latest = None
    sample_total = 0
    for r in per_station_records:
        m = r["mae"]
        sample_total += m.get("sample_count", 0) or 0
        e = m.get("earliest_obs_time")
        l = m.get("latest_obs_time")
        if e and (earliest is None or e < earliest):
            earliest = e
        if l and (latest is None or l > latest):
            latest = l

    return {
        "wind_mae_kt": wind_mae,
        "dir_mae_deg": dir_mae,
        "gust_mae_kt": gust_mae,
        "temp_mae_c": temp_mae,
        "pressure_mae_hpa": pres_mae,
        "rh_mae_pct": rh_mae,
        "vis_mae_sm": vis_mae,
        "sample_count": sample_total,
        "wind_n": wind_n, "dir_n": dir_n, "gust_n": gust_n, "temp_n": temp_n,
        "pressure_n": pres_n, "rh_n": rh_n, "vis_n": vis_n,
        "earliest_obs_time": earliest,
        "latest_obs_time": latest,
        # Diagnostic — list of (sid, distance_km, quality_weight, combined_weight) for debugging
        "_stations_used": [
            (r["sid"], round(r["distance_km"], 1),
             round(r.get("quality_weight", 1.0), 2),
             round(r["weight"], 3))
            for r in per_station_records
        ],
    }


# =============================================================================
# METAR HISTORY FETCH (AviationWeather.gov)
# =============================================================================

def fetch_metar_history(icao: str, hours: int = 24) -> list:
    """Fetches the last N hours of METAR from AviationWeather.gov.

    Returns list of dicts with 'time' (datetime), 'wind_kt', 'wind_dir',
    'gust_kt', 'temp_c', 'pressure_hpa'. Fields that weren't reported
    are set to None.
    """
    if not icao or icao == "NONE":
        return []

    # The aviationweather.gov API was modernized in September 2025. The
    # legacy `hoursBeforeNow` parameter still works as an alias but the
    # canonical name is `hours`. Using `hours` ensures we get the full
    # trailing-N-hour history rather than just the most-recent obs.
    url = (
        f"https://aviationweather.gov/api/data/metar"
        f"?ids={icao}&format=json&hours={hours}"
    )
    return _fetch_and_parse_metar(url)


def fetch_metars_in_radius(lat: float, lon: float, radius_km: float = 75.0,
                            hours: int = 24) -> tuple:
    """Fetches the last N hours of METAR for ALL stations within radius_km.

    The aviationweather.gov bbox endpoint returns only the *latest* report
    for each station in the box. To get the full hourly history we do this
    in two phases:
        1. bbox query → list of station IDs in the box (latest record only)
        2. for each station ID → ids=XXXX query with hoursBeforeNow=N to
           pull the trailing-N-hour history

    Returns:
        (observations, station_ids) where station_ids is a sorted list of
        unique ICAO codes that contributed records.
    """
    deg_lat = radius_km / 111.0
    cos_lat = max(0.05, math.cos(math.radians(lat)))
    deg_lon = radius_km / (111.0 * cos_lat)

    min_lat = lat - deg_lat
    max_lat = lat + deg_lat
    min_lon = lon - deg_lon
    max_lon = lon + deg_lon

    # --- Phase 1: discover stations in the bbox ---
    # Note: post-Sept 2025 API uses `hours=N` (was `hoursBeforeNow=N`).
    bbox_url = (
        f"https://aviationweather.gov/api/data/metar"
        f"?bbox={min_lat:.4f},{min_lon:.4f},{max_lat:.4f},{max_lon:.4f}"
        f"&format=json&hours=2"   # only need recent ping for discovery
    )
    discovery = _fetch_and_parse_metar(bbox_url, want_station_id=True)

    # Filter to stations within the great-circle radius and collect IDs
    station_ids = set()
    for obs in discovery:
        s_lat = obs.get("_lat")
        s_lon = obs.get("_lon")
        sid = obs.get("station_id")
        if not sid:
            continue
        if s_lat is None or s_lon is None:
            station_ids.add(sid)
            continue
        try:
            lat1, lat2 = math.radians(lat), math.radians(s_lat)
            dlat = lat2 - lat1
            dlon = math.radians(s_lon - lon)
            a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
            d_km = 2 * 6371.0 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        except Exception:
            d_km = radius_km
        if d_km <= radius_km:
            station_ids.add(sid)

    # Snapshot the bbox-discovered coordinates so we can backfill them onto
    # the per-station history fetch (the ids= endpoint sometimes omits coords)
    station_coords: dict = {}
    for obs in discovery:
        sid = obs.get("station_id")
        if sid and sid in station_ids and obs.get("_lat") is not None:
            station_coords[sid] = (float(obs["_lat"]), float(obs["_lon"]))

    if not station_ids:
        return [], []

    # --- Phase 2: fetch full N-hour history for each station via ids= ---
    # Limit to 15 stations to keep API load and latency reasonable.
    station_list = sorted(station_ids)[:15]
    all_observations = []
    for sid in station_list:
        sid_url = (
            f"https://aviationweather.gov/api/data/metar"
            f"?ids={sid}&format=json&hours={hours}"
        )
        sid_obs = _fetch_and_parse_metar(sid_url, want_station_id=True)
        # Keep _lat/_lon on the obs — needed for distance-weighted per-station
        # MAE scoring downstream. Backfill from bbox discovery if the per-
        # station endpoint didn't return coords.
        for o in sid_obs:
            if o.get("_lat") is None and sid in station_coords:
                o["_lat"], o["_lon"] = station_coords[sid]
            all_observations.append(o)

    return all_observations, station_list


def _fetch_and_parse_metar(url: str, want_station_id: bool = False) -> list:
    """Shared METAR JSON parse logic for single-ICAO and bbox queries."""
    try:
        from modules.http_client import fetch_json as _fetch_json, HttpFetchError as _HttpFetchError
        data = _fetch_json(url, timeout=REQUEST_TIMEOUT_S, retries=2)
    except _HttpFetchError as e:
        logger.warning("METAR fetch failed: %s", e)
        return []

    observations = []
    if not isinstance(data, list):
        return []

    for row in data:
        try:
            # API returns fields like 'obsTime' (unix), 'temp', 'dewp',
            # 'wdir', 'wspd', 'wgst', 'altim' (hPa), 'slp' (hPa), 'visib' (sm)
            obs_time = row.get("obsTime") or row.get("reportTime")
            if obs_time is None:
                continue

            if isinstance(obs_time, (int, float)):
                t = datetime.fromtimestamp(obs_time, tz=timezone.utc)
            elif isinstance(obs_time, str):
                t = datetime.fromisoformat(obs_time.replace("Z", "+00:00"))
            else:
                continue

            def _safe_float(key):
                v = row.get(key)
                if v is None or v == "":
                    return None
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None

            wspd = _safe_float("wspd")   # knots
            wgst = _safe_float("wgst")   # knots

            # Wind direction can be the string "VRB" for variable winds at low
            # speeds — these reports are not directionally meaningful and must
            # be excluded from direction MAE.
            wdir_raw = row.get("wdir")
            if wdir_raw is None or wdir_raw == "" or wdir_raw == "VRB":
                wdir = None
            else:
                try:
                    wdir = float(wdir_raw)
                except (TypeError, ValueError):
                    wdir = None

            temp = _safe_float("temp")   # Celsius
            dewp = _safe_float("dewp")   # Celsius
            altim = _safe_float("altim") # hPa (altimeter setting)
            slp = _safe_float("slp")     # hPa sea level pressure
            visib = _safe_float("visib") # statute miles

            # METAR station pressure isn't always directly available — altim is
            # sea-level-adjusted. For a scorecard use altim as a reasonable
            # approximation at low-elevation airports.
            pressure = altim if altim is not None else slp

            # Compute RH from temp and dewpoint using the August-Roche-Magnus
            # approximation. Both must be present.
            rh = None
            if temp is not None and dewp is not None:
                try:
                    a, b = 17.625, 243.04
                    alpha_t = (a * temp) / (b + temp)
                    alpha_d = (a * dewp) / (b + dewp)
                    rh = 100.0 * math.exp(alpha_d - alpha_t)
                    rh = max(0.0, min(100.0, rh))
                except Exception:
                    rh = None

            record = {
                "time": t,
                "wind_kt": wspd,
                "wind_dir": wdir,
                "gust_kt": wgst,
                "temp_c": temp,
                "pressure_hpa": pressure,
                "rh": rh,
                "visibility_sm": visib,
                "source": "METAR",
            }

            # Capture station identity for the bbox query case
            if want_station_id:
                record["station_id"] = row.get("icaoId") or row.get("stationId")
                # Coordinates needed for post-filtering by great-circle distance
                record["_lat"] = row.get("lat")
                record["_lon"] = row.get("lon")

            observations.append(record)
        except Exception:
            continue

    return observations


# =============================================================================
# KESTREL SESSION FETCH (Supabase)
# =============================================================================

def fetch_kestrel_sessions_24h(sb_client, lat: float, lon: float) -> list:
    """Fetches Kestrel sessions near (lat, lon) from the trailing 24 hours.

    Returns list of dicts matching the METAR observation format so they
    can be merged into the truth set.
    """
    if sb_client is None:
        return []

    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        result = (
            sb_client.table("forecast_verifications")
            .select("timestamp,actual_wind_kt,actual_wind_dir,actual_temp_c,actual_pressure_hpa,actual_rh")
            .gte("timestamp", cutoff)
            .gte("lat", lat - 0.2).lte("lat", lat + 0.2)
            .gte("lon", lon - 0.2).lte("lon", lon + 0.2)
            .execute()
        )
    except Exception as e:
        logger.debug("Kestrel session fetch failed: %s", e)
        return []

    observations = []
    for row in result.data or []:
        try:
            t_str = row.get("timestamp")
            if not t_str:
                continue
            t = datetime.fromisoformat(t_str.replace("Z", "+00:00"))

            observations.append({
                "time": t,
                "wind_kt": row.get("actual_wind_kt"),
                "wind_dir": row.get("actual_wind_dir"),
                "gust_kt": None,  # Kestrel session average doesn't capture instantaneous gusts reliably
                "temp_c": row.get("actual_temp_c"),
                "pressure_hpa": row.get("actual_pressure_hpa"),
                "rh": row.get("actual_rh"),
                "visibility_sm": None,  # Kestrel does not measure visibility
                "source": "KESTREL",
            })
        except Exception:
            continue

    return observations


# =============================================================================
# MESONET / MADIS FETCH (via Synoptic Data API)
# =============================================================================

# Synoptic Data PBC aggregates MADIS plus 320+ other networks. Public data is
# free for non-commercial use with the 'demotoken' or a registered token.
# CANSOFCOM/government use should request a National Mesonet Program token.
SYNOPTIC_TOKEN = "demotoken"   # Override via secrets.toml in production
SYNOPTIC_TIMESERIES_URL = "https://api.synopticdata.com/v2/stations/timeseries"
KM_TO_MILES = 0.621371


def fetch_mesonet_history(
    lat: float,
    lon: float,
    radius_km: float = 75.0,
    hours: int = 24,
    token: str = None,
) -> tuple:
    """Fetches surface obs from MADIS-aggregated networks via Synoptic Data.

    Returns a tuple (observations, status):
        observations: list of obs dicts (same shape as fetch_metar_history)
            with extra fields station_id, network, qc_flag, elevation_m, source.
        status: dict with keys:
            ok           — bool, True if fetch succeeded with data
            message      — human-readable status / error string
            using_demo   — True if demotoken was used (rate-limited)
            http_error   — HTTP status code on failure, else None
            api_status   — Synoptic's own response status if returned

    All QC tiers are returned; the caller decides how to filter for display/scoring.
    """
    tok = token or SYNOPTIC_TOKEN
    using_demo = (tok == "demotoken")
    radius_mi = radius_km * KM_TO_MILES

    status = {
        "ok": False,
        "message": "",
        "using_demo": using_demo,
        "http_error": None,
        "api_status": None,
    }

    # Variables mirror the METAR scorecard set so the merged truth set is uniform
    vars_csv = (
        "air_temp,wind_speed,wind_direction,wind_gust,"
        "relative_humidity,pressure,sea_level_pressure,altimeter,visibility"
    )

    params = {
        "radius": f"{lat:.4f},{lon:.4f},{radius_mi:.0f}",
        "recent": str(hours * 60),    # minutes
        "vars": vars_csv,
        "qc": "on",
        "qc_remove_data": "off",      # we want flagged data + the flags
        "qc_flags": "on",
        "units": "speed|kts,temp|C,pres|mb,height|m",
        "obtimezone": "utc",
        "token": tok,
    }

    qs = "&".join(f"{k}={urllib.parse.quote(str(v), safe=',|')}" for k, v in params.items())
    url = f"{SYNOPTIC_TIMESERIES_URL}?{qs}"

    try:
        from modules.http_client import fetch_json as _fetch_json, HttpFetchError as _HttpFetchError
        payload = _fetch_json(url, timeout=REQUEST_TIMEOUT_S, retries=2)
    except _HttpFetchError as e:
        status["http_error"] = e.status
        if e.status == 401:
            status["message"] = ("Synoptic API rejected the token (HTTP 401). "
                                 "If using demotoken, register at "
                                 "synopticdata.com for a free public token "
                                 "and add it to SECRETS_TOML as [synoptic] token = \"...\".")
        elif e.status == 429:
            status["message"] = ("Synoptic API rate limit hit (HTTP 429). "
                                 "The demotoken has very tight limits; a registered "
                                 "free token allows higher request volumes.")
        elif e.status is not None:
            status["message"] = f"Synoptic API returned HTTP {e.status}."
        else:
            status["message"] = f"Synoptic API unreachable: {e.message}"
        logger.warning("Synoptic fetch failed: %s", status["message"])
        return [], status

    # Check Synoptic's own response status code — it returns 200 even on errors
    # and signals success via a SUMMARY block inside the payload.
    summary = payload.get("SUMMARY", {}) or {}
    api_response_code = summary.get("RESPONSE_CODE")
    api_message = summary.get("RESPONSE_MESSAGE", "")
    status["api_status"] = api_response_code

    if api_response_code is not None and api_response_code != 1:
        # 1 = OK, 2 = zero results found, anything else = error
        status["message"] = f"Synoptic API: {api_message} (code {api_response_code})"
        logger.warning("Synoptic API error: %s", status["message"])
        return [], status

    stations = payload.get("STATION", [])
    if not stations:
        if using_demo:
            status["message"] = ("Synoptic returned zero stations. The demotoken "
                                 "often returns empty payloads outside specific test "
                                 "regions. Register for a free token at "
                                 "synopticdata.com and add it to SECRETS_TOML.")
        else:
            status["message"] = f"No mesonet stations found within {radius_km:.0f} km."
        return [], status

    observations = []

    def _safe_iso(t):
        try:
            return datetime.fromisoformat(t.replace("Z", "+00:00"))
        except Exception:
            return None

    for stn in stations:
        try:
            stid = stn.get("STID", "?")
            network_name = (stn.get("MNET", {}) or {}).get("LONGNAME") or stn.get("MNET_ID", "")
            elev = stn.get("ELEVATION")
            try:
                elev = float(elev) if elev is not None else None
            except (TypeError, ValueError):
                elev = None

            # CWOP stations use network IDs in a known range. The Synoptic
            # network_id "65" historically corresponds to APRSWXNET/CWOP.
            mnet_id_raw = stn.get("MNET_ID")
            try:
                mnet_id = int(mnet_id_raw) if mnet_id_raw is not None else None
            except (TypeError, ValueError):
                mnet_id = None
            is_cwop = (mnet_id == 65) or (
                isinstance(network_name, str) and "CWOP" in network_name.upper()
            )
            source_tag = "CWOP" if is_cwop else "MESONET"

            obs_block = stn.get("OBSERVATIONS", {}) or {}
            qc_block = stn.get("QC", {}) or {}

            times = obs_block.get("date_time", [])
            n = len(times)
            if n == 0:
                continue

            # The variable keys are suffixed with _set_1, _set_2 etc. We use
            # the first available set for each variable.
            def _series(name):
                v = obs_block.get(f"{name}_set_1")
                return v if isinstance(v, list) and len(v) == n else [None] * n

            def _qc_series(name):
                v = qc_block.get(f"{name}_set_1")
                return v if isinstance(v, list) and len(v) == n else [None] * n

            wspd_s = _series("wind_speed")           # kt
            wdir_s = _series("wind_direction")       # deg
            wgst_s = _series("wind_gust")            # kt
            temp_s = _series("air_temp")             # C
            rh_s   = _series("relative_humidity")    # %
            altim_s = _series("altimeter")           # mb (preferred for METAR-comparable pressure)
            slp_s = _series("sea_level_pressure")
            pres_s = _series("pressure")
            vis_s  = _series("visibility")           # statute miles per Synoptic default

            wspd_qc = _qc_series("wind_speed")
            temp_qc = _qc_series("air_temp")

            for i in range(n):
                t = _safe_iso(times[i])
                if t is None:
                    continue

                # Pick the best pressure available
                pressure = altim_s[i] if altim_s[i] is not None else (
                    slp_s[i] if slp_s[i] is not None else pres_s[i]
                )

                # Aggregate QC: any per-variable QC flag presence is recorded
                qc_status = None
                if wspd_qc[i] or temp_qc[i]:
                    qc_status = "FLAGGED"
                else:
                    qc_status = "PASS"

                observations.append({
                    "time": t,
                    "wind_kt": wspd_s[i],
                    "wind_dir": wdir_s[i],
                    "gust_kt": wgst_s[i],
                    "temp_c": temp_s[i],
                    "pressure_hpa": pressure,
                    "rh": rh_s[i],
                    "visibility_sm": vis_s[i],
                    "station_id": stid,
                    "network": network_name,
                    "qc_flag": qc_status,
                    "elevation_m": elev,
                    "source": source_tag,
                })
        except Exception as e:
            logger.debug("Mesonet station %s parse failed: %s", stn.get("STID"), e)
            continue

    status["ok"] = True
    status["message"] = f"{len(observations)} obs from {len(stations)} stations."
    return observations, status


# =============================================================================
# PAIRING & MAE COMPUTATION
# =============================================================================

def _match_forecast_to_observation(obs_time: datetime, fcst_times: list) -> int:
    """Returns the index of the forecast hour nearest to obs_time,
    or -1 if no match within 45 minutes.
    """
    if not fcst_times:
        return -1

    obs_ts = obs_time.timestamp()
    best_idx = -1
    best_diff = float("inf")

    for i, t_str in enumerate(fcst_times):
        try:
            t = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        diff = abs(t.timestamp() - obs_ts)
        if diff < best_diff:
            best_diff = diff
            best_idx = i

    # Reject matches more than 45 minutes away — METAR hourly cadence means
    # we expect exact hour matches
    if best_diff > 2700:
        return -1

    return best_idx


def compute_model_mae(model_history: dict, observations: list) -> dict:
    """Computes mean absolute error per variable for one model.

    Args:
        model_history: output of _fetch_model_history
        observations: list of observation dicts (METAR + Kestrel combined)

    Returns:
        dict with MAE per variable, sample counts, and time bounds of the
        paired observations actually used.
    """
    result = {
        "wind_mae_kt": None,
        "dir_mae_deg": None,
        "gust_mae_kt": None,
        "temp_mae_c": None,
        "pressure_mae_hpa": None,
        "rh_mae_pct": None,
        "vis_mae_sm": None,
        "sample_count": 0,
        "wind_n": 0, "dir_n": 0, "gust_n": 0, "temp_n": 0,
        "pressure_n": 0, "rh_n": 0, "vis_n": 0,
        "earliest_obs_time": None,
        "latest_obs_time": None,
    }

    if not model_history or not observations:
        return result

    wind_errs, dir_errs, gust_errs = [], [], []
    temp_errs, pressure_errs = [], []
    rh_errs, vis_errs = [], []
    matched_times = []

    def _shortest_arc(a: float, b: float) -> float:
        """Shortest absolute angular distance between two bearings."""
        d = abs(((a - b) + 180) % 360 - 180)
        return d

    for obs in observations:
        idx = _match_forecast_to_observation(obs["time"], model_history["times"])
        if idx < 0:
            continue

        matched_times.append(obs["time"])

        # Wind speed
        fw = model_history["wind_kt"][idx] if idx < len(model_history["wind_kt"]) else None
        ow = obs.get("wind_kt")
        if fw is not None and ow is not None:
            wind_errs.append(abs(fw - ow))

        # Wind direction (shortest-arc; only meaningful when wind is non-trivial)
        fd = model_history["wind_dir"][idx] if idx < len(model_history["wind_dir"]) else None
        od = obs.get("wind_dir")
        # Skip direction comparison for calm/light winds where direction is
        # poorly defined (METAR uses VRB at low speeds; we already null those,
        # but also exclude observations with reported wind speed < 3 kt)
        if fd is not None and od is not None and (ow is None or ow >= 3.0):
            dir_errs.append(_shortest_arc(fd, od))

        # Gust
        fg = model_history["gust_kt"][idx] if idx < len(model_history["gust_kt"]) else None
        og = obs.get("gust_kt")
        if fg is not None and og is not None:
            gust_errs.append(abs(fg - og))

        # Temp
        ft = model_history["temp_c"][idx] if idx < len(model_history["temp_c"]) else None
        ot = obs.get("temp_c")
        if ft is not None and ot is not None:
            temp_errs.append(abs(ft - ot))

        # Pressure
        fp = model_history["pressure_hpa"][idx] if idx < len(model_history["pressure_hpa"]) else None
        op = obs.get("pressure_hpa")
        if fp is not None and op is not None:
            pressure_errs.append(abs(fp - op))

        # RH
        frh = model_history["rh"][idx] if idx < len(model_history["rh"]) else None
        orh = obs.get("rh")
        if frh is not None and orh is not None:
            rh_errs.append(abs(frh - orh))

        # Visibility (statute miles; capped at 10 sm because METAR reports >10 sm
        # as "10+" and the model values can be very high in clear conditions —
        # capping prevents runaway error from a single near-perfect observation)
        fv = model_history["visibility_sm"][idx] if idx < len(model_history["visibility_sm"]) else None
        ov = obs.get("visibility_sm")
        if fv is not None and ov is not None:
            fv_capped = min(fv, 10.0)
            ov_capped = min(ov, 10.0)
            vis_errs.append(abs(fv_capped - ov_capped))

    def _mae(errs):
        return round(sum(errs) / len(errs), 1) if errs else None

    result["wind_mae_kt"] = _mae(wind_errs)
    result["dir_mae_deg"] = _mae(dir_errs)
    result["gust_mae_kt"] = _mae(gust_errs)
    result["temp_mae_c"] = _mae(temp_errs)
    result["pressure_mae_hpa"] = _mae(pressure_errs)
    result["rh_mae_pct"] = _mae(rh_errs)
    result["vis_mae_sm"] = _mae(vis_errs)
    result["sample_count"] = len(observations)
    result["wind_n"] = len(wind_errs)
    result["dir_n"] = len(dir_errs)
    result["gust_n"] = len(gust_errs)
    result["temp_n"] = len(temp_errs)
    result["pressure_n"] = len(pressure_errs)
    result["rh_n"] = len(rh_errs)
    result["vis_n"] = len(vis_errs)

    if matched_times:
        result["earliest_obs_time"] = min(matched_times)
        result["latest_obs_time"] = max(matched_times)

    return result


def compute_model_pairings(model_history: dict, observations: list) -> list:
    """Returns per-hour paired (forecast, observation) error records for one model.

    Used as input to compute_rolling_mae for trend computation.

    Returns a list of dicts, one per paired hour, each containing:
        time           — datetime (UTC) of the observation
        wind_err_kt    — |fcst - obs| or None
        dir_err_deg    — shortest-arc absolute error or None (excludes light winds)
        gust_err_kt    — or None
        temp_err_c     — or None
        pressure_err_hpa — or None
        rh_err_pct     — or None
        vis_err_sm     — or None
    """
    if not model_history or not observations:
        return []

    def _shortest_arc(a, b):
        return abs(((a - b) + 180) % 360 - 180)

    pairings = []
    for obs in observations:
        idx = _match_forecast_to_observation(obs["time"], model_history["times"])
        if idx < 0:
            continue

        rec = {"time": obs["time"]}

        # Wind speed
        fw = model_history["wind_kt"][idx] if idx < len(model_history["wind_kt"]) else None
        ow = obs.get("wind_kt")
        rec["wind_err_kt"] = abs(fw - ow) if (fw is not None and ow is not None) else None

        # Direction (skip when wind is too light)
        fd = model_history["wind_dir"][idx] if idx < len(model_history["wind_dir"]) else None
        od = obs.get("wind_dir")
        if fd is not None and od is not None and (ow is None or ow >= 3.0):
            rec["dir_err_deg"] = _shortest_arc(fd, od)
        else:
            rec["dir_err_deg"] = None

        # Gust
        fg = model_history["gust_kt"][idx] if idx < len(model_history["gust_kt"]) else None
        og = obs.get("gust_kt")
        rec["gust_err_kt"] = abs(fg - og) if (fg is not None and og is not None) else None

        # Temp
        ft = model_history["temp_c"][idx] if idx < len(model_history["temp_c"]) else None
        ot = obs.get("temp_c")
        rec["temp_err_c"] = abs(ft - ot) if (ft is not None and ot is not None) else None

        # Pressure
        fp = model_history["pressure_hpa"][idx] if idx < len(model_history["pressure_hpa"]) else None
        op = obs.get("pressure_hpa")
        rec["pressure_err_hpa"] = abs(fp - op) if (fp is not None and op is not None) else None

        # RH
        frh = model_history["rh"][idx] if idx < len(model_history["rh"]) else None
        orh = obs.get("rh")
        rec["rh_err_pct"] = abs(frh - orh) if (frh is not None and orh is not None) else None

        # Visibility (capped at 10 sm both sides)
        fv = model_history["visibility_sm"][idx] if idx < len(model_history["visibility_sm"]) else None
        ov = obs.get("visibility_sm")
        if fv is not None and ov is not None:
            rec["vis_err_sm"] = abs(min(fv, 10.0) - min(ov, 10.0))
        else:
            rec["vis_err_sm"] = None

        pairings.append(rec)

    return pairings


def compute_rolling_mae(
    pairings: list,
    window_hours: int = 6,
    step_hours: int = 1,
    span_hours: int = 24,
) -> dict:
    """Computes a sliding-window MAE timeseries for trend visualization.

    Walks a `window_hours`-wide window across the trailing `span_hours` of
    pairings, stepping by `step_hours`. Each step produces one MAE point per
    variable using all pairings whose observation time falls in the window.

    Returns:
        dict with keys:
            window_centers  — list of datetime (UTC) at each window centre
            wind_mae_kt     — list of MAE values, same length as window_centers
            dir_mae_deg     — same
            gust_mae_kt     — same
            temp_mae_c      — same
            pressure_mae_hpa — same
            rh_mae_pct      — same
            vis_mae_sm      — same
        Missing windows (no pairings) get None.
    """
    out = {
        "window_centers": [],
        "wind_mae_kt": [],
        "dir_mae_deg": [],
        "gust_mae_kt": [],
        "temp_mae_c": [],
        "pressure_mae_hpa": [],
        "rh_mae_pct": [],
        "vis_mae_sm": [],
    }

    if not pairings:
        return out

    times = [p["time"] for p in pairings]
    end_time = max(times)
    start_time = end_time - timedelta(hours=span_hours)

    half_w = timedelta(hours=window_hours / 2.0)
    step = timedelta(hours=step_hours)

    centre = start_time + half_w
    while centre <= end_time:
        w_lo = centre - half_w
        w_hi = centre + half_w

        window_pairs = [p for p in pairings if w_lo <= p["time"] <= w_hi]

        def _wmae(key):
            errs = [p[key] for p in window_pairs if p.get(key) is not None]
            return round(sum(errs) / len(errs), 2) if errs else None

        out["window_centers"].append(centre)
        out["wind_mae_kt"].append(_wmae("wind_err_kt"))
        out["dir_mae_deg"].append(_wmae("dir_err_deg"))
        out["gust_mae_kt"].append(_wmae("gust_err_kt"))
        out["temp_mae_c"].append(_wmae("temp_err_c"))
        out["pressure_mae_hpa"].append(_wmae("pressure_err_hpa"))
        out["rh_mae_pct"].append(_wmae("rh_err_pct"))
        out["vis_mae_sm"].append(_wmae("vis_err_sm"))

        centre += step

    return out


def _composite_score(mae_dict: dict) -> float:
    """Computes a weighted composite error score for ranking.

    Lower is better. Weights reflect operational impact for UAS operations:
        wind     × 3.0  (primary hazard)
        gust     × 2.0  (excursion-driver)
        dir      × 0.05 (per degree, capped influence)
        temp     × 1.0
        pressure × 0.5
        rh       × 0.05 (per percent)
        vis      × 0.5  (per statute mile)

    Returns infinity if no wind MAE is available (model can't be ranked).
    """
    w = mae_dict.get("wind_mae_kt")
    if w is None:
        return float("inf")

    score = w * 3.0  # wind weighted x3

    g = mae_dict.get("gust_mae_kt")
    if g is not None:
        score += g * 2.0

    d = mae_dict.get("dir_mae_deg")
    if d is not None:
        score += d * 0.05

    t = mae_dict.get("temp_mae_c")
    if t is not None:
        score += t * 1.0

    p = mae_dict.get("pressure_mae_hpa")
    if p is not None:
        score += p * 0.5

    rh = mae_dict.get("rh_mae_pct")
    if rh is not None:
        score += rh * 0.05

    v = mae_dict.get("vis_mae_sm")
    if v is not None:
        score += v * 0.5

    return score


# =============================================================================
# TOP-LEVEL ORCHESTRATION
# =============================================================================

def compute_performance_scorecard(
    lat: float,
    lon: float,
    icao: str,
    sb_client=None,
    synoptic_token: str = None,
    mesonet_radius_km: float = 75.0,
) -> dict:
    """Produces the complete performance scorecard for all active models.

    Args:
        lat, lon:           site coordinates (used to select regional model)
        icao:               nearest ICAO for METAR history (can be "NONE")
        sb_client:          optional Supabase client for Kestrel data
        synoptic_token:     optional Synoptic API token (defaults to demotoken)
        mesonet_radius_km:  search radius for MADIS/mesonet stations (default 75)

    Returns:
        dict with:
          - models:            list of per-model results
                                each model entry now includes 'rolling' (trend dict)
          - best_performer:    name of the lowest-error model
          - observation_count: total observations used in scoring
          - metar_count:       distinct METAR records contributing
          - mesonet_count:     distinct MADIS/Synoptic mesonet records contributing
          - cwop_count:        subset of mesonet records flagged as CWOP
          - mesonet_stations:  list of unique station IDs that contributed
          - kestrel_count:     how many Kestrel sessions contributed
          - has_data:          True if scoring was possible
    """
    # Fetch observations from all three sources.
    # METAR: pull every reporting station in the same 75km radius, not just the
    # nearest one. This typically gives 3-15 independent METAR truth points
    # instead of one — much more statistically meaningful.
    metar_obs, metar_station_ids = fetch_metars_in_radius(
        lat, lon, radius_km=mesonet_radius_km, hours=24,
    )

    # mix-obs fallback: if AviationWeather.gov returned nothing (typically
    # at non-airport sites or outside Canada/CONUS), try Meteomatics' global
    # station observation feed. Same observation shape so the scorer doesn't
    # care which source produced the data.
    mix_obs_used = False
    if not metar_obs:
        try:
            from modules.meteomatics_provider import (
                fetch_meteomatics_station_obs, has_credentials as _mm_has,
            )
            from modules.ensemble_analysis import _nearest_icao_for_mos
            if _mm_has():
                icao = _nearest_icao_for_mos(lat, lon)
                if icao:
                    fallback_station = f"metar_{icao}"
                    metar_obs = fetch_meteomatics_station_obs(fallback_station, hours_back=24)
                    if metar_obs:
                        metar_station_ids = [icao]
                        mix_obs_used = True
                        logger.info("AviationWeather METAR empty; using mix-obs fallback for %s", icao)
        except Exception as e:
            logger.info("mix-obs fallback failed: %s", e)

    mesonet_obs, mesonet_status = fetch_mesonet_history(
        lat, lon,
        radius_km=mesonet_radius_km,
        hours=24,
        token=synoptic_token,
    )
    kestrel_obs = fetch_kestrel_sessions_24h(sb_client, lat, lon) if sb_client else []

    # Augment with non-METAR stations from Meteomatics' find_station catalog.
    # Discovers WMO synoptic stations, marine buoys, and cooperative observer
    # sites that AviationWeather.gov doesn't surface. Each station's
    # observations are tagged with a quality_weight (per category) that
    # multiplies the distance weight in MAE aggregation. Default on; the
    # cost is +1 quota unit for the catalog query plus ~10 quota units per
    # non-overlapping station for the mix-obs history fetch.
    nonmetar_obs = []
    nonmetar_station_records = []   # for diagnostics + scorecard footer
    # Diagnostic status — surfaced to the dashboard footer so operators can
    # see what happened. Possible states:
    #   not_attempted    — Meteomatics credentials missing
    #   no_credentials   — Meteomatics module not importable
    #   catalog_empty    — find_station returned no stations in radius
    #   all_redundant    — catalog returned stations but all overlap METAR
    #   ok               — N non-METAR stations added
    #   error            — exception during discovery/fetch
    find_station_status = {
        "attempted": False,
        "state": "not_attempted",
        "catalog_size": 0,
        "new_stations": 0,
        "obs_added": 0,
        "message": "",
    }
    try:
        from modules.meteomatics_provider import (
            fetch_meteomatics_find_station,
            fetch_meteomatics_station_obs,
            has_credentials as _mm_has,
        )
        if _mm_has():
            find_station_status["attempted"] = True
            existing_sids = set(metar_station_ids)
            catalog = fetch_meteomatics_find_station(
                lat, lon, radius_km=mesonet_radius_km, source="mix-obs", limit=15
            )
            find_station_status["catalog_size"] = len(catalog)

            if not catalog:
                find_station_status["state"] = "catalog_empty"
                find_station_status["message"] = (
                    "Meteomatics find_station returned no stations in radius. "
                    "Non-METAR network coverage is sparse in this region."
                )
            else:
                # Filter to stations NOT already covered by METAR — no point
                # double-counting CYTR if AviationWeather already returned it.
                new_stations = []
                for s in catalog:
                    bare_id = s["station_id"].replace("metar_", "").replace("wmo_", "")
                    if bare_id in existing_sids:
                        continue
                    new_stations.append(s)

                # Cap at a reasonable number to bound quota — we already have
                # METAR coverage from AviationWeather; this is supplementary.
                new_stations = new_stations[:8]
                find_station_status["new_stations"] = len(new_stations)

                if not new_stations:
                    find_station_status["state"] = "all_redundant"
                    find_station_status["message"] = (
                        f"Meteomatics returned {len(catalog)} stations but all "
                        "overlap with AviationWeather METAR coverage."
                    )
                else:
                    from concurrent.futures import ThreadPoolExecutor, as_completed
                    with ThreadPoolExecutor(max_workers=len(new_stations)) as ex:
                        futures = {
                            ex.submit(fetch_meteomatics_station_obs,
                                      s["station_id"], 24): s
                            for s in new_stations
                        }
                        for fut in as_completed(futures):
                            s = futures[fut]
                            try:
                                obs = fut.result()
                            except Exception as e:
                                logger.info("Non-METAR obs fetch failed for %s: %s",
                                            s["station_id"], e)
                                continue
                            if not obs:
                                continue
                            for o in obs:
                                o["_lat"] = s["lat"]
                                o["_lon"] = s["lon"]
                                o["_quality_weight"] = s["quality_weight"]
                                o["_station_category"] = s["category"]
                                o["station_id"] = s["station_id"]
                            nonmetar_obs.extend(obs)
                            nonmetar_station_records.append(s)
                    find_station_status["obs_added"] = len(nonmetar_obs)
                    find_station_status["state"] = "ok"
        else:
            find_station_status["state"] = "no_credentials"
    except ImportError as e:
        find_station_status["state"] = "no_credentials"
        find_station_status["message"] = f"Module unavailable: {e}"
    except Exception as e:
        find_station_status["state"] = "error"
        find_station_status["message"] = f"{type(e).__name__}: {e}"
        logger.info("find_station augmentation failed: %s", e)

    all_observations = metar_obs + mesonet_obs + kestrel_obs + nonmetar_obs

    # Mesonet station summary for the dashboard's source list
    mesonet_station_ids = set()
    cwop_count = 0
    for o in mesonet_obs:
        sid = o.get("station_id")
        if sid:
            mesonet_station_ids.add(sid)
        if o.get("source") == "CWOP":
            cwop_count += 1

    if not all_observations:
        return {
            "models": [],
            "best_performer": None,
            "observation_count": 0,
            "metar_count": 0,
            "metar_stations": [],
            "mesonet_count": 0,
            "cwop_count": 0,
            "mesonet_stations": [],
            "mesonet_status": mesonet_status,
            "kestrel_count": 0,
            "has_data": False,
            "message": "No surface observations available in the last 24 hours within the search radius.",
        }

    # Determine which models to score. Each gets a coverage flag — out-of-
    # coverage models still appear in the scorecard with an OUT_OF_COVERAGE
    # status so the operator can see which models aren't available and why.
    regional_name, regional_url = _select_regional_model(lat, lon)
    in_conus = _is_conus_coverage(lat, lon)

    # Check if Meteomatics credentials are present so we can include
    # Meteomatics-only models (MIX, AIFS) in scoring.
    try:
        from modules.meteomatics_provider import has_credentials as _mm_has
        _mm_in_scorecard = _mm_has()
    except ImportError:
        _mm_in_scorecard = False

    # The scorecard model list. URLs starting with "meteomatics://" are
    # dispatched to _fetch_model_history_meteomatics; everything else uses
    # the Open-Meteo fetcher. MODEL_ENDPOINTS now returns the best-available
    # source for each model name (Meteomatics where the subscription has it,
    # Open-Meteo otherwise) via the source-aware routing in ensemble_analysis.
    all_candidate_models = [
        # (display_name, endpoint_url_or_marker, in_coverage)
        (regional_name, regional_url, True),  # always in coverage by definition
        ("GFS",   MODEL_ENDPOINTS["GFS"],   True),
        ("ECMWF", MODEL_ENDPOINTS["ECMWF"], True),
        ("ICON",  MODEL_ENDPOINTS["ICON"],  True),
        ("NAM",   MODEL_ENDPOINTS["NAM"],   in_conus),
        ("HRRR",  MODEL_ENDPOINTS["HRRR"],  in_conus),
    ]
    # Meteomatics-only models — surface only when credentials configured
    if _mm_in_scorecard:
        if "MIX" in MODEL_ENDPOINTS:
            all_candidate_models.append(("MIX", MODEL_ENDPOINTS["MIX"], True))
        if "AIFS" in MODEL_ENDPOINTS:
            all_candidate_models.append(("AIFS", MODEL_ENDPOINTS["AIFS"], True))
        if "MOS" in MODEL_ENDPOINTS:
            # MOS coverage depends on whether there's an ICAO station near
            # the query point. The ensemble logic does the lookup; we mirror
            # it here so the scorecard only attempts MOS where it can succeed.
            from modules.ensemble_analysis import _nearest_icao_for_mos
            _mos_icao = _nearest_icao_for_mos(lat, lon)
            all_candidate_models.append(("MOS", MODEL_ENDPOINTS["MOS"], bool(_mos_icao)))

    def _empty_record(name: str, status: str) -> dict:
        return {
            "name": name,
            "status": status,
            "wind_mae_kt": None, "dir_mae_deg": None,
            "gust_mae_kt": None, "temp_mae_c": None,
            "pressure_mae_hpa": None, "rh_mae_pct": None,
            "vis_mae_sm": None,
            "sample_count": 0,
            "wind_n": 0, "dir_n": 0, "gust_n": 0,
            "temp_n": 0, "pressure_n": 0, "rh_n": 0, "vis_n": 0,
            "earliest_obs_time": None, "latest_obs_time": None,
            "composite_score": float("inf"),
            "rolling": None,
        }

    # Phase 1 — Discover the unique METAR stations contributing observations,
    # with coordinates. The verification scorer fetches each model's forecast
    # at every station's location independently, then aggregates per-station
    # MAEs via the distance-weighted top-hat + exponential method (see
    # _distance_weight). This is the statistically professional approach:
    # forecast and observation share a coordinate per pairing, eliminating
    # the spatial mismatch in simpler "single forecast at site" methods.
    obs_by_station = _group_observations_by_station(all_observations)
    stations_for_verification: list = []   # list of (sid, lat, lon)
    for sid, obs in obs_by_station.items():
        if not obs:
            continue
        slat = obs[0].get("_lat")
        slon = obs[0].get("_lon")
        if slat is None or slon is None:
            continue
        stations_for_verification.append((sid, float(slat), float(slon)))

    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Resolve nearest ICAO once for MOS dispatch (lookup is cached). MOS is
    # station-keyed natively so it gets a single fetch keyed on the nearest
    # ICAO regardless of how many METARs are in the radius — distance
    # weighting doesn't apply because there's only one MOS point per query.
    _mos_station_id = ""
    if "MOS" in MODEL_ENDPOINTS:
        from modules.ensemble_analysis import _nearest_icao_for_mos
        _icao = _nearest_icao_for_mos(lat, lon)
        if _icao:
            _mos_station_id = f"metar_{_icao}"

    def _dispatch_mos_only(name: str, url: str):
        """MOS is the exception — single-point fetch since it's already
        station-tuned. The aggregation is degenerate (single station)."""
        if not _mos_station_id:
            return None
        return _fetch_model_history_meteomatics_mos(
            _mos_station_id, lat, lon, display_name=name
        )

    in_coverage_models = [
        (name, url) for name, url, in_coverage in all_candidate_models if in_coverage
    ]

    # Two dispatch tables — per-station for gridded models, single-fetch for
    # MOS. Each runs in parallel against its own thread pool.
    per_station_jobs = [
        (name, url) for name, url in in_coverage_models
        if not url.startswith("meteomatics-mos://")
    ]
    single_fetch_jobs = [
        (name, url) for name, url in in_coverage_models
        if url.startswith("meteomatics-mos://")
    ]

    # Phase 1a — per-station fetches for all gridded models
    per_station_histories: dict = {}   # {model_name: {station_id: history}}
    if per_station_jobs and stations_for_verification:
        with ThreadPoolExecutor(max_workers=len(per_station_jobs)) as ex:
            futures = {
                ex.submit(
                    _fetch_model_history_at_stations,
                    name, url, stations_for_verification
                ): name
                for name, url in per_station_jobs
            }
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    per_station_histories[name] = fut.result()
                except Exception as e:
                    logger.warning("Per-station worker crashed for %s: %s", name, e)
                    per_station_histories[name] = {}

    # Phase 1b — single-fetch for MOS (parallel only if we ever have more
    # than one MOS-style single-point model later)
    single_fetch_histories: dict = {}
    if single_fetch_jobs:
        with ThreadPoolExecutor(max_workers=max(1, len(single_fetch_jobs))) as ex:
            futures = {
                ex.submit(_dispatch_mos_only, name, url): name
                for name, url in single_fetch_jobs
            }
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    single_fetch_histories[name] = fut.result()
                except Exception as e:
                    logger.warning("Single-fetch worker crashed for %s: %s", name, e)
                    single_fetch_histories[name] = None

    model_results = []
    for name, url, in_coverage in all_candidate_models:
        if not in_coverage:
            model_results.append(_empty_record(name, "OUT_OF_COVERAGE"))
            continue

        # MOS path — single-point history, score the single station against
        # its own obs (degenerate "weighting" — one station gets weight 1.0)
        if url.startswith("meteomatics-mos://"):
            history = single_fetch_histories.get(name)
            if history is None:
                model_results.append(_empty_record(name, "UNAVAILABLE"))
                continue
            # Use only the MOS station's observations for scoring
            mos_obs = obs_by_station.get(_mos_station_id.replace("metar_", ""), [])
            if not mos_obs:
                # Fallback — score against all observations if we can't find
                # a matching METAR. Less precise but better than nothing.
                mae = compute_model_mae(history, all_observations)
            else:
                mae = compute_model_mae(history, mos_obs)
            mae["name"] = name
            mae["status"] = "OK"
            mae["composite_score"] = _composite_score(mae)
            pairings = compute_model_pairings(history, mos_obs or all_observations)
            mae["rolling"] = compute_rolling_mae(
                pairings, window_hours=6, step_hours=1, span_hours=24
            )
            model_results.append(mae)
            continue

        # Gridded model path — per-station weighted aggregation
        station_hist = per_station_histories.get(name) or {}
        if not station_hist:
            model_results.append(_empty_record(name, "UNAVAILABLE"))
            continue

        mae = compute_weighted_model_mae(
            station_hist, obs_by_station, lat, lon
        )
        if mae["sample_count"] == 0:
            model_results.append(_empty_record(name, "UNAVAILABLE"))
            continue

        mae["name"] = name
        mae["status"] = "OK"
        mae["composite_score"] = _composite_score(mae)

        # Rolling-MAE trend — uses the station closest to the site as a
        # representative timeseries (so the trend line is interpretable as
        # "the model at the most-representative ground truth"). Far stations
        # don't contribute to the rolling trend even though they contribute
        # to the aggregated MAE — this is a deliberate choice to keep the
        # trend visualization clean.
        if stations_for_verification:
            closest_sid = min(
                stations_for_verification,
                key=lambda t: _haversine_km(lat, lon, t[1], t[2])
            )[0]
            closest_history = station_hist.get(closest_sid)
            closest_obs = obs_by_station.get(closest_sid) or []
            if closest_history is not None and closest_obs:
                pairings = compute_model_pairings(closest_history, closest_obs)
                mae["rolling"] = compute_rolling_mae(
                    pairings, window_hours=6, step_hours=1, span_hours=24
                )
            else:
                mae["rolling"] = None
        else:
            mae["rolling"] = None

        model_results.append(mae)

    # Identify the best performer (lowest composite score)
    scorable = [m for m in model_results if m.get("composite_score", float("inf")) < float("inf")]
    best = min(scorable, key=lambda m: m["composite_score"])["name"] if scorable else None

    # Compute the actual evaluation window from matched observations
    all_starts = [m.get("earliest_obs_time") for m in model_results if m.get("earliest_obs_time")]
    all_ends = [m.get("latest_obs_time") for m in model_results if m.get("latest_obs_time")]
    window_start = min(all_starts) if all_starts else None
    window_end = max(all_ends) if all_ends else None

    return {
        "models": model_results,
        "best_performer": best,
        "observation_count": len(all_observations),
        "metar_count": len(metar_obs),
        "metar_stations": metar_station_ids,
        "mesonet_count": len(mesonet_obs),
        "cwop_count": cwop_count,
        "mesonet_stations": sorted(mesonet_station_ids),
        "mesonet_status": mesonet_status,
        "kestrel_count": len(kestrel_obs),
        "nonmetar_count": len(nonmetar_obs),
        "nonmetar_stations": [
            {"id": s["station_id"], "name": s["name"], "category": s["category"],
             "distance_km": round(s["distance_km"], 1)}
            for s in nonmetar_station_records
        ],
        "find_station_status": find_station_status,
        "window_start_utc": window_start,
        "window_end_utc": window_end,
        "has_data": True,
    }


# =============================================================================
# DISPLAY HELPERS
# =============================================================================

def grade_wind_mae(mae: float) -> str:
    """Returns 'GOOD', 'WARN', or 'POOR' for wind MAE."""
    if mae is None: return "NONE"
    if mae <= WIND_MAE_GOOD_KT: return "GOOD"
    if mae <= WIND_MAE_WARN_KT: return "WARN"
    return "POOR"


def grade_gust_mae(mae: float) -> str:
    if mae is None: return "NONE"
    if mae <= GUST_MAE_GOOD_KT: return "GOOD"
    if mae <= GUST_MAE_WARN_KT: return "WARN"
    return "POOR"


def grade_temp_mae(mae: float) -> str:
    if mae is None: return "NONE"
    if mae <= TEMP_MAE_GOOD_C: return "GOOD"
    if mae <= TEMP_MAE_WARN_C: return "WARN"
    return "POOR"


def grade_pressure_mae(mae: float) -> str:
    if mae is None: return "NONE"
    if mae <= PRESSURE_MAE_GOOD_HPA: return "GOOD"
    if mae <= PRESSURE_MAE_WARN_HPA: return "WARN"
    return "POOR"


def grade_dir_mae(mae: float) -> str:
    if mae is None: return "NONE"
    if mae <= DIR_MAE_GOOD_DEG: return "GOOD"
    if mae <= DIR_MAE_WARN_DEG: return "WARN"
    return "POOR"


def grade_rh_mae(mae: float) -> str:
    if mae is None: return "NONE"
    if mae <= RH_MAE_GOOD_PCT: return "GOOD"
    if mae <= RH_MAE_WARN_PCT: return "WARN"
    return "POOR"


def grade_vis_mae(mae: float) -> str:
    if mae is None: return "NONE"
    if mae <= VIS_MAE_GOOD_SM: return "GOOD"
    if mae <= VIS_MAE_WARN_SM: return "WARN"
    return "POOR"


GRADE_COLORS = {
    "GOOD": "#4ade80",
    "WARN": "#E58E26",
    "POOR": "#ff6b4a",
    "NONE": "#6B7280",
}
