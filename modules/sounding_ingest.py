"""
VECTOR CHECK AERIAL GROUP INC. — Drone Sounding Verification

Ingests vertical atmospheric profiles from meteo-drone ascents (e.g. the
Meteomatics MM-670 series payload) and verifies them against NWP model
output, layer by layer through the boundary layer.

Unlike Kestrel surface verification (a single point at the ground) or METAR
verification (a single surface station), a drone sounding is a CONTINUOUS
VERTICAL PROFILE — typically the lowest few hundred to few thousand feet,
sampled at high frequency during an ascent. This is the exact operating
envelope of tactical RPAS, which makes it the highest-value verification
source ARMS can ingest:

    - METAR verifies the model's surface value at one height.
    - A drone sounding verifies the model's VERTICAL STRUCTURE — whether it
      gets the surface right but the gradient wrong, a very common failure
      mode in stable boundary layers and near shorelines.

PIPELINE
    Drone CSV export → parse_sounding_csv()  → SoundingProfile
    SoundingProfile  → bin_profile_by_alt()  → list[SoundingLayer]
    SoundingLayer[]  + model_columns         → verify_profile()
                                             → ProfileVerification

CSV FORMAT (Meteomatics MM-670M, semicolon-delimited)
    Line 1: "<timestamp> UTC time;Aircraft: <tail>"
    Line 2: header row (22 columns)
    Line 3+: data rows

    Columns of interest:
        Datetime UTC, Latitude [deg], Longitude [deg], Altitude AMSL [ft],
        Temperature [dC], Dew Point [dC], Relative Humidity [perc],
        Air Pressure [hPa], Wind Speed [kts], Wind direction [deg],
        Wind gusts [kts]
    Plus turbulence structure (Cn2/Ct2), acoustics, refractivity — retained
    in the raw profile for future hazard products but not yet scored.
"""

from __future__ import annotations

import csv
import io
import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("arms.sounding")

# Models scored. Matches the surface scorecard model set so a sounding
# verification slots cleanly into the existing Model Performance page.
SOUNDING_MODELS = ["HRDPS", "GFS", "ECMWF", "ICON", "NAM", "HRRR", "MIX", "AIFS"]

# Default vertical bin size (feet AGL). 50 ft balances resolution against
# having enough drone samples per bin to form a stable mean. A typical
# MM-670 ascent at ~3 Hz produces dozens of samples per 50 ft bin.
DEFAULT_BIN_FT = 50.0

FT_PER_M = 3.28084


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class SoundingSample:
    """One high-frequency sample from a drone ascent."""
    timestamp: datetime
    alt_amsl_ft: float
    temp_c: float
    dewpoint_c: float
    rh: float
    pressure_hpa: float
    wind_speed_kt: float
    wind_dir_deg: float
    wind_gust_kt: float
    # Optional turbulence-structure fields (retained, not yet scored)
    cn2: Optional[float] = None
    ct2: Optional[float] = None


@dataclass
class SoundingLayer:
    """An altitude bin aggregating many drone samples into one observation.

    alt_agl_ft is the bin-center height above the launch surface; all model
    interpolation and MAE pairing happens against this height.
    """
    alt_agl_ft: float
    alt_amsl_ft: float
    n_samples: int
    temp_c: float
    dewpoint_c: float
    rh: float
    pressure_hpa: float
    wind_speed_kt: float
    wind_dir_deg: float
    wind_gust_kt: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SoundingProfile:
    """A parsed drone sounding: metadata + ordered samples."""
    lat: float
    lon: float
    launch_time: datetime
    aircraft: str
    samples: list = field(default_factory=list)

    @property
    def surface_amsl_ft(self) -> float:
        return min(s.alt_amsl_ft for s in self.samples) if self.samples else 0.0

    @property
    def top_amsl_ft(self) -> float:
        return max(s.alt_amsl_ft for s in self.samples) if self.samples else 0.0

    @property
    def span_ft(self) -> float:
        return self.top_amsl_ft - self.surface_amsl_ft

    @property
    def n_samples(self) -> int:
        return len(self.samples)

    @property
    def duration_s(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        return (self.samples[-1].timestamp - self.samples[0].timestamp).total_seconds()


@dataclass
class LayerVerification:
    """Per-model MAE for a single altitude layer."""
    alt_agl_ft: float
    model: str
    temp_mae: Optional[float] = None
    rh_mae: Optional[float] = None
    wind_mae: Optional[float] = None
    dir_mae: Optional[float] = None
    pressure_mae: Optional[float] = None
    # model interpolated values (for plotting the model profile alongside obs)
    model_temp: Optional[float] = None
    model_wind: Optional[float] = None
    model_dir: Optional[float] = None


@dataclass
class ProfileVerification:
    """Full result of verifying one sounding against all models."""
    lat: float
    lon: float
    launch_time: datetime
    aircraft: str
    n_layers: int
    bin_ft: float
    # Per-model aggregate MAE across the whole profile
    model_scores: dict = field(default_factory=dict)
    # Per-layer detail for charting (list of LayerVerification)
    layer_details: list = field(default_factory=list)
    # Best model by profile-mean composite
    best_model: Optional[str] = None
    # Surface-layer row, formatted for the Model Performance scorecard
    surface_scorecard_row: dict = field(default_factory=dict)


# =============================================================================
# PARSING
# =============================================================================

def parse_sounding_csv(csv_text: str) -> Optional[SoundingProfile]:
    """Parses a Meteomatics MM-670 drone sounding CSV.

    Returns a SoundingProfile, or None if the file can't be parsed. Tolerant
    of minor format drift: locates columns by fuzzy header match rather than
    fixed position so column re-ordering or added fields don't break it.
    """
    if not csv_text or not csv_text.strip():
        return None

    try:
        lines = csv_text.splitlines()
        if len(lines) < 3:
            logger.warning("Sounding CSV has fewer than 3 lines")
            return None

        # --- Metadata line (line 1) ---
        meta = lines[0].split(";")
        aircraft = "UNKNOWN"
        for part in meta:
            if "aircraft" in part.lower():
                aircraft = part.split(":", 1)[-1].strip()
                break

        # --- Header line (line 2) ---
        header = [h.strip() for h in lines[1].split(";")]

        def _col(keyword: str) -> int:
            kw = keyword.lower()
            for i, h in enumerate(header):
                if kw in h.lower():
                    return i
            return -1

        i_time = _col("datetime")
        i_lat = _col("latitude")
        i_lon = _col("longitude")
        i_alt = _col("altitude")
        i_temp = _col("temperature")
        i_dew = _col("dew")
        i_rh = _col("relative humidity")
        i_press = _col("air pressure")
        i_ws = _col("wind speed")
        i_wd = _col("wind direction")
        i_wg = _col("wind gust")
        i_cn2 = _col("cn2")
        i_ct2 = _col("ct2")

        # Required columns. Without these we can't verify anything.
        required = {
            "time": i_time, "lat": i_lat, "lon": i_lon, "alt": i_alt,
            "temp": i_temp, "rh": i_rh, "wind_speed": i_ws, "wind_dir": i_wd,
        }
        missing = [k for k, v in required.items() if v < 0]
        if missing:
            logger.warning("Sounding CSV missing required columns: %s", missing)
            return None

        samples: list = []
        lat_acc, lon_acc, n_pos = 0.0, 0.0, 0

        def _f(row: list, idx: int) -> Optional[float]:
            if idx < 0 or idx >= len(row):
                return None
            v = row[idx].strip()
            if not v:
                return None
            try:
                return float(v)
            except ValueError:
                return None

        for line in lines[2:]:
            if not line.strip():
                continue
            row = line.split(";")
            if len(row) <= i_alt:
                continue

            # Timestamp (ISO-ish: 2026-06-17T22:32:10.6)
            ts_raw = row[i_time].strip() if i_time < len(row) else ""
            ts = _parse_sounding_time(ts_raw)
            if ts is None:
                continue

            alt = _f(row, i_alt)
            temp = _f(row, i_temp)
            rh = _f(row, i_rh)
            ws = _f(row, i_ws)
            wd = _f(row, i_wd)
            if alt is None or temp is None or rh is None or ws is None or wd is None:
                continue

            lat_v = _f(row, i_lat)
            lon_v = _f(row, i_lon)
            if lat_v is not None and lon_v is not None:
                lat_acc += lat_v
                lon_acc += lon_v
                n_pos += 1

            samples.append(SoundingSample(
                timestamp=ts,
                alt_amsl_ft=alt,
                temp_c=temp,
                dewpoint_c=_f(row, i_dew) if i_dew >= 0 else 0.0,
                rh=rh,
                pressure_hpa=_f(row, i_press) if i_press >= 0 else 0.0,
                wind_speed_kt=ws,
                wind_dir_deg=wd % 360,
                wind_gust_kt=_f(row, i_wg) if i_wg >= 0 else ws,
                cn2=_f(row, i_cn2) if i_cn2 >= 0 else None,
                ct2=_f(row, i_ct2) if i_ct2 >= 0 else None,
            ))

        if not samples:
            logger.warning("Sounding CSV produced no valid samples")
            return None

        lat = lat_acc / n_pos if n_pos else 0.0
        lon = lon_acc / n_pos if n_pos else 0.0
        launch_time = samples[0].timestamp

        return SoundingProfile(
            lat=lat, lon=lon, launch_time=launch_time,
            aircraft=aircraft, samples=samples,
        )

    except Exception as e:
        logger.warning("Sounding CSV parse failed: %s", e)
        return None


def _parse_sounding_time(ts_raw: str) -> Optional[datetime]:
    """Parses the drone timestamp. Format observed: 2026-06-17T22:32:10.6
    (UTC, fractional seconds, no zone suffix). Falls back across a few forms.
    """
    if not ts_raw:
        return None
    candidates = [ts_raw, ts_raw.replace("Z", ""), ts_raw.split(".")[0]]
    for c in candidates:
        try:
            dt = datetime.fromisoformat(c)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


# =============================================================================
# BINNING
# =============================================================================

def bin_profile_by_alt(profile: SoundingProfile,
                        bin_ft: float = DEFAULT_BIN_FT) -> list:
    """Aggregates high-frequency samples into altitude bins.

    Each bin's value is the mean of its samples (vector mean for wind
    direction, to avoid the 359°/1° averaging trap). Bin center is reported
    as height AGL relative to the launch surface (lowest sample).

    Returns an altitude-ascending list of SoundingLayer.
    """
    if not profile.samples:
        return []

    surface = profile.surface_amsl_ft
    buckets: dict = {}

    for s in profile.samples:
        agl = s.alt_amsl_ft - surface
        bin_idx = int(agl // bin_ft)
        buckets.setdefault(bin_idx, []).append(s)

    layers: list = []
    for bin_idx in sorted(buckets.keys()):
        group = buckets[bin_idx]
        n = len(group)

        # Scalar means
        temp = sum(g.temp_c for g in group) / n
        dew = sum(g.dewpoint_c for g in group) / n
        rh = sum(g.rh for g in group) / n
        press = sum(g.pressure_hpa for g in group) / n
        ws = sum(g.wind_speed_kt for g in group) / n
        wg = sum(g.wind_gust_kt for g in group) / n
        amsl = sum(g.alt_amsl_ft for g in group) / n

        # Vector mean for direction
        sin_sum = sum(math.sin(math.radians(g.wind_dir_deg)) for g in group)
        cos_sum = sum(math.cos(math.radians(g.wind_dir_deg)) for g in group)
        wd = math.degrees(math.atan2(sin_sum, cos_sum)) % 360

        layers.append(SoundingLayer(
            alt_agl_ft=(bin_idx + 0.5) * bin_ft,
            alt_amsl_ft=amsl,
            n_samples=n,
            temp_c=round(temp, 2),
            dewpoint_c=round(dew, 2),
            rh=round(rh, 1),
            pressure_hpa=round(press, 1),
            wind_speed_kt=round(ws, 1),
            wind_dir_deg=round(wd, 1),
            wind_gust_kt=round(wg, 1),
        ))

    return layers


# =============================================================================
# MODEL INTERPOLATION
# =============================================================================

def _shortest_arc(a: float, b: float) -> float:
    """Smallest absolute angular difference between two bearings (degrees)."""
    d = abs((a - b) % 360)
    return min(d, 360 - d)


def interpolate_model_to_height(model_col: dict, target_agl_ft: float,
                                 surface_amsl_ft: float) -> dict:
    """Interpolates a model's vertical column to a target height AGL.

    model_col is expected to provide surface values plus optional pressure-
    level values, in this shape:
        {
          "surface": {"temp_c":.., "rh":.., "wind_kt":.., "dir":.., "press_hpa":..},
          "levels": [
             {"height_ft_amsl":.., "temp_c":.., "rh":.., "wind_kt":.., "dir":..},
             ...
          ]
        }

    Returns a dict of interpolated values at target height, or surface values
    if no pressure-level data is available (graceful degradation).
    """
    surface = model_col.get("surface", {})
    levels = model_col.get("levels", [])

    target_amsl = surface_amsl_ft + target_agl_ft

    # Build an ascending list of (amsl_ft, values) anchored at the surface.
    anchors = [(surface_amsl_ft, {
        "temp_c": surface.get("temp_c"),
        "rh": surface.get("rh"),
        "wind_kt": surface.get("wind_kt"),
        "dir": surface.get("dir"),
        "press_hpa": surface.get("press_hpa"),
    })]
    for lv in levels:
        h = lv.get("height_ft_amsl")
        if h is not None and h > surface_amsl_ft:
            anchors.append((h, {
                "temp_c": lv.get("temp_c"),
                "rh": lv.get("rh"),
                "wind_kt": lv.get("wind_kt"),
                "dir": lv.get("dir"),
                "press_hpa": lv.get("press_hpa"),
            }))
    anchors.sort(key=lambda x: x[0])

    # Below the lowest anchor or only one anchor → use surface values
    if len(anchors) < 2 or target_amsl <= anchors[0][0]:
        return dict(anchors[0][1])
    if target_amsl >= anchors[-1][0]:
        return dict(anchors[-1][1])

    # Find bracketing anchors and linearly interpolate
    for i in range(len(anchors) - 1):
        h0, v0 = anchors[i]
        h1, v1 = anchors[i + 1]
        if h0 <= target_amsl <= h1:
            frac = (target_amsl - h0) / (h1 - h0) if h1 != h0 else 0.0
            out = {}
            for key in ["temp_c", "rh", "wind_kt", "press_hpa"]:
                a, b = v0.get(key), v1.get(key)
                out[key] = (a + frac * (b - a)) if (a is not None and b is not None) else (a if a is not None else b)
            # Direction: interpolate along the shortest arc
            d0, d1 = v0.get("dir"), v1.get("dir")
            if d0 is not None and d1 is not None:
                delta = ((d1 - d0 + 180) % 360) - 180
                out["dir"] = (d0 + frac * delta) % 360
            else:
                out["dir"] = d0 if d0 is not None else d1
            return out

    return dict(anchors[-1][1])


# =============================================================================
# VERIFICATION
# =============================================================================

def verify_profile(layers: list, model_columns: dict,
                   surface_amsl_ft: float,
                   profile: Optional[SoundingProfile] = None,
                   bin_ft: float = DEFAULT_BIN_FT) -> ProfileVerification:
    """Verifies binned drone layers against each model's vertical column.

    model_columns: { "ECMWF": <model_col dict>, "GFS": <...>, ... }
                   each model_col is the shape interpolate_model_to_height expects.

    Returns a ProfileVerification with per-model aggregate MAE, per-layer
    detail for charting, the best model, and a surface scorecard row.
    """
    # Accumulators per model
    acc: dict = {m: {"temp": [], "rh": [], "wind": [], "dir": [], "press": []}
                 for m in model_columns.keys()}
    layer_details: list = []

    for layer in layers:
        for model, col in model_columns.items():
            interp = interpolate_model_to_height(col, layer.alt_agl_ft, surface_amsl_ft)

            lv = LayerVerification(alt_agl_ft=layer.alt_agl_ft, model=model)

            if interp.get("temp_c") is not None:
                lv.temp_mae = abs(layer.temp_c - interp["temp_c"])
                lv.model_temp = interp["temp_c"]
                acc[model]["temp"].append(lv.temp_mae)
            if interp.get("rh") is not None:
                lv.rh_mae = abs(layer.rh - interp["rh"])
                acc[model]["rh"].append(lv.rh_mae)
            if interp.get("wind_kt") is not None:
                lv.wind_mae = abs(layer.wind_speed_kt - interp["wind_kt"])
                lv.model_wind = interp["wind_kt"]
                acc[model]["wind"].append(lv.wind_mae)
            if interp.get("dir") is not None and layer.wind_speed_kt >= 3.0:
                # Skip direction scoring in near-calm (direction is meaningless)
                lv.dir_mae = _shortest_arc(layer.wind_dir_deg, interp["dir"])
                lv.model_dir = interp["dir"]
                acc[model]["dir"].append(lv.dir_mae)
            if interp.get("press_hpa") is not None and layer.pressure_hpa > 0:
                lv.pressure_mae = abs(layer.pressure_hpa - interp["press_hpa"])
                acc[model]["press"].append(lv.pressure_mae)

            layer_details.append(lv)

    # Aggregate per-model MAE (mean across layers)
    def _mean(xs):
        return round(sum(xs) / len(xs), 2) if xs else None

    model_scores: dict = {}
    for model, a in acc.items():
        model_scores[model] = {
            "temp_mae": _mean(a["temp"]),
            "rh_mae": _mean(a["rh"]),
            "wind_mae": _mean(a["wind"]),
            "dir_mae": _mean(a["dir"]),
            "pressure_mae": _mean(a["press"]),
            "n_layers": len(a["temp"]),
        }

    # Best model by the same UAS-operational composite the surface scorecard
    # uses: wind ×3 + temp ×1 + press ×0.5 + dir ×0.05. (No gust at altitude.)
    def _composite(s: dict) -> float:
        score = 0.0
        if s["wind_mae"] is not None:  score += s["wind_mae"] * 3.0
        if s["temp_mae"] is not None:  score += s["temp_mae"] * 1.0
        if s["pressure_mae"] is not None: score += s["pressure_mae"] * 0.5
        if s["dir_mae"] is not None:   score += s["dir_mae"] * 0.05
        if s["rh_mae"] is not None:    score += s["rh_mae"] * 0.05
        return score if any(s[k] is not None for k in ["wind_mae", "temp_mae"]) else float("inf")

    scored = [(m, _composite(s)) for m, s in model_scores.items()]
    scored = [(m, c) for m, c in scored if c != float("inf")]
    best_model = min(scored, key=lambda x: x[1])[0] if scored else None

    # Surface scorecard row: the lowest layer, formatted to match the
    # Model Performance scorecard's expected per-model dict.
    surface_row: dict = {}
    if layers:
        lowest = layers[0]
        surface_row = {
            "alt_agl_ft": round(lowest.alt_agl_ft, 0),
            "obs": {
                "temp_c": lowest.temp_c,
                "rh": lowest.rh,
                "wind_kt": lowest.wind_speed_kt,
                "dir": lowest.wind_dir_deg,
                "gust_kt": lowest.wind_gust_kt,
                "press_hpa": lowest.pressure_hpa,
            },
            "per_model": {},
        }
        for model, col in model_columns.items():
            interp = interpolate_model_to_height(col, lowest.alt_agl_ft, surface_amsl_ft)
            surface_row["per_model"][model] = {
                "temp_mae": round(abs(lowest.temp_c - interp["temp_c"]), 2) if interp.get("temp_c") is not None else None,
                "wind_mae": round(abs(lowest.wind_speed_kt - interp["wind_kt"]), 2) if interp.get("wind_kt") is not None else None,
                "dir_mae": round(_shortest_arc(lowest.wind_dir_deg, interp["dir"]), 0) if (interp.get("dir") is not None and lowest.wind_speed_kt >= 3.0) else None,
            }

    return ProfileVerification(
        lat=profile.lat if profile else 0.0,
        lon=profile.lon if profile else 0.0,
        launch_time=profile.launch_time if profile else datetime.now(timezone.utc),
        aircraft=profile.aircraft if profile else "UNKNOWN",
        n_layers=len(layers),
        bin_ft=bin_ft,
        model_scores=model_scores,
        layer_details=layer_details,
        best_model=best_model,
        surface_scorecard_row=surface_row,
    )


# =============================================================================
# MODEL COLUMN FETCHING
# =============================================================================
# Fetches surface + low-level pressure-level data for each model at the
# drone's launch coordinate and the forecast hour nearest the launch time.
# A drone profile this shallow (typically < 1500 ft AGL) lives between the
# surface and the 925 hPa level, so we fetch surface + 1000 hPa + 925 hPa and
# interpolate by geopotential height. Deeper profiles automatically pick up
# 850/700 hPa too.

# Pressure levels to request, shallow→deep. 1000/925 bracket most RPAS
# ascents; 850/700 cover extended-trajectory soundings.
_SOUNDING_P_LEVELS = [1000, 925, 850, 700]


def _nearest_hour_index(times_iso: list, target: datetime) -> int:
    """Index of the forecast hour closest to the drone launch time."""
    best_i, best_dt = 0, None
    for i, t in enumerate(times_iso):
        try:
            ft = datetime.fromisoformat(t.replace("Z", ""))
            if ft.tzinfo is None:
                ft = ft.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            continue
        diff = abs((ft - target).total_seconds())
        if best_dt is None or diff < best_dt:
            best_dt, best_i = diff, i
    return best_i


def _build_open_meteo_column(model_slug: str, lat: float, lon: float,
                              launch_time: datetime) -> Optional[dict]:
    """Fetches one Open-Meteo model's surface + pressure-level column."""
    try:
        from modules.open_meteo_endpoints import build_url
        from modules.http_client import fetch_json
    except ImportError:
        return None

    surface_vars = ["temperature_2m", "relative_humidity_2m",
                    "wind_speed_10m", "wind_direction_10m",
                    "wind_gusts_10m", "surface_pressure"]
    level_vars = []
    for p in _SOUNDING_P_LEVELS:
        level_vars += [f"temperature_{p}hPa", f"relative_humidity_{p}hPa",
                       f"wind_speed_{p}hPa", f"wind_direction_{p}hPa",
                       f"geopotential_height_{p}hPa"]
    hourly = ",".join(surface_vars + level_vars)

    date_str = launch_time.strftime("%Y-%m-%d")
    suffix = (f"latitude={lat:.4f}&longitude={lon:.4f}&hourly={hourly}"
              f"&wind_speed_unit=kn&start_date={date_str}&end_date={date_str}"
              f"&timezone=UTC")
    url = build_url(model_slug, suffix)

    try:
        payload = fetch_json(url, timeout=20, retries=2)
    except Exception as e:
        logger.warning("Open-Meteo column fetch failed for %s: %s", model_slug, e)
        return None

    hb = payload.get("hourly") or {}
    times = hb.get("time") or []
    if not times:
        return None
    idx = _nearest_hour_index(times, launch_time)

    def _at(key):
        arr = hb.get(key)
        if arr and len(arr) > idx and arr[idx] is not None:
            return float(arr[idx])
        return None

    surface = {
        "temp_c": _at("temperature_2m"),
        "rh": _at("relative_humidity_2m"),
        "wind_kt": _at("wind_speed_10m"),
        "dir": _at("wind_direction_10m"),
        "press_hpa": _at("surface_pressure"),
    }
    levels = []
    for p in _SOUNDING_P_LEVELS:
        gh_m = _at(f"geopotential_height_{p}hPa")
        if gh_m is None:
            continue
        levels.append({
            "height_ft_amsl": gh_m * FT_PER_M,
            "temp_c": _at(f"temperature_{p}hPa"),
            "rh": _at(f"relative_humidity_{p}hPa"),
            "wind_kt": _at(f"wind_speed_{p}hPa"),
            "dir": _at(f"wind_direction_{p}hPa"),
            "press_hpa": float(p),
        })
    return {"surface": surface, "levels": levels}


def _build_meteomatics_column(model_id: str, lat: float, lon: float,
                               launch_time: datetime) -> Optional[dict]:
    """Fetches one Meteomatics model's surface + pressure-level column."""
    try:
        from modules.meteomatics_provider import (
            METEOMATICS_BASE, _get_credentials, _MODEL_PARAM_BLOCKLIST,
        )
        from modules.http_client import fetch_json
    except ImportError:
        return None

    creds = _get_credentials()
    if creds is None:
        return None

    validdate = launch_time.strftime("%Y-%m-%dT%H:%M:%SZ")

    surface_params = ["t_2m:C", "relative_humidity_2m:p",
                      "wind_speed_10m:kn", "wind_dir_10m:d", "msl_pressure:hPa"]
    level_params = []
    for p in _SOUNDING_P_LEVELS:
        level_params += [f"t_{p}hPa:C", f"relative_humidity_{p}hPa:p",
                         f"wind_speed_{p}hPa:kn", f"wind_dir_{p}hPa:d",
                         f"geopotential_height_{p}hPa:m"]

    # Filter blocklisted params for this model (per-model 404 avoidance).
    block = _MODEL_PARAM_BLOCKLIST.get(model_id, set())
    all_params = [p for p in (surface_params + level_params) if p not in block]
    param_str = ",".join(all_params)

    url = f"{METEOMATICS_BASE}/{validdate}/{param_str}/{lat:.4f},{lon:.4f}/json?model={model_id}"

    try:
        from modules.meteomatics_provider import _mm_fetch_json as _mmfj
        payload = _mmfj(url, timeout=12, retries=1, basic_auth=creds)
    except Exception as e:
        logger.warning("Meteomatics column fetch failed for %s: %s", model_id, e)
        return None

    # Meteomatics returns one block per parameter with a single coordinate,
    # single date. Flatten into a param→value dict.
    pv: dict = {}
    for block_d in (payload.get("data") or []):
        param = block_d.get("parameter")
        coords = block_d.get("coordinates") or []
        if not coords:
            continue
        dates = coords[0].get("dates") or []
        if dates and dates[0].get("value") is not None:
            try:
                pv[param] = float(dates[0]["value"])
            except (TypeError, ValueError):
                pass

    surface = {
        "temp_c": pv.get("t_2m:C"),
        "rh": pv.get("relative_humidity_2m:p"),
        "wind_kt": pv.get("wind_speed_10m:kn"),
        "dir": pv.get("wind_dir_10m:d"),
        "press_hpa": pv.get("msl_pressure:hPa"),
    }
    levels = []
    for p in _SOUNDING_P_LEVELS:
        gh = pv.get(f"geopotential_height_{p}hPa:m")
        if gh is None:
            continue
        levels.append({
            "height_ft_amsl": gh * FT_PER_M,
            "temp_c": pv.get(f"t_{p}hPa:C"),
            "rh": pv.get(f"relative_humidity_{p}hPa:p"),
            "wind_kt": pv.get(f"wind_speed_{p}hPa:kn"),
            "dir": pv.get(f"wind_dir_{p}hPa:d"),
            "press_hpa": float(p),
        })
    return {"surface": surface, "levels": levels}


def fetch_all_model_columns(lat: float, lon: float, launch_time: datetime,
                             in_conus: bool = True) -> dict:
    """Fetches surface+pressure-level columns for all 8 scorecard models in
    parallel, routed through each model's best provider (mirrors the routing
    in ensemble_analysis.MODEL_ROUTES).

    Returns { "ECMWF": <column>, "GFS": <column>, ... } omitting any model
    that failed or returned no usable data.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    try:
        from modules.ensemble_analysis import MODEL_ROUTES
    except ImportError:
        MODEL_ROUTES = {}

    # Build the fetch plan: (display_name, callable)
    plan: list = []
    for name in SOUNDING_MODELS:
        route = MODEL_ROUTES.get(name)
        if route is None:
            continue
        source, target = route
        # CONUS-only models are skipped outside CONUS coverage
        if name in ("HRRR", "NAM") and not in_conus:
            continue
        if source == "meteomatics":
            plan.append((name, lambda t=target: _build_meteomatics_column(t, lat, lon, launch_time)))
        elif source == "open-meteo":
            plan.append((name, lambda t=target: _build_open_meteo_column(t, lat, lon, launch_time)))

    columns: dict = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fn): name for name, fn in plan}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                col = fut.result()
            except Exception as e:
                logger.warning("Column fetch errored for %s: %s", name, e)
                col = None
            # Keep only columns with at least a surface temp or wind
            if col and col.get("surface") and (
                col["surface"].get("temp_c") is not None or
                col["surface"].get("wind_kt") is not None
            ):
                columns[name] = col

    return columns


def verify_sounding_csv(csv_text: str, in_conus: bool = True,
                        bin_ft: float = DEFAULT_BIN_FT) -> Optional[ProfileVerification]:
    """End-to-end convenience: CSV text → full ProfileVerification.

    Parses, bins, fetches all model columns at the drone's location/time, and
    verifies. Returns None if the CSV can't be parsed.
    """
    profile = parse_sounding_csv(csv_text)
    if profile is None:
        return None

    layers = bin_profile_by_alt(profile, bin_ft=bin_ft)
    if not layers:
        return None

    columns = fetch_all_model_columns(
        profile.lat, profile.lon, profile.launch_time, in_conus=in_conus
    )
    if not columns:
        # Return a verification with no model scores rather than None, so the
        # UI can still show the observed profile and explain the fetch failure.
        return ProfileVerification(
            lat=profile.lat, lon=profile.lon, launch_time=profile.launch_time,
            aircraft=profile.aircraft, n_layers=len(layers), bin_ft=bin_ft,
            model_scores={}, layer_details=[], best_model=None,
            surface_scorecard_row={},
        )

    return verify_profile(
        layers, columns, profile.surface_amsl_ft,
        profile=profile, bin_ft=bin_ft,
    )
