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
    "temperature_2m,surface_pressure,relative_humidity_2m,visibility"
)

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
    )

    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
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

    return {
        "times": [times_iso[i] for i in kept_indices],
        "wind_kt": _pick("wind_speed_10m", KMH_TO_KT),
        "wind_dir": _pick("wind_direction_10m"),
        "gust_kt": _pick("wind_gusts_10m", KMH_TO_KT),
        "temp_c": _pick("temperature_2m"),
        "pressure_hpa": _pick("surface_pressure"),
        "rh": _pick("relative_humidity_2m"),
        # Open-Meteo returns visibility in meters; convert to statute miles
        # to match METAR's vsby field. Some endpoints don't include this.
        "visibility_sm": _pick("visibility", 1.0 / 1609.344),
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

    url = (
        f"https://aviationweather.gov/api/data/metar"
        f"?ids={icao}&format=json&hoursBeforeNow={hours}"
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
    bbox_url = (
        f"https://aviationweather.gov/api/data/metar"
        f"?bbox={min_lat:.4f},{min_lon:.4f},{max_lat:.4f},{max_lon:.4f}"
        f"&format=json&hoursBeforeNow=2"   # only need recent ping for discovery
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

    if not station_ids:
        return [], []

    # --- Phase 2: fetch full N-hour history for each station via ids= ---
    # Limit to 15 stations to keep API load and latency reasonable.
    station_list = sorted(station_ids)[:15]
    all_observations = []
    for sid in station_list:
        sid_url = (
            f"https://aviationweather.gov/api/data/metar"
            f"?ids={sid}&format=json&hoursBeforeNow={hours}"
        )
        sid_obs = _fetch_and_parse_metar(sid_url, want_station_id=True)
        for o in sid_obs:
            o.pop("_lat", None)
            o.pop("_lon", None)
            all_observations.append(o)

    return all_observations, station_list


def _fetch_and_parse_metar(url: str, want_station_id: bool = False) -> list:
    """Shared METAR JSON parse logic for single-ICAO and bbox queries."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
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
) -> list:
    """Fetches surface obs from MADIS-aggregated networks via Synoptic Data.

    Returns a list of observation dicts in the same shape as fetch_metar_history,
    with extra fields:
        station_id  (str)         — Synoptic STID
        network     (str)         — provider network code/name
        qc_flag     (str|None)    — Synoptic QC outcome ("PASS"/"FAIL"/None)
        elevation_m (float|None)  — station elevation if known
        source      ("MESONET"|"CWOP")  — distinguishes professional from citizen

    All QC tiers are returned; the caller decides how to filter for display/scoring.

    Args:
        lat, lon:    centre point
        radius_km:   search radius in kilometres
        hours:       trailing window
        token:       Synoptic API token (falls back to demotoken)
    """
    tok = token or SYNOPTIC_TOKEN
    radius_mi = radius_km * KM_TO_MILES

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
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning("Synoptic mesonet fetch failed: %s", e)
        return []

    stations = payload.get("STATION", [])
    if not stations:
        return []

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

    return observations


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
    mesonet_obs = fetch_mesonet_history(
        lat, lon,
        radius_km=mesonet_radius_km,
        hours=24,
        token=synoptic_token,
    )
    kestrel_obs = fetch_kestrel_sessions_24h(sb_client, lat, lon) if sb_client else []

    all_observations = metar_obs + mesonet_obs + kestrel_obs

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
            "kestrel_count": 0,
            "has_data": False,
            "message": "No surface observations available in the last 24 hours within the search radius.",
        }

    # Determine which models to score. Each gets a coverage flag — out-of-
    # coverage models still appear in the scorecard with an OUT_OF_COVERAGE
    # status so the operator can see which models aren't available and why.
    regional_name, regional_url = _select_regional_model(lat, lon)
    in_conus = _is_conus_coverage(lat, lon)

    all_candidate_models = [
        # (display_name, endpoint_url, in_coverage)
        (regional_name, regional_url, True),  # always in coverage by definition
        ("GFS",   MODEL_ENDPOINTS["GFS"],   True),
        ("ECMWF", MODEL_ENDPOINTS["ECMWF"], True),
        ("ICON",  MODEL_ENDPOINTS["ICON"],  True),
        ("NAM",   MODEL_ENDPOINTS["NAM"],   in_conus),
        ("HRRR",  MODEL_ENDPOINTS["HRRR"],  in_conus),
    ]

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

    model_results = []
    for name, url, in_coverage in all_candidate_models:
        if not in_coverage:
            # Don't query — return an OUT_OF_COVERAGE placeholder so the
            # dashboard can show the row with a clear "not available here"
            # state rather than silently letting Open-Meteo return GFS data
            # masquerading as the requested model.
            model_results.append(_empty_record(name, "OUT_OF_COVERAGE"))
            continue

        history = _fetch_model_history(name, url, lat, lon)
        if history is None:
            model_results.append(_empty_record(name, "UNAVAILABLE"))
            continue

        # Aggregate MAE
        mae = compute_model_mae(history, all_observations)
        mae["name"] = name
        mae["status"] = "OK"
        mae["composite_score"] = _composite_score(mae)

        # Per-hour pairings (used both for trend rendering and downstream analysis)
        pairings = compute_model_pairings(history, all_observations)
        mae["rolling"] = compute_rolling_mae(
            pairings, window_hours=6, step_hours=1, span_hours=24
        )

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
        "kestrel_count": len(kestrel_obs),
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
