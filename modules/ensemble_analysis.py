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

MODEL_ENDPOINTS = {
    "HRDPS": "https://api.open-meteo.com/v1/gem",
    "GFS":   "https://api.open-meteo.com/v1/gfs",
    "ECMWF": "https://api.open-meteo.com/v1/ecmwf",
    "ICON":  "https://api.open-meteo.com/v1/dwd-icon",
    # CONUS-only NCEP mesoscale models served via Open-Meteo's /v1/gfs endpoint.
    # The &models= parameter targets the specific NWP system; outside CONUS
    # these endpoints fall back to GFS-13km, which would duplicate our GFS
    # member, so we gate inclusion on CONUS coverage.
    "HRRR":  "https://api.open-meteo.com/v1/gfs?models=ncep_hrrr_conus",
    "NAM":   "https://api.open-meteo.com/v1/gfs?models=ncep_nam_conus",
}

# Regional high-resolution model swap-ins when outside primary coverage.
# These replace HRDPS in the ensemble when the query point is outside Canada.
REGIONAL_MODELS = {
    # Europe — DWD ICON-EU and Meteo-France ARPEGE-Europe are both accurate at regional scale
    # (~7 km). We use ICON-EU because it's the most-cited skilful model for Europe.
    "icon_eu":   "https://api.open-meteo.com/v1/dwd-icon",
    # Pacific / Australia — BOM ACCESS-G at 12 km, global coverage with Australia focus
    "access_g":  "https://api.open-meteo.com/v1/bom",
    # Generic global best-match — Open-Meteo auto-selects the highest-resolution
    # model available for the location. This is the universal fallback.
    "best":      "https://api.open-meteo.com/v1/forecast",
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
    "precipitation_probability,weather_code"
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

    # If the endpoint URL already contains a query string (e.g. "?models=..."),
    # use & to append our parameters; otherwise use ?
    sep = "&" if "?" in url else "?"
    full_url = (
        f"{url}{sep}latitude={lat}&longitude={lon}"
        f"&hourly={_HOURLY_VARS}&timezone=UTC&forecast_days=4"
    )

    try:
        req = urllib.request.Request(full_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning("Ensemble fetch failed for %s: %s", name, e)
        return mf

    h = data.get("hourly")
    if not h or "time" not in h:
        return mf

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

    mf.wind_kt = _safe_list("wind_speed_10m", KMH_TO_KT)
    mf.wind_dir = _safe_list("wind_direction_10m")
    mf.gust_kt = _safe_list("wind_gusts_10m", KMH_TO_KT)
    mf.temp_c = _safe_list("temperature_2m")
    mf.rh = _safe_list("relative_humidity_2m")
    mf.pressure_hpa = _safe_list("surface_pressure")
    mf.precip_prob = _safe_list("precipitation_probability")
    mf.wx_code = _safe_list("weather_code")
    mf.valid = len(mf.wind_kt) >= 24

    return mf


def fetch_all_models(lat: float, lon: float) -> list:
    """Fetches the active ensemble for this location. Returns list of valid
    ModelForecast objects.

    Ensemble composition:
      - 1 regional high-res model (HRDPS / ICON-EU / ACCESS-G / Best Match)
      - 3 global models (GFS, ECMWF, ICON)
      - 2 additional CONUS-specific models (NAM, HRRR) if site is in CONUS

    For Canadian sites the typical ensemble size is 4. For CONUS sites it
    grows to 5-6 (HRDPS over the northern CONUS strip + HRRR/NAM).
    """
    regional_name, regional_url = _select_regional_model(lat, lon)

    active_endpoints = {
        regional_name: regional_url,
        "GFS":   MODEL_ENDPOINTS["GFS"],
        "ECMWF": MODEL_ENDPOINTS["ECMWF"],
        "ICON":  MODEL_ENDPOINTS["ICON"],
    }

    # Add CONUS-specific high-res mesoscale models if the site is in CONUS.
    # NAM 3km has a longer forecast horizon (60h) than HRRR (48h) but lower
    # update cadence (4×/day vs hourly).
    if _is_conus_coverage(lat, lon):
        active_endpoints["NAM"] = MODEL_ENDPOINTS["NAM"]
        active_endpoints["HRRR"] = MODEL_ENDPOINTS["HRRR"]

    results = []
    for name, url in active_endpoints.items():
        mf = _fetch_model(name, url, lat, lon)
        if mf.valid:
            results.append(mf)
    return results


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
