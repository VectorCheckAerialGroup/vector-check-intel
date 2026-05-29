import urllib.request
import json
from datetime import datetime, timezone
import logging

from modules.http_client import fetch_json, fetch_text, HttpFetchError

logger = logging.getLogger("arms.data_ingest")


def get_model_run_info(model_url: str, model_id: str = None) -> dict:
    """Fetches the latest model run cycle timestamp from Open-Meteo metadata.

    Each model exposes a meta.json at a predictable path. The response contains
    `last_run_initialisation_time` (Unix timestamp) which identifies which
    cycle (00Z, 06Z, 12Z, 18Z, etc.) produced the current data.

    Args:
        model_url:  the forecast endpoint (e.g. https://api.open-meteo.com/v1/gem)
        model_id:   optional explicit identifier ('hrdps', 'gfs', 'ecmwf',
                    'icon', 'nam', 'hrrr'). When provided, takes precedence
                    over substring matching on model_url. Required when querying
                    HRRR/NAM since both share the v1/gfs URL prefix.

    Returns:
        dict with keys: run_cycle_z (e.g. "12Z"), run_date (YYYY-MM-DD),
        run_datetime_utc (datetime), age_hours (int), or empty dict on failure
    """
    # Direct identifier lookups — preferred path
    id_map = {
        "hrdps":  "https://api.open-meteo.com/data/cmc_gem_hrdps_continental/static/meta.json",
        "ecmwf":  "https://api.open-meteo.com/data/ecmwf_ifs025/static/meta.json",
        "gfs":    "https://api.open-meteo.com/data/ncep_gfs025/static/meta.json",
        "icon":   "https://api.open-meteo.com/data/dwd_icon/static/meta.json",
        "nam":    "https://api.open-meteo.com/data/ncep_nam_conus/static/meta.json",
        "hrrr":   "https://api.open-meteo.com/data/ncep_hrrr_conus/static/meta.json",
        "icon-eu": "https://api.open-meteo.com/data/dwd_icon_eu/static/meta.json",
    }

    meta_url = None
    if model_id:
        meta_url = id_map.get(model_id.lower())

    if meta_url is None:
        # Fallback to URL substring matching (legacy behavior, may give wrong
        # answer for HRRR/NAM since they share the v1/gfs prefix)
        substring_map = {
            "v1/gem":      id_map["hrdps"],
            "v1/forecast": id_map["ecmwf"],
            "v1/gfs":      id_map["gfs"],
            "v1/ecmwf":    id_map["ecmwf"],
            "v1/dwd-icon": id_map["icon"],
        }
        for key, url in substring_map.items():
            if key in model_url:
                meta_url = url
                break

    if meta_url is None:
        return {}

    try:
        meta = fetch_json(meta_url, timeout=5, retries=2)
    except HttpFetchError as e:
        logger.info("Run-info fetch failed: %s", e)
        return {}

    try:
        ts = meta.get("last_run_initialisation_time")
        if ts is None:
            return {}

        run_dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        now = datetime.now(timezone.utc)
        age_hours = int((now - run_dt).total_seconds() / 3600)

        return {
            "run_cycle_z": f"{run_dt.hour:02d}Z",
            "run_date": run_dt.strftime('%Y-%m-%d'),
            "run_datetime_utc": run_dt,
            "age_hours": age_hours,
        }
    except (ValueError, TypeError, KeyError) as e:
        logger.warning("Run-info parse failed: %s", e)
        return {}


def get_aviation_weather(icao):
    """Fetches real-time METAR and TAF for the specified ICAO code.

    Returns a (metar, taf) tuple with "NIL" sentinels on failure. Each fetch
    is independently retried; a failure of one doesn't block the other.
    """
    metar_url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=raw"
    taf_url = f"https://aviationweather.gov/api/data/taf?ids={icao}&format=raw"

    try:
        metar = fetch_text(metar_url, timeout=5, retries=2).strip()
    except HttpFetchError as e:
        logger.info("METAR fetch failed for %s: %s", icao, e)
        metar = ""

    try:
        taf = fetch_text(taf_url, timeout=5, retries=2).strip()
    except HttpFetchError as e:
        logger.info("TAF fetch failed for %s: %s", icao, e)
        taf = ""

    return metar if metar else "NIL", taf if taf else "NIL"


def fetch_mission_data(lat, lon, model_url):
    """
    Fetches tactical surface, absolute AGL boundaries, and 15-layer upper-air NWP data.

    CHANGELOG v2.1:
      - Added snow_depth to hourly_vars. Previously absent, which silently disabled
        the BLSN kinetic gate's snowpack depth trigger (has_snowpack was permanently
        False). The gate now correctly evaluates all three BLSN trigger conditions.
    """

    # Core surface, absolute AGL wind vectors, and thermodynamic variables.
    # snow_depth is in metres; the BLSN gate threshold is 0.05 m (5 cm).
    hourly_vars = (
        "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m,"
        "wind_speed_80m,wind_speed_120m,wind_speed_180m,"
        "wind_direction_80m,wind_direction_120m,wind_direction_180m,"
        "weather_code,visibility,freezing_level_height,"
        "precipitation_probability,precipitation,cape,boundary_layer_height,surface_pressure,"
        "snow_depth"
    )

    # 15-Layer Tactical Column
    p_levels = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300, 250, 200, 150]
    for p in p_levels:
        hourly_vars += (
            f",temperature_{p}hPa,relative_humidity_{p}hPa,"
            f"geopotential_height_{p}hPa,wind_speed_{p}hPa,wind_direction_{p}hPa"
        )

    # Explicit wind_speed_unit=kn — Open-Meteo serves all wind variables in
    # knots directly, eliminating the silent-conversion failure mode where
    # an unexpected unit (km/h, m/s, mph) gets treated as the wrong unit.
    # The detection logic at app.py is retained as a safety net.
    sep = "&" if "?" in model_url else "?"
    url = (
        f"{model_url}{sep}latitude={lat}&longitude={lon}"
        f"&hourly={hourly_vars}&elevation=nan&timezone=UTC"
        f"&wind_speed_unit=kn"
    )

    try:
        return fetch_json(url, timeout=15, retries=2)
    except HttpFetchError as e:
        logger.warning("Mission data fetch failed: %s", e)
        return {"error": True, "message": str(e)}


# =============================================================================
# PROVIDER DISPATCH / FAILOVER
# =============================================================================
# This is the multi-provider routing layer. Each model in the dashboard has
# a primary provider (typically Meteomatics) and an optional secondary
# (typically Open-Meteo). When the primary fails, we transparently fall
# back to the secondary and surface a structured status the caller can use
# to display an alert banner.
#
# The routing table itself lives in app.py — this module just executes a
# given route. That keeps the model-name → provider mapping with the UI
# concerns and keeps this module's contract focused on "given a route,
# fetch the data."

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProviderRoute:
    """A forecast routing instruction.

    Attributes:
        primary:           ("meteomatics", model_id) or ("open-meteo", url)
        fallback:          optional same-shape tuple for failover
        model_label:       human-readable model name (e.g. "ECMWF IFS")
                           used for status messages.
    """
    primary: tuple                       # (provider_name, target)
    fallback: Optional[tuple] = None
    model_label: str = ""


@dataclass
class ProviderFetchResult:
    """Result of a forecast fetch attempt with metadata about which provider
    actually served the data.

    Attributes:
        data:               the forecast payload (Open-Meteo shape) or empty dict
        served_by:          "meteomatics" / "open-meteo" / None on total failure
        ok:                 True if at least one provider succeeded
        attempted:          list of (provider, status) tuples in order tried
        primary_failed:     True if the primary provider failed (even if fallback succeeded)
        primary_error:      error message from primary if it failed, else None
    """
    data: dict = field(default_factory=dict)
    served_by: Optional[str] = None
    ok: bool = False
    attempted: list = field(default_factory=list)
    primary_failed: bool = False
    primary_error: Optional[str] = None


def _fetch_one(provider: str, target: str, lat: float, lon: float) -> dict:
    """Fetches forecast data from one provider. Returns the standard payload
    shape (with "_provider" key set) or an error dict.
    """
    if provider == "open-meteo":
        result = fetch_mission_data(lat, lon, target)
        # fetch_mission_data uses the legacy "error" key; preserve that
        if isinstance(result, dict) and not result.get("error"):
            result["_provider"] = "open-meteo"
        return result

    if provider == "meteomatics":
        # Lazy import to avoid hard dependency in places that don't use it
        from modules.meteomatics_provider import fetch_meteomatics_forecast
        return fetch_meteomatics_forecast(lat, lon, model=target)

    return {
        "error": True,
        "message": f"Unknown provider: {provider}",
        "_provider": provider,
    }


def fetch_forecast_with_fallback(route: ProviderRoute, lat: float, lon: float) -> ProviderFetchResult:
    """Executes a forecast fetch with provider failover.

    Tries the primary provider first. On failure, falls back to the secondary
    if one is defined. Records which provider actually served the data and
    whether the primary failed (so callers can show appropriate alerting).
    """
    result = ProviderFetchResult()

    # --- Primary attempt ---
    primary_provider, primary_target = route.primary
    data = _fetch_one(primary_provider, primary_target, lat, lon)
    if not data.get("error"):
        result.data = data
        result.served_by = primary_provider
        result.ok = True
        result.attempted.append((primary_provider, "ok"))
        return result

    # Primary failed
    result.primary_failed = True
    result.primary_error = data.get("message", "unknown error")
    result.attempted.append((primary_provider, result.primary_error))
    logger.warning(
        "Primary provider %s failed for %s: %s",
        primary_provider, route.model_label, result.primary_error,
    )

    # --- Fallback attempt ---
    if route.fallback is None:
        result.data = data       # carry error through so caller sees the message
        return result

    fb_provider, fb_target = route.fallback
    fb_data = _fetch_one(fb_provider, fb_target, lat, lon)
    if not fb_data.get("error"):
        result.data = fb_data
        result.served_by = fb_provider
        result.ok = True
        result.attempted.append((fb_provider, "ok-fallback"))
        return result

    # Both failed
    result.attempted.append((fb_provider, fb_data.get("message", "unknown")))
    result.data = fb_data     # surface the fallback error too
    logger.error(
        "Both providers failed for %s: primary=%s, fallback=%s",
        route.model_label, primary_provider, fb_provider,
    )
    return result
