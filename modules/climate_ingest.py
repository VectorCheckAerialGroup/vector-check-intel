"""
VECTOR CHECK AERIAL GROUP INC. — Climate Context Engine (Tiered)

Fetches 25-year hourly historical climate data from a tiered set of sources:

  TIER 1: ECCC GeoMet OGC API (api.weather.gc.ca)
          - Real station observations from Canadian weather stations
          - Free, no auth, commercial use allowed (Government of Canada open data)
          - Used when an hourly station exists within 30 km of the query point

  TIER 2: NASA POWER Hourly Point API (power.larc.nasa.gov)
          - Gridded reanalysis (MERRA-2) at ~50 km resolution
          - Free, no auth, no commercial restrictions (US government open data)
          - Global coverage, used as fallback when no ECCC station nearby

ARCHITECTURE:
    Dashboard → get_climate_context(lat, lon, month)
        → check Supabase cache
        → on miss: try ECCC station first
        → if no station within 30 km: try NASA POWER
        → cache result with source tag
        → return ClimateContext with source badge data
"""

import urllib.request
import urllib.parse
import json
import math
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger("arms.climate")

# =============================================================================
# CONFIGURATION
# =============================================================================

ECCC_STATIONS_URL = "https://api.weather.gc.ca/collections/climate-stations/items"
ECCC_HOURLY_URL = "https://api.weather.gc.ca/collections/climate-hourly/items"
NASA_POWER_URL = "https://power.larc.nasa.gov/api/temporal/hourly/point"

# Date range — defines the climatology window. NASA POWER's earliest hourly
# data is 2001; ERA5 extends back to 1940 but we keep the same window so
# percentiles are comparable across sources (mixing record lengths would
# produce misleading "this is unusual for the season" badges). To extend
# the record on an ERA5-only site, change CLIMATE_START_YEAR — the rest of
# the pipeline handles arbitrary spans.
CLIMATE_START_YEAR = 2001
CLIMATE_END_YEAR = 2025

ECCC_MAX_STATION_DISTANCE_KM = 50.0
ECCC_BBOX_PADDING_DEG = 0.6

SPATIAL_BIN_RESOLUTION = 0.1

SOURCE_ECCC = "ECCC"
SOURCE_NASA_POWER = "NASA_POWER"

REQUEST_TIMEOUT_S = 30
REQUEST_DELAY_S = 0.4

KMH_TO_KT = 0.539957
MS_TO_KT = 1.94384
KPA_TO_HPA = 10.0

COMPASS_DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

PERCENTILE_TABLE = "climate_percentiles"
WIND_ROSE_TABLE = "climate_wind_rose"

USER_AGENT = "VectorCheck-ARMS/2.1 (atmospheric risk management)"


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class VariablePercentiles:
    p10: float = 0.0
    p25: float = 0.0
    p50: float = 0.0
    p75: float = 0.0
    p90: float = 0.0
    p99: float = 0.0
    mean: float = 0.0
    sample_count: int = 0


@dataclass
class WindRoseBin:
    direction: str = ""
    total_pct: float = 0.0
    calm_pct: float = 0.0
    moderate_pct: float = 0.0
    strong_pct: float = 0.0
    avg_speed_kt: float = 0.0


@dataclass
class ClimateContext:
    lat_bin: float = 0.0
    lon_bin: float = 0.0
    month: int = 1
    years_range: str = ""

    wind: VariablePercentiles = field(default_factory=VariablePercentiles)
    temp: VariablePercentiles = field(default_factory=VariablePercentiles)
    pressure: VariablePercentiles = field(default_factory=VariablePercentiles)
    rh: VariablePercentiles = field(default_factory=VariablePercentiles)

    wind_rose: list = field(default_factory=list)
    prevailing_dir: str = ""
    prevailing_pct: float = 0.0

    source: str = ""
    source_label: str = ""
    source_distance_km: float = 0.0

    cached: bool = False
    error: str = ""


# =============================================================================
# HELPERS
# =============================================================================

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _bin_coord(val: float) -> float:
    return round(round(val / SPATIAL_BIN_RESOLUTION) * SPATIAL_BIN_RESOLUTION, 4)


# =============================================================================
# TIER 1 — ECCC
# =============================================================================

def _find_nearest_eccc_station(lat: float, lon: float):
    pad = ECCC_BBOX_PADDING_DEG
    bbox = f"{lon - pad},{lat - pad},{lon + pad},{lat + pad}"

    params = {
        "bbox": bbox,
        "HAS_HOURLY_DATA": "Y",
        "f": "json",
        "limit": "200",
    }
    url = f"{ECCC_STATIONS_URL}?{urllib.parse.urlencode(params)}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning("ECCC station search failed: %s", e)
        return None

    features = data.get("features", [])
    if not features:
        return None

    best = None
    best_dist = float("inf")

    for feat in features:
        props = feat.get("properties", {})
        coords = feat.get("geometry", {}).get("coordinates", [])
        if not coords or len(coords) < 2:
            continue

        try:
            stn_lon, stn_lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            continue

        dist = _haversine_km(lat, lon, stn_lat, stn_lon)
        if dist > ECCC_MAX_STATION_DISTANCE_KM:
            continue

        stn_id = (
            props.get("CLIMATE_IDENTIFIER")
            or props.get("STN_ID")
            or props.get("STATION_ID")
        )
        if not stn_id:
            continue

        last_date = props.get("LAST_DATE", "")
        if last_date and isinstance(last_date, str) and len(last_date) >= 4:
            try:
                if int(last_date[:4]) < CLIMATE_START_YEAR:
                    continue
            except (ValueError, TypeError):
                pass

        if dist < best_dist:
            best_dist = dist
            best = {
                "station_id": str(stn_id),
                "station_name": props.get("STATION_NAME", "Unknown Station"),
                "distance_km": round(dist, 1),
            }

    return best


def _fetch_eccc_year(station_id: str, year: int):
    params = {
        "CLIMATE_IDENTIFIER": station_id,
        "datetime": f"{year}-01-01 00:00:00/{year}-12-31 23:59:59",
        "f": "json",
        "limit": "10000",
        "sortby": "LOCAL_DATE",
    }
    url = f"{ECCC_HOURLY_URL}?{urllib.parse.urlencode(params)}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning("ECCC hourly fetch failed for %s year %d: %s", station_id, year, e)
        return None

    features = data.get("features", [])
    if not features:
        return None

    out = {
        "wind_kt": [], "temp_c": [], "rh": [],
        "pressure_hpa": [], "wind_dir": [], "timestamps": [],
    }

    for feat in features:
        p = feat.get("properties", {})
        ts = p.get("LOCAL_DATE") or p.get("UTC_DATE")
        if not ts or not isinstance(ts, str):
            continue
        out["timestamps"].append(ts)

        # Temperature (°C) — field name unchanged across schema versions
        t = p.get("TEMP")
        out["temp_c"].append(float(t) if t is not None else None)

        # Relative humidity (%) — ECCC OGC API uses RELATIVE_HUMIDITY
        # Fall back to legacy REL_HUM if the schema is older
        rh = p.get("RELATIVE_HUMIDITY")
        if rh is None:
            rh = p.get("REL_HUM")
        out["rh"].append(float(rh) if rh is not None else None)

        # Wind speed (km/h → kt) — ECCC OGC API uses WIND_SPEED
        ws = p.get("WIND_SPEED")
        if ws is None:
            ws = p.get("WIND_SPD")
        out["wind_kt"].append(float(ws) * KMH_TO_KT if ws is not None else None)

        # Wind direction — ECCC OGC API uses WIND_DIRECTION
        # Encoding auto-detect: legacy uses tens-of-degrees (0-36),
        # newer schemas use full degrees (0-360)
        wd = p.get("WIND_DIRECTION")
        if wd is None:
            wd = p.get("WIND_DIR")
        if wd is not None:
            wd_val = float(wd)
            if wd_val <= 36:
                wd_val = wd_val * 10.0
            out["wind_dir"].append(wd_val)
        else:
            out["wind_dir"].append(None)

        # Station pressure (kPa → hPa)
        sp = p.get("STATION_PRESSURE")
        out["pressure_hpa"].append(float(sp) * KPA_TO_HPA if sp is not None else None)

    return out


# =============================================================================
# TIER 2 — NASA POWER
# =============================================================================

def _fetch_nasa_power_year(lat: float, lon: float, year: int):
    params = {
        "parameters": "T2M,RH2M,WS10M,WD10M,PS",
        "community": "RE",
        "longitude": str(lon),
        "latitude": str(lat),
        "start": f"{year}0101",
        "end": f"{year}1231",
        "format": "JSON",
        "time-standard": "UTC",
    }
    url = f"{NASA_POWER_URL}?{urllib.parse.urlencode(params)}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning("NASA POWER fetch failed for %f,%f year %d: %s", lat, lon, year, e)
        return None

    try:
        params_data = data["properties"]["parameter"]
    except (KeyError, TypeError):
        return None

    t2m = params_data.get("T2M", {})
    rh2m = params_data.get("RH2M", {})
    ws10m = params_data.get("WS10M", {})
    wd10m = params_data.get("WD10M", {})
    ps = params_data.get("PS", {})

    out = {
        "wind_kt": [], "temp_c": [], "rh": [],
        "pressure_hpa": [], "wind_dir": [], "timestamps": [],
    }

    NA = -999.0
    for ts in sorted(t2m.keys()):
        out["timestamps"].append(ts)

        t_val = t2m.get(ts)
        out["temp_c"].append(float(t_val) if t_val is not None and t_val != NA else None)

        rh_val = rh2m.get(ts)
        out["rh"].append(float(rh_val) if rh_val is not None and rh_val != NA else None)

        ws_val = ws10m.get(ts)
        out["wind_kt"].append(float(ws_val) * MS_TO_KT if ws_val is not None and ws_val != NA else None)

        wd_val = wd10m.get(ts)
        out["wind_dir"].append(float(wd_val) if wd_val is not None and wd_val != NA else None)

        ps_val = ps.get(ts)
        out["pressure_hpa"].append(float(ps_val) * KPA_TO_HPA if ps_val is not None and ps_val != NA else None)

    return out


# =============================================================================
# TIER 2 (preferred reanalysis): ERA5 via Meteomatics (downscaled to 90m)
# =============================================================================
# Meteomatics serves ECMWF ERA5 reanalysis with their proprietary 90m
# downscaling layer applied. While the underlying ERA5 grid is 25km, the
# downscaled output accounts for high-resolution terrain, land usage, and
# coastline features. For point-based aviation forecasting at VCAG sites
# (some of which sit on lake shores or in valleys), this produces a
# meaningfully better climatology than raw 25km ERA5.
#
# Latency: 5-7 days behind realtime (Copernicus production cycle).
# Resolution: ~90 m horizontal (downscaled), 1 hour temporal.
# Coverage: global.
# Record: 1940 to present.
#
# Quota cost: each year-of-hourly-data call is sized like a normal forecast
# query. For a 25-year bootstrap at one site that's 25 × (5 params × hours)
# which approximates ~125 quota units total. Cached in Supabase after first
# computation so subsequent operators at the same site pay nothing.

SOURCE_ERA5_MM = "ERA5_MM"   # ERA5 via Meteomatics (downscaled 90m)
SOURCE_ERA5 = "ERA5"          # ERA5 via Open-Meteo archive (25km, fallback)


def _fetch_meteomatics_era5_year(lat: float, lon: float, year: int):
    """Fetches one year of hourly ERA5 reanalysis via Meteomatics.

    Uses source=ecmwf-era5 with Meteomatics' 90m downscaling layer applied.
    Returns the same dict shape as _fetch_era5_year so the merge/percentile
    pipeline downstream is unchanged.
    Returns None on any failure (caller falls through to the next tier).
    """
    # Lazy import to keep climate_ingest independent of Meteomatics module
    # at import time (some environments may not have credentials configured).
    try:
        from modules.meteomatics_provider import (
            _get_credentials as _mm_creds,
            METEOMATICS_BASE as _MM_BASE,
            DEFAULT_TIMEOUT_S as _MM_TIMEOUT,
        )
    except ImportError:
        return None

    creds = _mm_creds()
    if creds is None:
        return None

    # Build the time range. Meteomatics validdate format:
    #   START--END:STEP   where STEP is PT1H for hourly
    start = f"{year}-01-01T00:00:00Z"
    end = f"{year}-12-31T23:00:00Z"
    validdate = f"{start}--{end}:PT1H"

    # Surface parameters matching what NASA POWER and Open-Meteo ERA5 return.
    # Pressure uses msl_pressure for METAR-comparable values.
    params = ",".join([
        "t_2m:C",
        "relative_humidity_2m:p",
        "wind_speed_10m:kn",
        "wind_dir_10m:d",
        "msl_pressure:hPa",
    ])

    url = (f"{_MM_BASE}/{validdate}/{params}/{lat:.4f},{lon:.4f}"
           f"/json?source=ecmwf-era5")

    try:
        from modules.meteomatics_provider import _mm_fetch_json as _fj
        from modules.http_client import HttpFetchError as _HFE
        payload = _fj(url, timeout=_MM_TIMEOUT, retries=2, basic_auth=creds)
    except Exception as e:
        logger.warning("Meteomatics ERA5 fetch failed for %f,%f year %d: %s",
                        lat, lon, year, e)
        return None

    # Parse: Meteomatics returns one block per parameter, each with parallel
    # date/value lists. We need to align them by validdate.
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
            times_iso = [d["date"] for d in dates]

    if not times_iso:
        return None

    def _vals(mm_key: str) -> list:
        dates = by_param.get(mm_key) or []
        out = []
        for d in dates:
            v = d.get("value")
            if v is None:
                out.append(None)
            else:
                try:
                    f = float(v)
                    out.append(None if f <= -998 else f)
                except (TypeError, ValueError):
                    out.append(None)
        return out

    out = {
        "timestamps":   times_iso,
        "temp_c":       _vals("t_2m:C"),
        "rh":           _vals("relative_humidity_2m:p"),
        "wind_kt":      _vals("wind_speed_10m:kn"),
        "wind_dir":     _vals("wind_dir_10m:d"),
        "pressure_hpa": _vals("msl_pressure:hPa"),
    }

    # Sanity: if all variables are entirely empty, return None
    if not any(any(v is not None for v in out[k])
                for k in ["temp_c", "wind_kt", "rh", "pressure_hpa"]):
        return None

    return out


# =============================================================================
# TIER 3 (fallback reanalysis): ERA5 via Open-Meteo archive
# =============================================================================
# ERA5 is the European Centre for Medium-Range Weather Forecasts' fifth
# global atmospheric reanalysis. Hourly data from 1940 to present, ~25 km
# grid resolution, internally consistent across the full record because the
# same NWP system (ECMWF IFS frozen at cycle 41r2) is used throughout. It's
# the de facto standard for climate normals and operational analysis.
#
# Latency: 5-7 days behind realtime (Copernicus production cycle).
# Resolution: 25 km horizontal, hourly temporal.
# Coverage: global, including ocean and poles.
#
# We pull via Open-Meteo's archive API which serves pre-decoded ERA5 as a
# fast JSON time-series. Through the paid customer-archive-api endpoint
# we get higher rate limits and no IP-based throttling.


def _fetch_era5_year(lat: float, lon: float, year: int):
    """Fetches one year of hourly ERA5 reanalysis at the query point.

    Returns the same dict shape as _fetch_nasa_power_year so it slots into
    the existing _merge_data + _compute_percentiles pipeline unchanged.
    Returns None on any failure (caller falls through to the next tier).
    """
    from modules.open_meteo_endpoints import build_archive_url

    # Variable selection matches what we use for climatology: 2m temp/RH,
    # 10m wind speed/direction, MSL pressure. Open-Meteo's archive API
    # canonical names (different from forecast API in places — note
    # wind_speed_10m vs windspeed_10m; we use the underscore form which
    # they accept on the archive endpoint per their docs).
    suffix = (
        f"latitude={lat}&longitude={lon}"
        f"&start_date={year}-01-01&end_date={year}-12-31"
        f"&hourly=temperature_2m,relative_humidity_2m,"
        f"wind_speed_10m,wind_direction_10m,pressure_msl"
        f"&wind_speed_unit=kn"
        f"&timezone=UTC"
    )
    url = build_archive_url(endpoint="archive", query_suffix=suffix)

    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning("ERA5 fetch failed for %f,%f year %d: %s", lat, lon, year, e)
        return None

    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        return None

    # Open-Meteo returns parallel arrays already in our target units
    # (wind in knots because we passed wind_speed_unit=kn). Direct copy.
    t2m_arr = hourly.get("temperature_2m") or []
    rh2m_arr = hourly.get("relative_humidity_2m") or []
    ws_arr = hourly.get("wind_speed_10m") or []
    wd_arr = hourly.get("wind_direction_10m") or []
    pmsl_arr = hourly.get("pressure_msl") or []

    out = {
        "wind_kt": [], "temp_c": [], "rh": [],
        "pressure_hpa": [], "wind_dir": [], "timestamps": [],
    }
    for i, t_str in enumerate(times):
        # Normalize timestamps to ISO 8601 with 'T' separator for
        # _filter_to_month compatibility. Open-Meteo returns
        # "2024-01-01T00:00" form already.
        out["timestamps"].append(t_str)

        def _safe(arr, idx):
            if idx < len(arr) and arr[idx] is not None:
                try:
                    return float(arr[idx])
                except (TypeError, ValueError):
                    return None
            return None

        out["temp_c"].append(_safe(t2m_arr, i))
        out["rh"].append(_safe(rh2m_arr, i))
        out["wind_kt"].append(_safe(ws_arr, i))
        out["wind_dir"].append(_safe(wd_arr, i))
        out["pressure_hpa"].append(_safe(pmsl_arr, i))

    return out


# =============================================================================
# DATA UTILITIES
# =============================================================================

def _filter_to_month(year_data: dict, month: int) -> dict:
    filtered = {k: [] for k in year_data.keys()}
    for i, ts in enumerate(year_data["timestamps"]):
        try:
            ts_month = int(ts[5:7]) if "-" in ts else int(ts[4:6])
        except (ValueError, IndexError):
            continue
        if ts_month == month:
            for k in year_data.keys():
                filtered[k].append(year_data[k][i])
    return filtered


def _merge_data(merged: dict, new: dict) -> None:
    for k in merged.keys():
        merged[k].extend(new.get(k, []))


# =============================================================================
# STATISTICS
# =============================================================================

def _compute_percentiles(values: list) -> VariablePercentiles:
    clean = sorted(v for v in values if v is not None)
    n = len(clean)
    if n == 0:
        return VariablePercentiles()

    def pct(p: float) -> float:
        idx = (p / 100.0) * (n - 1)
        lo = int(math.floor(idx))
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return round(clean[lo] + frac * (clean[hi] - clean[lo]), 2)

    return VariablePercentiles(
        p10=pct(10), p25=pct(25), p50=pct(50),
        p75=pct(75), p90=pct(90), p99=pct(99),
        mean=round(sum(clean) / n, 2),
        sample_count=n,
    )


def _compute_wind_rose(speeds_kt: list, directions: list) -> list:
    bins = {d: {0: 0, 1: 0, 2: 0, "speeds": [], "total": 0} for d in COMPASS_DIRS}
    total_valid = 0

    for spd, deg in zip(speeds_kt, directions):
        if spd is None or deg is None:
            continue
        try:
            spd_f = float(spd)
            deg_f = float(deg)
        except (TypeError, ValueError):
            continue

        dir_idx = int(round(deg_f / 45.0)) % 8
        dir_name = COMPASS_DIRS[dir_idx]

        if spd_f < 10:   bin_idx = 0
        elif spd_f < 20: bin_idx = 1
        else:            bin_idx = 2

        bins[dir_name][bin_idx] += 1
        bins[dir_name]["speeds"].append(spd_f)
        bins[dir_name]["total"] += 1
        total_valid += 1

    if total_valid == 0:
        return [WindRoseBin(direction=d) for d in COMPASS_DIRS]

    result = []
    for d in COMPASS_DIRS:
        b = bins[d]
        total = b["total"]
        avg_spd = round(sum(b["speeds"]) / len(b["speeds"]), 1) if b["speeds"] else 0.0
        result.append(WindRoseBin(
            direction=d,
            total_pct=round(100.0 * total / total_valid, 1),
            calm_pct=round(100.0 * b[0] / total_valid, 1),
            moderate_pct=round(100.0 * b[1] / total_valid, 1),
            strong_pct=round(100.0 * b[2] / total_valid, 1),
            avg_speed_kt=avg_spd,
        ))

    result.sort(key=lambda x: x.total_pct, reverse=True)
    return result


def _build_context(merged, lat_bin, lon_bin, month, source, source_label, distance_km):
    ctx = ClimateContext(
        lat_bin=lat_bin, lon_bin=lon_bin, month=month,
        years_range=f"{CLIMATE_START_YEAR}\u2013{CLIMATE_END_YEAR}",
        source=source, source_label=source_label, source_distance_km=distance_km,
    )
    ctx.wind = _compute_percentiles(merged["wind_kt"])
    ctx.temp = _compute_percentiles(merged["temp_c"])
    ctx.pressure = _compute_percentiles(merged["pressure_hpa"])
    ctx.rh = _compute_percentiles(merged["rh"])
    ctx.wind_rose = _compute_wind_rose(merged["wind_kt"], merged["wind_dir"])

    if ctx.wind_rose:
        top = ctx.wind_rose[0]
        if len(ctx.wind_rose) >= 2:
            second = ctx.wind_rose[1]
            ctx.prevailing_dir = f"{top.direction} / {second.direction}"
            ctx.prevailing_pct = round(top.total_pct + second.total_pct, 1)
        else:
            ctx.prevailing_dir = top.direction
            ctx.prevailing_pct = top.total_pct

    return ctx


# =============================================================================
# SUPABASE CACHE
# =============================================================================

def _load_from_cache(sb_client, lat_bin, lon_bin, month):
    try:
        pct_result = (
            sb_client.table(PERCENTILE_TABLE)
            .select("*")
            .eq("lat_bin", lat_bin).eq("lon_bin", lon_bin).eq("month", month)
            .execute()
        )
        if not pct_result.data:
            return None

        first = pct_result.data[0]
        _cached_source = first.get("source", "")

        # Tier-order upgrade: when this code was deployed the tier order
        # changed from (ECCC, NASA_POWER) to (ECCC, ERA5, NASA_POWER). Any
        # entries cached as NASA_POWER may now be eligible for ECCC (wider
        # 50km radius) or ERA5 (preferred reanalysis). Treat them as cache
        # miss so the tier dispatcher re-resolves once, then settles into
        # the better source.
        if _cached_source == SOURCE_NASA_POWER:
            logger.info("Cached NASA POWER entry for (%s, %s, %d) treated as "
                        "stale to allow re-evaluation against new tier order",
                        lat_bin, lon_bin, month)
            return None

        ctx = ClimateContext(
            lat_bin=lat_bin, lon_bin=lon_bin, month=month,
            years_range=f"{CLIMATE_START_YEAR}\u2013{CLIMATE_END_YEAR}",
            cached=True,
            source=_cached_source,
            source_label=first.get("source_label", ""),
            source_distance_km=first.get("source_distance_km", 0.0) or 0.0,
        )

        for row in pct_result.data:
            vp = VariablePercentiles(
                p10=row["p10"], p25=row["p25"], p50=row["p50"],
                p75=row["p75"], p90=row["p90"], p99=row["p99"],
                mean=row["mean_val"], sample_count=row["sample_count"],
            )
            v = row["variable"]
            if v == "wind":     ctx.wind = vp
            elif v == "temp":   ctx.temp = vp
            elif v == "pressure": ctx.pressure = vp
            elif v == "rh":     ctx.rh = vp

        wr_result = (
            sb_client.table(WIND_ROSE_TABLE)
            .select("*")
            .eq("lat_bin", lat_bin).eq("lon_bin", lon_bin).eq("month", month)
            .order("total_pct", desc=True)
            .execute()
        )
        if wr_result.data:
            for row in wr_result.data:
                ctx.wind_rose.append(WindRoseBin(
                    direction=row["direction"],
                    total_pct=row["total_pct"],
                    calm_pct=row["calm_pct"],
                    moderate_pct=row["moderate_pct"],
                    strong_pct=row["strong_pct"],
                    avg_speed_kt=row["avg_speed_kt"],
                ))
            if ctx.wind_rose:
                top = ctx.wind_rose[0]
                if len(ctx.wind_rose) >= 2:
                    second = ctx.wind_rose[1]
                    ctx.prevailing_dir = f"{top.direction} / {second.direction}"
                    ctx.prevailing_pct = round(top.total_pct + second.total_pct, 1)
                else:
                    ctx.prevailing_dir = top.direction
                    ctx.prevailing_pct = top.total_pct

        return ctx
    except Exception as e:
        logger.debug("Climate cache read failed: %s", e)
        return None


def _save_to_cache(sb_client, ctx: ClimateContext) -> None:
    """Persists computed climate context to Supabase with source tagging.

    Uses upsert with explicit on_conflict targets so that re-running the
    bootstrap (or refreshing a cached site) updates existing rows in place
    instead of failing on the unique constraint.
    """
    try:
        now_iso = datetime.now(timezone.utc).isoformat()

        for var_name, vp in [("wind", ctx.wind), ("temp", ctx.temp),
                             ("pressure", ctx.pressure), ("rh", ctx.rh)]:
            try:
                sb_client.table(PERCENTILE_TABLE).upsert(
                    {
                        "lat_bin": ctx.lat_bin, "lon_bin": ctx.lon_bin,
                        "month": ctx.month, "variable": var_name,
                        "p10": vp.p10, "p25": vp.p25, "p50": vp.p50,
                        "p75": vp.p75, "p90": vp.p90, "p99": vp.p99,
                        "mean_val": vp.mean, "sample_count": vp.sample_count,
                        "source": ctx.source, "source_label": ctx.source_label,
                        "source_distance_km": ctx.source_distance_km,
                        "updated_at": now_iso,
                    },
                    on_conflict="lat_bin,lon_bin,month,variable",
                ).execute()
            except Exception as e:
                logger.warning("Percentile upsert failed for %s: %s", var_name, e)

        for wr in ctx.wind_rose:
            try:
                sb_client.table(WIND_ROSE_TABLE).upsert(
                    {
                        "lat_bin": ctx.lat_bin, "lon_bin": ctx.lon_bin,
                        "month": ctx.month, "direction": wr.direction,
                        "total_pct": wr.total_pct,
                        "calm_pct": wr.calm_pct,
                        "moderate_pct": wr.moderate_pct,
                        "strong_pct": wr.strong_pct,
                        "avg_speed_kt": wr.avg_speed_kt,
                        "source": ctx.source, "source_label": ctx.source_label,
                        "updated_at": now_iso,
                    },
                    on_conflict="lat_bin,lon_bin,month,direction",
                ).execute()
            except Exception as e:
                logger.warning("Wind rose upsert failed for %s: %s", wr.direction, e)
    except Exception as e:
        logger.warning("Climate cache write failed: %s", e)


# =============================================================================
# RUNTIME ENTRY POINT
# =============================================================================

def get_climate_context(lat: float, lon: float, month: int, sb_client=None) -> ClimateContext:
    """Returns climate context for one location and month using tiered fallback.

    Cache → ECCC station → NASA POWER → graceful failure.
    """
    lat_bin = _bin_coord(lat)
    lon_bin = _bin_coord(lon)

    if sb_client is not None:
        cached = _load_from_cache(sb_client, lat_bin, lon_bin, month)
        if cached is not None and cached.wind.sample_count > 0:
            return cached

    # Tier 1: ECCC (real station observations)
    station = _find_nearest_eccc_station(lat, lon)
    if station is not None:
        merged = {k: [] for k in ["wind_kt", "temp_c", "rh", "pressure_hpa", "wind_dir", "timestamps"]}
        for year in range(CLIMATE_START_YEAR, CLIMATE_END_YEAR + 1):
            year_data = _fetch_eccc_year(station["station_id"], year)
            if year_data is not None:
                _merge_data(merged, _filter_to_month(year_data, month))
            time.sleep(REQUEST_DELAY_S)

        if len(merged["wind_kt"]) >= 100:
            ctx = _build_context(
                merged, lat_bin, lon_bin, month,
                source=SOURCE_ECCC,
                source_label=f"ECCC {station['station_name']} \u00b7 {station['distance_km']} km",
                distance_km=station["distance_km"],
            )
            if sb_client is not None:
                _save_to_cache(sb_client, ctx)
            return ctx

    # Tier 2: ERA5 reanalysis via Meteomatics (downscaled to 90m).
    # This is the highest-quality reanalysis option when Meteomatics
    # credentials are configured. Falls through silently if not.
    try:
        from modules.meteomatics_provider import has_credentials as _mm_has_creds
        if _mm_has_creds():
            merged_mm = {k: [] for k in ["wind_kt", "temp_c", "rh", "pressure_hpa", "wind_dir", "timestamps"]}
            for year in range(CLIMATE_START_YEAR, CLIMATE_END_YEAR + 1):
                year_data = _fetch_meteomatics_era5_year(lat, lon, year)
                if year_data is not None:
                    _merge_data(merged_mm, _filter_to_month(year_data, month))
                time.sleep(REQUEST_DELAY_S)

            if len(merged_mm["wind_kt"]) >= 100:
                ctx = _build_context(
                    merged_mm, lat_bin, lon_bin, month,
                    source=SOURCE_ERA5_MM,
                    source_label="ERA5 reanalysis \u00b7 Meteomatics 90 m downscaled",
                    distance_km=0.09,    # 90 m
                )
                if sb_client is not None:
                    _save_to_cache(sb_client, ctx)
                return ctx
    except ImportError:
        pass

    # Tier 3: ERA5 reanalysis via Open-Meteo archive (25 km native grid).
    # Falls back here if Meteomatics ERA5 fails or credentials aren't set.
    # ECMWF ERA5 remains the gold standard for climatology — internally
    # consistent 1940-present record at hourly resolution.
    merged = {k: [] for k in ["wind_kt", "temp_c", "rh", "pressure_hpa", "wind_dir", "timestamps"]}
    for year in range(CLIMATE_START_YEAR, CLIMATE_END_YEAR + 1):
        year_data = _fetch_era5_year(lat, lon, year)
        if year_data is not None:
            _merge_data(merged, _filter_to_month(year_data, month))
        time.sleep(REQUEST_DELAY_S)

    if len(merged["wind_kt"]) >= 100:
        ctx = _build_context(
            merged, lat_bin, lon_bin, month,
            source=SOURCE_ERA5,
            source_label="ERA5 reanalysis \u00b7 ~25 km grid",
            distance_km=25.0,
        )
        if sb_client is not None:
            _save_to_cache(sb_client, ctx)
        return ctx

    # Tier 4: NASA POWER (MERRA-2 reanalysis, ~50 km, final fallback)
    merged = {k: [] for k in ["wind_kt", "temp_c", "rh", "pressure_hpa", "wind_dir", "timestamps"]}
    for year in range(CLIMATE_START_YEAR, CLIMATE_END_YEAR + 1):
        year_data = _fetch_nasa_power_year(lat, lon, year)
        if year_data is not None:
            _merge_data(merged, _filter_to_month(year_data, month))
        time.sleep(REQUEST_DELAY_S)

    if len(merged["wind_kt"]) >= 100:
        ctx = _build_context(
            merged, lat_bin, lon_bin, month,
            source=SOURCE_NASA_POWER,
            source_label="NASA POWER \u00b7 ~50 km grid",
            distance_km=50.0,
        )
        if sb_client is not None:
            _save_to_cache(sb_client, ctx)
        return ctx

    return ClimateContext(
        lat_bin=lat_bin, lon_bin=lon_bin, month=month,
        error="ECCC, Meteomatics ERA5, Open-Meteo ERA5, and NASA POWER all unavailable for this location.",
    )


# =============================================================================
# BOOTSTRAP HELPER — All 12 months at one site, optimized
# =============================================================================

def bootstrap_site(lat: float, lon: float, sb_client) -> dict:
    """Bootstraps all 12 months for one site with one fetch per year."""
    lat_bin = _bin_coord(lat)
    lon_bin = _bin_coord(lon)

    station = _find_nearest_eccc_station(lat, lon)
    if station is not None:
        use_tier = "ECCC"
        source = SOURCE_ECCC
        source_label = f"ECCC {station['station_name']} \u00b7 {station['distance_km']} km"
        distance_km = station["distance_km"]
    else:
        # Prefer Meteomatics ERA5 (90m downscaled) when credentials available;
        # falls back to raw 25km ERA5 via Open-Meteo if not.
        _mm_available = False
        try:
            from modules.meteomatics_provider import has_credentials as _mm_has_creds
            _mm_available = _mm_has_creds()
        except ImportError:
            pass

        if _mm_available:
            use_tier = "ERA5_MM"
            source = SOURCE_ERA5_MM
            source_label = "ERA5 reanalysis \u00b7 Meteomatics 90 m downscaled"
            distance_km = 0.09
        else:
            use_tier = "ERA5"
            source = SOURCE_ERA5
            source_label = "ERA5 reanalysis \u00b7 ~25 km grid"
            distance_km = 25.0

    monthly_buckets = {
        m: {k: [] for k in ["wind_kt", "temp_c", "rh", "pressure_hpa", "wind_dir", "timestamps"]}
        for m in range(1, 13)
    }

    years_succeeded = 0
    years_failed = 0

    for year in range(CLIMATE_START_YEAR, CLIMATE_END_YEAR + 1):
        if use_tier == "ECCC":
            year_data = _fetch_eccc_year(station["station_id"], year)
        elif use_tier == "ERA5_MM":
            year_data = _fetch_meteomatics_era5_year(lat, lon, year)
        elif use_tier == "ERA5":
            year_data = _fetch_era5_year(lat, lon, year)
        else:
            year_data = _fetch_nasa_power_year(lat, lon, year)

        if year_data is None:
            years_failed += 1
            time.sleep(REQUEST_DELAY_S)
            continue

        years_succeeded += 1

        for i, ts in enumerate(year_data["timestamps"]):
            try:
                m = int(ts[5:7]) if "-" in ts else int(ts[4:6])
            except (ValueError, IndexError):
                continue
            if 1 <= m <= 12:
                for k in monthly_buckets[m].keys():
                    monthly_buckets[m][k].append(year_data[k][i])

        time.sleep(REQUEST_DELAY_S)

    months_saved = 0
    for month in range(1, 13):
        merged = monthly_buckets[month]
        if len(merged["wind_kt"]) < 100:
            continue
        ctx = _build_context(
            merged, lat_bin, lon_bin, month,
            source=source, source_label=source_label, distance_km=distance_km,
        )
        _save_to_cache(sb_client, ctx)
        months_saved += 1

    return {
        "tier": use_tier,
        "source_label": source_label,
        "years_succeeded": years_succeeded,
        "years_failed": years_failed,
        "months_saved": months_saved,
        "station": station,
    }
