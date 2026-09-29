"""
VECTOR CHECK AERIAL GROUP INC. — Forecast Verification Engine

Compares Kestrel 5500 Fire Weather Pro ground-truth observations against
the ARMS NWP forecast that was active for the same location and time.
Produces per-variable deltas and a single Model Verification Score (MVS).

DATA FLOW:
    Kestrel CSV → kestrel_ingest.parse_kestrel_csv()
                → average_session()           ← this module
                → match_forecast_hour()       ← this module
                → compute_verification()      ← this module
                → store in Supabase           ← this module
                → dashboard display           ← app.py

MVS SCORING:
    Weighted penalty system biased toward wind (the primary UAS risk driver).
    Each variable deducts from 100 based on absolute error scaled by
    operational significance. Score ≥ 85 = excellent, ≥ 70 = good,
    ≥ 50 = fair, < 50 = poor.

SUPABASE TABLES:
    kestrel_sessions         — one row per upload (session metadata + averages)
    forecast_verifications   — one row per session (paired fcst vs actual + MVS)
"""

import math
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger("arms.verification")


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class SessionSummary:
    """Averaged measurements from a single Kestrel logging session."""
    timestamp_start: datetime = None
    timestamp_end: datetime = None
    duration_seconds: int = 0
    sample_count: int = 0

    wind_speed_kt: float = 0.0
    wind_dir_true: float = 0.0      # vector-averaged, corrected to true north
    temp_c: float = 0.0
    rh: float = 0.0
    pressure_hpa: float = 0.0
    density_alt_ft: int = 0
    dewpoint_c: float = 0.0

    device_serial: str = ""
    file_hash: str = ""              # SHA-256 of uploaded file (dedup)


@dataclass
class VerificationResult:
    """Complete forecast vs actual comparison for one Kestrel session."""
    # Identification
    session_hash: str = ""
    operator: str = ""
    lat: float = 0.0
    lon: float = 0.0
    timestamp: datetime = None
    model_name: str = ""
    lead_time_hours: int = 0         # how far ahead the forecast was

    # Paired values — forecast
    fcst_wind_kt: float = 0.0
    fcst_wind_dir: float = 0.0
    fcst_temp_c: float = 0.0
    fcst_rh: float = 0.0
    fcst_pressure_hpa: float = 0.0
    fcst_density_alt_ft: int = 0

    # Paired values — actual (Kestrel)
    actual_wind_kt: float = 0.0
    actual_wind_dir: float = 0.0
    actual_temp_c: float = 0.0
    actual_rh: float = 0.0
    actual_pressure_hpa: float = 0.0
    actual_density_alt_ft: int = 0

    # Deltas (forecast minus actual; positive = over-predicted)
    delta_wind_kt: float = 0.0
    delta_wind_dir: float = 0.0      # shortest-arc degrees
    delta_temp_c: float = 0.0
    delta_rh: float = 0.0
    delta_pressure_hpa: float = 0.0
    delta_density_alt_ft: int = 0

    # Scoring
    mvs: int = 100                   # Model Verification Score (0-100)
    grade: str = "A"                 # A/B/C/F
    assessment: str = ""             # human-readable
    flags: list = field(default_factory=list)   # notable deviations

    # Session metadata
    sample_count: int = 0
    duration_seconds: int = 0


# =============================================================================
# SESSION AVERAGING
# =============================================================================

def average_session(observations: list, magnetic_declination: float = 0.0) -> SessionSummary:
    """Computes session-averaged measurements from a list of KestrelObservation objects.

    Wind direction is vector-averaged (not scalar-averaged) to handle
    wrap-around at 360°/0° correctly. All other variables are arithmetic means.

    Args:
        observations: list of KestrelObservation from kestrel_ingest.parse_kestrel_csv()
        magnetic_declination: degrees to add for mag→true conversion (if not already applied)

    Returns:
        SessionSummary with averaged values and session metadata
    """
    if not observations:
        return SessionSummary()

    n = len(observations)
    summary = SessionSummary(
        timestamp_start=observations[0].timestamp,
        timestamp_end=observations[-1].timestamp,
        sample_count=n,
    )

    # Duration
    dt = (summary.timestamp_end - summary.timestamp_start).total_seconds()
    summary.duration_seconds = max(0, int(dt))

    # Scalar averages
    summary.temp_c = round(sum(o.temp_c for o in observations) / n, 1)
    summary.rh = round(sum(o.rh for o in observations) / n, 1)
    summary.pressure_hpa = round(sum(o.station_pressure_hpa for o in observations) / n, 1)
    summary.density_alt_ft = int(sum(o.density_alt_ft for o in observations) / n)
    summary.dewpoint_c = round(sum(o.dewpoint_c for o in observations) / n, 1)
    summary.wind_speed_kt = round(sum(o.wind_speed_kt for o in observations) / n, 1)

    # Vector-averaged wind direction (handles 359°/1° correctly)
    sin_sum = sum(math.sin(math.radians(o.wind_dir_true)) for o in observations)
    cos_sum = sum(math.cos(math.radians(o.wind_dir_true)) for o in observations)
    avg_dir = math.degrees(math.atan2(sin_sum / n, cos_sum / n)) % 360
    summary.wind_dir_true = round(avg_dir, 0)

    return summary


def compute_file_hash(file_bytes: bytes) -> str:
    """SHA-256 hash for deduplication."""
    return hashlib.sha256(file_bytes).hexdigest()[:16]


# =============================================================================
# FORECAST MATCHING
# =============================================================================

def match_forecast_hour(
    session: SessionSummary,
    forecast_times: list,
    forecast_data: dict,
) -> dict:
    """Finds the forecast hour closest to the Kestrel session midpoint.

    Args:
        session: SessionSummary with timestamp_start and timestamp_end
        forecast_times: list of ISO time strings from h["time"]
        forecast_data: the full hourly dict from fetch_mission_data()

    Returns:
        dict with matched forecast values and metadata, or None if no match
    """
    if not session.timestamp_start or not forecast_times:
        return None

    # Session midpoint
    mid_ts = session.timestamp_start.timestamp()
    if session.timestamp_end:
        mid_ts = (session.timestamp_start.timestamp() + session.timestamp_end.timestamp()) / 2

    # Find closest forecast hour
    best_idx = 0
    best_diff = float("inf")

    for i, time_str in enumerate(forecast_times):
        try:
            fcst_ts = datetime.fromisoformat(time_str).replace(tzinfo=timezone.utc).timestamp()
        except (ValueError, TypeError):
            continue

        diff = abs(fcst_ts - mid_ts)
        if diff < best_diff:
            best_diff = diff
            best_idx = i

    # Reject matches more than 90 minutes away
    if best_diff > 5400:
        return None

    h = forecast_data
    lead_time_h = int(best_diff / 3600)

    def _fv_at(src: dict, key: str, idx: int):
        """Safe hourly read returning a float or None."""
        series = src.get(key)
        if not series or idx >= len(series):
            return None
        val = series[idx]
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    try:
        return {
            "index": best_idx,
            "time": forecast_times[best_idx],
            "lead_time_hours": lead_time_h,
            # Per-field safe reads. The previous `h.get(k,[0])[best_idx]`
            # pattern raised IndexError whenever a field was absent and
            # best_idx > 0, and the surrounding handler then discarded the
            # ENTIRE verification pairing — losing four good variables
            # because one was missing. Each field now degrades on its own.
            "wind_speed_kt": (_fv_at(h, "wind_speed_10m", best_idx) or 0.0) * 0.539957,
            "wind_dir": _fv_at(h, "wind_direction_10m", best_idx) or 0.0,
            "temp_c": _fv_at(h, "temperature_2m", best_idx) or 0.0,
            "rh": _fv_at(h, "relative_humidity_2m", best_idx) or 0.0,
            "pressure_hpa": _fv_at(h, "surface_pressure", best_idx) or 0.0,
        }
    except (IndexError, TypeError, ValueError) as e:
        logger.warning("Forecast match failed at index %d: %s", best_idx, e)
        return None


# =============================================================================
# DELTA & MVS COMPUTATION
# =============================================================================

def _shortest_arc(a: float, b: float) -> float:
    """Shortest angular distance between two bearings, signed."""
    d = (a - b + 180) % 360 - 180
    return d


def compute_verification(
    session: SessionSummary,
    forecast: dict,
    elevation_ft: float,
    operator: str = "",
    lat: float = 0.0,
    lon: float = 0.0,
    model_name: str = "",
) -> VerificationResult:
    """Computes the full forecast-vs-actual verification for one session.

    Args:
        session: averaged Kestrel measurements
        forecast: matched forecast values from match_forecast_hour()
        elevation_ft: site elevation for DA computation
        operator: operator name for tagging
        lat, lon: site coordinates
        model_name: NWP model identifier (e.g. "GFS", "ICON")

    Returns:
        VerificationResult with all deltas, MVS, grade, and flags
    """
    from modules.physics import calculate_density_altitude

    # Compute forecast DA from matched temp + pressure
    fcst_da = calculate_density_altitude(
        elevation_ft, forecast["temp_c"], forecast["pressure_hpa"]
    )

    vr = VerificationResult(
        session_hash=session.file_hash,
        operator=operator,
        lat=lat, lon=lon,
        timestamp=session.timestamp_start,
        model_name=model_name,
        lead_time_hours=forecast.get("lead_time_hours", 0),
        sample_count=session.sample_count,
        duration_seconds=session.duration_seconds,

        fcst_wind_kt=round(forecast["wind_speed_kt"], 1),
        fcst_wind_dir=round(forecast["wind_dir"], 0),
        fcst_temp_c=round(forecast["temp_c"], 1),
        fcst_rh=round(forecast["rh"], 0),
        fcst_pressure_hpa=round(forecast["pressure_hpa"], 1),
        fcst_density_alt_ft=fcst_da,

        actual_wind_kt=session.wind_speed_kt,
        actual_wind_dir=session.wind_dir_true,
        actual_temp_c=session.temp_c,
        actual_rh=session.rh,
        actual_pressure_hpa=session.pressure_hpa,
        actual_density_alt_ft=session.density_alt_ft,
    )

    # --- Deltas (forecast minus actual; positive = over-predicted) ---
    vr.delta_wind_kt = round(vr.fcst_wind_kt - vr.actual_wind_kt, 1)
    vr.delta_wind_dir = round(_shortest_arc(vr.fcst_wind_dir, vr.actual_wind_dir), 0)
    vr.delta_temp_c = round(vr.fcst_temp_c - vr.actual_temp_c, 1)
    vr.delta_rh = round(vr.fcst_rh - vr.actual_rh, 0)
    vr.delta_pressure_hpa = round(vr.fcst_pressure_hpa - vr.actual_pressure_hpa, 1)
    vr.delta_density_alt_ft = vr.fcst_density_alt_ft - vr.actual_density_alt_ft

    # --- Model Verification Score ---
    # Weighted penalty: wind is the primary UAS risk driver
    penalties = 0.0

    # Wind speed: 4 pts per kt error, max 35 (most critical)
    penalties += min(35, abs(vr.delta_wind_kt) * 4)

    # Wind direction: 0.3 pts per degree, max 15
    penalties += min(15, abs(vr.delta_wind_dir) * 0.3)

    # Temperature: 2 pts per °C, max 20
    penalties += min(20, abs(vr.delta_temp_c) * 2)

    # Pressure: 3 pts per hPa, max 10
    penalties += min(10, abs(vr.delta_pressure_hpa) * 3)

    # RH: 0.15 pts per %, max 10
    penalties += min(10, abs(vr.delta_rh) * 0.15)

    # DA: 0.5 pts per 100 ft, max 10
    penalties += min(10, abs(vr.delta_density_alt_ft) / 100 * 0.5)

    vr.mvs = max(0, min(100, int(100 - penalties)))

    # --- Grade ---
    if vr.mvs >= 85:
        vr.grade = "A"
        vr.assessment = "Forecast closely matched observed conditions."
    elif vr.mvs >= 70:
        vr.grade = "B"
        vr.assessment = "Minor deviations. Forecast operationally valid."
    elif vr.mvs >= 50:
        vr.grade = "C"
        vr.assessment = "Significant deviations detected."
    else:
        vr.grade = "F"
        vr.assessment = "Model divergence exceeds operational thresholds."

    # --- Flags for notable deviations ---
    if abs(vr.delta_wind_kt) >= 5:
        bias = "under" if vr.delta_wind_kt < 0 else "over"
        vr.flags.append(f"Wind {bias}-forecast by {abs(vr.delta_wind_kt):.0f} kt")

    if abs(vr.delta_wind_dir) >= 30:
        vr.flags.append(f"Wind direction off by {abs(vr.delta_wind_dir):.0f}\u00b0")

    if abs(vr.delta_temp_c) >= 3:
        bias = "under" if vr.delta_temp_c < 0 else "over"
        vr.flags.append(f"Temp {bias}-forecast by {abs(vr.delta_temp_c):.1f}\u00b0C")

    if abs(vr.delta_pressure_hpa) >= 3:
        vr.flags.append(f"Pressure off by {abs(vr.delta_pressure_hpa):.1f} hPa")

    if abs(vr.delta_density_alt_ft) >= 300:
        bias = "under" if vr.delta_density_alt_ft < 0 else "over"
        vr.flags.append(f"DA {bias}-forecast by {abs(vr.delta_density_alt_ft):,} ft")

    return vr


# =============================================================================
# SUPABASE PERSISTENCE
# =============================================================================

def store_verification(sb_client, vr: VerificationResult) -> bool:
    """Stores a verification result in Supabase.

    Uses the file_hash as a dedup key — re-uploading the same file
    updates the existing row instead of creating a duplicate.

    Returns True on success, False on failure.
    """
    if sb_client is None:
        return False

    try:
        session_data = {
            "file_hash": vr.session_hash,
            "operator": vr.operator,
            "lat": vr.lat,
            "lon": vr.lon,
            "timestamp": vr.timestamp.isoformat() if vr.timestamp else None,
            "model_name": vr.model_name,
            "lead_time_hours": vr.lead_time_hours,
            "sample_count": vr.sample_count,
            "duration_seconds": vr.duration_seconds,
            "actual_wind_kt": vr.actual_wind_kt,
            "actual_wind_dir": vr.actual_wind_dir,
            "actual_temp_c": vr.actual_temp_c,
            "actual_rh": vr.actual_rh,
            "actual_pressure_hpa": vr.actual_pressure_hpa,
            "actual_density_alt_ft": vr.actual_density_alt_ft,
            "fcst_wind_kt": vr.fcst_wind_kt,
            "fcst_wind_dir": vr.fcst_wind_dir,
            "fcst_temp_c": vr.fcst_temp_c,
            "fcst_rh": vr.fcst_rh,
            "fcst_pressure_hpa": vr.fcst_pressure_hpa,
            "fcst_density_alt_ft": vr.fcst_density_alt_ft,
            "delta_wind_kt": vr.delta_wind_kt,
            "delta_wind_dir": vr.delta_wind_dir,
            "delta_temp_c": vr.delta_temp_c,
            "delta_rh": vr.delta_rh,
            "delta_pressure_hpa": vr.delta_pressure_hpa,
            "delta_density_alt_ft": vr.delta_density_alt_ft,
            "mvs": vr.mvs,
            "grade": vr.grade,
            "assessment": vr.assessment,
            "flags": ",".join(vr.flags) if vr.flags else "",
        }

        sb_client.table("forecast_verifications").upsert(
            session_data,
            on_conflict="file_hash",
        ).execute()

        return True
    except Exception as e:
        logger.warning("Verification store failed: %s", e)
        return False


def load_recent_verifications(sb_client, lat: float, lon: float, days: int = 90) -> list:
    """Loads recent verification results for a site from Supabase.

    Returns a list of dicts sorted by timestamp descending.
    """
    if sb_client is None:
        return []

    try:
        cutoff = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=days)).isoformat()
        result = (
            sb_client.table("forecast_verifications")
            .select("*")
            .gte("timestamp", cutoff)
            .gte("lat", lat - 0.2).lte("lat", lat + 0.2)
            .gte("lon", lon - 0.2).lte("lon", lon + 0.2)
            .order("timestamp", desc=True)
            .limit(100)
            .execute()
        )
        return result.data if result.data else []
    except Exception as e:
        logger.debug("Verification load failed: %s", e)
        return []
