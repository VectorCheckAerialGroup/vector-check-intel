import streamlit as st
import pandas as pd
import math
import re
import json
import urllib.request
import os
from datetime import datetime, timezone, timedelta
from timezonefinder import TimezoneFinder
import pytz
import plotly.graph_objects as go
import tempfile
from fpdf import FPDF
from supabase import create_client, Client

# Import Vector Check Modules
from modules.data_ingest    import get_aviation_weather, fetch_mission_data, get_model_run_info
from modules.hazard_logic   import get_weather_element, calculate_icing_profile, get_turb_ice, apply_tactical_highlights
from modules.atmosphere     import evaluate_blsn
from modules.visualizations import plot_convective_profile
from modules.sounding import extract_high_res_profile, render_sounding_plotly
from modules.telemetry      import log_action
from modules.astronomy      import get_astronomical_data, get_light_planning_window
from modules.space_weather  import get_kp_index
from modules.climate_ingest import get_climate_context
from modules.ensemble_analysis import (
    fetch_all_models, compute_ensemble_blocks,
    identify_risk_windows, generate_briefing,
    build_model_matrix, summarize_matrix,
)
from modules.model_performance import (
    compute_performance_scorecard,
    grade_wind_mae, grade_gust_mae, grade_temp_mae, grade_pressure_mae,
    grade_dir_mae, grade_rh_mae, grade_vis_mae,
    GRADE_COLORS,
)
from modules.geomag import get_magnetic_declination
from modules.kestrel_ingest import parse_kestrel_csv
from modules.sounding_ingest import (
    verify_sounding_csv, parse_sounding_csv, bin_profile_by_alt,
)
from modules.forecast_verification import (
    average_session, compute_file_hash, match_forecast_hour,
    compute_verification, store_verification, load_recent_verifications,
)

# FIX: calc_td and calculate_density_altitude now live exclusively in physics.py.
# Importing from the single authoritative source eliminates the previous duplicate
# definition in this file and in visualizations.py.
from modules.physics import calc_td, calculate_density_altitude, SNOWPACK_BLSN_THRESHOLD_M, attenuate_gust_delta

# =============================================================================
# CONSTANTS & PREFERENCES
# =============================================================================
CONVECTIVE_CCL_MULTIPLIER          = 400
METERS_TO_SM                       = 1609.34
ALL_P_LEVELS                       = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300, 250, 200, 150]
PREFS_FILE                         = "user_prefs.json"   # JSON fallback only
PREFS_TABLE                        = "user_preferences"  # Supabase primary store

# DETACHMENT FALLBACK COORDINATES
USER_DEFAULTS = {
    "VCAG":    {"lat": 44.1628, "lon": -77.3832},   # Belleville, ON
    "Vector1": {"lat": 54.4642, "lon": -110.1825},  # Cold Lake, AB
    "Vector2": {"lat": 45.9003, "lon": -77.2818},   # Petawawa, ON
    "Vector3": {"lat": 48.3303, "lon": -70.9961},   # Bagotville, QC
    "Vector4": {"lat": 43.6532, "lon": -79.3832},   # Toronto, ON
}

# =============================================================================
# PREFERENCES — SUPABASE-FIRST WITH JSON FALLBACK
# FIX: Previously written to the ephemeral container filesystem (user_prefs.json),
# which was wiped on every DigitalOcean App Platform redeploy or restart.
# Preferences are now persisted to Supabase (user_preferences table) with a
# graceful fallback to the local JSON file if the DB is unavailable.
#
# Required Supabase table (run once in SQL editor):
#   CREATE TABLE user_preferences (
#     operator_id TEXT PRIMARY KEY,
#     lat         FLOAT,  lon  FLOAT,
#     wind        INT,    ceil INT,   vis FLOAT,
#     turb        TEXT,   ice  TEXT,
#     updated_at  TIMESTAMPTZ DEFAULT now()
#   );
# =============================================================================

@st.cache_resource
def _get_supabase() -> Client | None:
    """Returns a cached Supabase client. Created once, reused across all calls.
    Returns None when the secrets are missing AND raises (clearing the cache)
    when initialization itself fails so a transient credential/network problem
    doesn't permanently pin the session at None.

    To force a fresh client creation (e.g. after rotating credentials),
    call `_get_supabase.clear()` and then invoke this function again.
    """
    try:
        url = st.secrets["supabase"]["url"]
        key = st.secrets["supabase"]["key"]
    except (KeyError, FileNotFoundError):
        # Secrets not configured — return None and don't retry (this is a
        # config issue that requires operator intervention to fix).
        return None
    try:
        return create_client(url, key)
    except Exception as e:
        # Transient failure — surface it so cache_resource doesn't pin None.
        # Streamlit will retry on the next call.
        raise RuntimeError(f"Supabase client init failed: {e}") from e


def _safe_get_supabase() -> Client | None:
    """Wrapper that suppresses the cache-clearing RuntimeError so callers can
    use a simple truthy check. Use this everywhere except in code that wants
    to know specifically that init failed (vs not-configured).
    """
    try:
        return _get_supabase()
    except RuntimeError:
        # Init failed — clear the cache so we retry on the next call, but
        # return None so this call can proceed with the JSON fallback path.
        try:
            _get_supabase.clear()
        except Exception:
            pass
        return None


def load_prefs(user: str) -> dict:
    """Loads operator preferences from Supabase, falling back to local JSON."""
    # --- Primary: Supabase ---
    try:
        sb = _safe_get_supabase()
        if sb:
            result = sb.table(PREFS_TABLE).select("*").eq("operator_id", user).execute()
            if result.data:
                row = result.data[0]
                return {
                    "lat": row.get("lat"), "lon": row.get("lon"),
                    "wind": row.get("wind"), "ceil": row.get("ceil"),
                    "vis": row.get("vis"), "turb": row.get("turb"),
                    "ice": row.get("ice"),
                }
    except Exception:
        pass

    # --- Fallback: Local JSON ---
    if os.path.exists(PREFS_FILE):
        try:
            with open(PREFS_FILE, "r") as f:
                return json.load(f).get(user, {})
        except Exception:
            pass

    return {}


def save_prefs(user: str, lat, lon, wind, ceil, vis, turb, ice) -> None:
    """Persists operator preferences to Supabase, falling back to local JSON."""
    payload = {
        "operator_id": user,
        "lat":  float(lat),  "lon":  float(lon),
        "wind": int(wind),   "ceil": int(ceil),
        "vis":  float(vis),  "turb": str(turb),
        "ice":  str(ice),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    # --- Primary: Supabase upsert ---
    try:
        sb = _safe_get_supabase()
        if sb:
            sb.table(PREFS_TABLE).upsert(payload).execute()
            return
    except Exception:
        pass

    # --- Fallback: Local JSON ---
    prefs: dict = {}
    if os.path.exists(PREFS_FILE):
        try:
            with open(PREFS_FILE, "r") as f:
                prefs = json.load(f)
        except Exception:
            pass

    prefs[user] = {
        "lat": float(lat), "lon": float(lon),
        "wind": int(wind), "ceil": int(ceil),
        "vis": float(vis), "turb": turb, "ice": ice,
    }
    try:
        with open(PREFS_FILE, "w") as f:
            json.dump(prefs, f)
    except Exception:
        pass


def sanitize_prefs(prefs: dict, user: str) -> tuple:
    """Anti-Corruption Gate: Scrubs poisoned memory states and enforces user-specific baselines."""
    base_loc = USER_DEFAULTS.get(user, {"lat": 44.1628, "lon": -77.3832})
    def_lat, def_lon = base_loc["lat"], base_loc["lon"]

    try:    lat  = float(prefs.get('lat',  def_lat))
    except (ValueError, TypeError): lat  = def_lat
    try:    lon  = float(prefs.get('lon',  def_lon))
    except (ValueError, TypeError): lon  = def_lon
    try:    wind = int(prefs.get('wind', 30))
    except (ValueError, TypeError): wind = 30
    try:    ceil = int(prefs.get('ceil', 500))
    except (ValueError, TypeError): ceil = 500
    try:    vis  = float(prefs.get('vis', 3.0))
    except (ValueError, TypeError): vis  = 3.0

    turb = str(prefs.get('turb', "MOD"))
    ice  = str(prefs.get('ice',  "NIL"))

    if lat == 0.0 or lon == 0.0:
        lat, lon = def_lat, def_lon
    if wind == 0 and ceil == 0:
        wind, ceil, vis = 30, 500, 3.0

    return lat, lon, wind, ceil, vis, turb, ice


# =============================================================================
# 1. PAGE CONFIG & CSS
# =============================================================================
st.set_page_config(page_title="Vector Check: Atmospheric Risk Management", layout="wide")
st.markdown("""
    <style>
    [data-testid="stMetricValue"] { font-size: 1.2rem !important; color: #E58E26 !important; }
    [data-testid="stMetricLabel"] { font-size: 0.8rem !important; color: #A0A4AB !important; text-transform: uppercase; }
    .ifr-text  { color: #ff4b4b; font-weight: bold; }
    .mvfr-text { color: inherit !important; font-weight: inherit !important; }
    .fz-warn   { background-color: #ff4b4b; color: white; padding: 2px; border-radius: 3px; font-weight: bold; }
    table { margin-left: auto; margin-right: auto; text-align: center !important; width: 90%; border-collapse: collapse; background-color: #1B1E23; }
    th { text-align: center !important; color: #8E949E !important; font-weight: bold !important; padding: 10px !important; border-bottom: 2px solid #3E444E !important; text-transform: uppercase; }
    td { text-align: center !important; padding: 8px !important; color: #D1D5DB !important; border-bottom: 1px solid #2D3139 !important; }
    .obs-text { font-family: "Source Sans Pro", sans-serif; font-size: 0.95rem; line-height: 1.6; color: #D1D5DB; }
    div[data-testid="column"] button { width: 100%; padding: 0px; font-size: 0.8rem; }
    </style>
    """, unsafe_allow_html=True)


# =============================================================================
# 2. ZERO-COST AUTHENTICATION & LEGAL GATEWAY
# =============================================================================
def _verify_password(supplied: str, stored: str) -> bool:
    """Constant-time password verification.

    Supports two storage formats in secrets.toml:
      1. bcrypt hash — recommended, starts with $2a$ / $2b$ / $2y$
      2. plaintext  — legacy, deprecated, will log a warning

    Uses hmac.compare_digest for the plaintext comparison so timing attacks
    against the plaintext path are at least not trivial.
    """
    import hmac
    if not isinstance(stored, str) or not isinstance(supplied, str):
        return False

    # bcrypt-style hash detection
    if stored.startswith(("$2a$", "$2b$", "$2y$")):
        try:
            import bcrypt
            return bcrypt.checkpw(supplied.encode("utf-8"), stored.encode("utf-8"))
        except Exception:
            # bcrypt failure (bad hash, library missing) — fail closed.
            return False

    # Legacy plaintext path — constant-time compare. Log a deprecation warning
    # so operators know to upgrade.
    import logging
    logging.getLogger("arms.auth").warning(
        "Plaintext password in secrets.toml detected — migrate to bcrypt hash. "
        "Generate with: python -c \"import bcrypt; "
        "print(bcrypt.hashpw(b'YOUR_PASSWORD', bcrypt.gensalt()).decode())\""
    )
    return hmac.compare_digest(supplied, stored)


# Rate limiting constants for login attempts (per-session, in-memory only).
# Doesn't protect against process restarts or new sessions, but raises the
# brute-force cost meaningfully for a single attacker.
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_S = 15 * 60   # 15 minutes


def check_password() -> bool:
    if "password_correct" not in st.session_state:
        st.session_state["password_correct"] = False
    if "eula_accepted" not in st.session_state:
        st.session_state["eula_accepted"] = False

    if st.session_state["password_correct"] and st.session_state["eula_accepted"]:
        return True

    st.title("Vector Check Aerial Group Inc.")
    st.caption("Atmospheric Risk Management System - Restricted Access")
    st.divider()
    st.subheader("End User License Agreement & Proprietary Rights")

    eula_text = """
<div style="color: #A0A4AB; font-size: 0.85rem; line-height: 1.5; margin-bottom: 20px; height: 250px; overflow-y: scroll; padding: 15px; border: 1px solid #3E444E; background-color: #15171A; border-radius: 5px;">
<strong>1. UNAUTHORIZED FOR PRIMARY DECISION MAKING (NOT A CERTIFIED BRIEFING)</strong><br>
This Atmospheric Risk Management System is an uncertified, supplemental situational awareness tool. It aggregates and visualizes raw numerical weather prediction (NWP) models. It is STRICTLY PROHIBITED to use this software as a primary or sole source of aeronautical weather information. It DOES NOT replace, nor is it an alternative to, official flight weather briefings provided by the designated civil aviation authority in the operator's jurisdiction (including but not limited to NAV CANADA, Environment and Climate Change Canada (ECCC), the U.S. Federal Aviation Administration (FAA) and National Oceanic and Atmospheric Administration (NOAA), the UK Civil Aviation Authority (CAA), the Civil Aviation Safety Authority (CASA) of Australia, EUROCONTROL, or any other national or regional aviation authority with jurisdiction over the airspace in which operations are conducted).
<br><br>
<strong>2. ABSOLUTE PILOT IN COMMAND (PIC) RESPONSIBILITY</strong><br>
In accordance with the civil aviation regulations applicable to the operator's jurisdiction (e.g. Transport Canada Civil Aviation (TCCA), the U.S. Federal Aviation Regulations (FAR), UK CAA regulations, or equivalent), the Pilot in Command (PIC) retains absolute, non-transferable authority and responsibility for the safe operation of the aircraft. Atmospheric models are inherently flawed, subject to latency, and cannot accurately predict micro-climates, sudden localized shear, or boundary layer anomalies. Vector Check Aerial Group Inc. does not clear, authorize, or endorse any flight operations.
<br><br>
<strong>3. INTELLECTUAL PROPERTY & PROPRIETARY ALGORITHMS</strong><br>
All meteorological algorithms, hazard matrices, logic engines (including but not limited to the Visible Moisture Gate, atmospheric interpolation, and Urban Venturi multipliers), source code, and visual interfaces contained within this software are the exclusive intellectual property and trade secrets of Vector Check Aerial Group Inc. This agreement grants you a limited, non-exclusive, revocable, non-transferable license to use the software solely for internal operational awareness.
<br><br>
<strong>4. RESTRICTIONS ON USE & NON-COMPETE</strong><br>
You are strictly prohibited from copying, scraping, reverse-engineering, decompiling, or otherwise attempting to extract the underlying mathematical matrices, proprietary thresholds, or source code. You may not use this software, its outputs, or its methodologies to develop, train, or inform any competing meteorological, aviation, or software product.
<br><br>
<strong>5. MAXIMUM LIMITATION OF LIABILITY</strong><br>
This software is provided "AS IS" and "AS AVAILABLE" with zero warranties, express or implied. To the maximum extent permitted by Canadian law, Vector Check Aerial Group Inc., its directors, officers, and affiliates shall bear ZERO LIABILITY for any direct, indirect, punitive, or consequential damages. This includes, but is not limited to: loss of airframes, destruction of payloads, property damage, personal injury, death, loss of revenue, or regulatory fines resulting from the use of, or inability to use, this software.
<br><br>
<strong>6. INDEMNIFICATION</strong><br>
By accessing this software, you agree to fully indemnify, defend, and hold harmless Vector Check Aerial Group Inc. from any and all claims, lawsuits, liabilities, penalties, or legal fees arising from your flight operations, your violation of any aviation regulations, or your misinterpretation of the data provided herein.
<br><br>
<strong>7. GOVERNING LAW & JURISDICTION</strong><br>
This Agreement shall be governed by and construed in accordance with the laws of the Province of Ontario and the federal laws of Canada applicable therein, without regard to its conflict of law provisions. Any legal action or proceeding arising under this Agreement will be brought exclusively in the federal or provincial courts located in Ontario, Canada.
</div>
    """
    st.markdown(eula_text, unsafe_allow_html=True)
    st.subheader("Operator Authentication")

    # Per-session rate limit state — lives in session_state.
    if "_login_attempts" not in st.session_state:
        st.session_state["_login_attempts"] = []   # list of unix-second timestamps

    # Evict timestamps older than the rate window
    import time as _t
    _now = _t.time()
    st.session_state["_login_attempts"] = [
        ts for ts in st.session_state["_login_attempts"]
        if _now - ts < _LOGIN_WINDOW_S
    ]
    _is_rate_limited = len(st.session_state["_login_attempts"]) >= _LOGIN_MAX_ATTEMPTS

    if _is_rate_limited:
        _oldest = min(st.session_state["_login_attempts"])
        _wait_s = int(_LOGIN_WINDOW_S - (_now - _oldest))
        _wait_min = max(1, _wait_s // 60)
        st.error(
            f"⚠️ Too many failed login attempts. Please wait {_wait_min} "
            f"minute(s) before trying again."
        )

    with st.form("login_form"):
        eula_check = st.checkbox("I confirm I am the Pilot in Command (PIC) and I accept the terms of this End User License Agreement.")
        st.markdown("<br>", unsafe_allow_html=True)
        user = st.text_input("Operator ID")
        pwd  = st.text_input("Passcode", type="password")
        submitted = st.form_submit_button("Acknowledge & Authenticate",
                                          disabled=_is_rate_limited)

        if submitted and not _is_rate_limited:
            stored = st.secrets.get("passwords", {}).get(user, "")
            valid = bool(stored) and _verify_password(pwd, stored)
            if valid:
                if not eula_check:
                    st.error("⚠️ REGULATORY HALT: You must accept the End User License Agreement to authenticate.")
                else:
                    # Reset rate-limit counter on successful auth
                    st.session_state["_login_attempts"] = []

                    st.session_state["password_correct"] = True
                    st.session_state["eula_accepted"]    = True
                    st.session_state["active_operator"]  = user

                    raw_prefs = load_prefs(user)
                    lat, lon, wind, ceil, vis, turb, ice = sanitize_prefs(raw_prefs, user)

                    st.session_state['input_lat']  = lat
                    st.session_state['input_lon']  = lon
                    st.session_state['input_wind'] = wind
                    st.session_state['input_ceil'] = ceil
                    st.session_state['input_vis']  = vis
                    st.session_state['input_turb'] = turb
                    st.session_state['input_ice']  = ice

                    try: log_action(user, 0.0, 0.0, "SYS", "AUTH_AND_EULA_SUCCESS")
                    except: pass
                    st.rerun()
            else:
                # Record the failed attempt
                st.session_state["_login_attempts"].append(_now)
                _remaining = _LOGIN_MAX_ATTEMPTS - len(st.session_state["_login_attempts"])
                if _remaining > 0:
                    st.error(f"⚠️ UNAUTHORIZED: Invalid Operator ID or Passcode. "
                             f"{_remaining} attempt(s) remaining before lockout.")
                else:
                    st.error("⚠️ UNAUTHORIZED: Account locked for 15 minutes.")
                try: log_action(user or "UNKNOWN", 0.0, 0.0, "SYS", "AUTH_FAIL")
                except: pass

    return False


if not check_password():
    st.stop()

if "input_lat" not in st.session_state:
    current_op = st.session_state.get("active_operator", "UNKNOWN")
    raw_prefs  = load_prefs(current_op)
    san_lat, san_lon, san_wind, san_ceil, san_vis, san_turb, san_ice = sanitize_prefs(raw_prefs, current_op)
    st.session_state['input_lat']  = san_lat
    st.session_state['input_lon']  = san_lon
    st.session_state['input_wind'] = san_wind
    st.session_state['input_ceil'] = san_ceil
    st.session_state['input_vis']  = san_vis
    st.session_state['input_turb'] = san_turb
    st.session_state['input_ice']  = san_ice


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_interp_thermals(alt_msl: float, profile: list) -> tuple:
    if not profile: return 0.0, 0
    if alt_msl <= profile[0]['h']:  return profile[0]['t'], profile[0]['rh']
    if alt_msl >= profile[-1]['h']: return profile[-1]['t'], profile[-1]['rh']
    for k in range(len(profile) - 1):
        if profile[k]['h'] <= alt_msl <= profile[k + 1]['h']:
            lower = profile[k]
            upper = profile[k + 1]
            frac  = (alt_msl - lower['h']) / (upper['h'] - lower['h']) if upper['h'] != lower['h'] else 0
            i_t   = lower['t']  + frac * (upper['t']  - lower['t'])
            i_rh  = lower['rh'] + frac * (upper['rh'] - lower['rh'])
            return i_t, int(i_rh)
    return profile[0]['t'], profile[0]['rh']


def format_dir(d: float, spd: float) -> int:
    r = int(round(float(d), -1)) % 360
    if r == 0 and spd > 0: return 360
    if spd == 0:            return 0
    return r


# Minimum excursion (kt) for a gust to be operationally meaningful. A gust is
# a short-duration peak ABOVE the sustained wind; a reported "gust" only a
# knot or two over sustained isn't a real gust, it's measurement noise. We
# only surface a gust when it exceeds the sustained wind by at least this much.
GUST_FACTOR_MIN_KT = 3.0


def resolve_gust(w_spd: float, raw_gust) -> float:
    """Returns the gust value to display, or the sustained wind if no
    meaningful gust exists.

    Rule: a gust is shown only when the model's reported gust exceeds the
    sustained wind by >= GUST_FACTOR_MIN_KT. Otherwise we return w_spd (i.e.
    "no gust"), which callers render as no G-value. This:
      - kills the old w_spd*1.25 synthetic-gust fabrication (fake gusts)
      - suppresses nonsense like 4 kt sustained / 5 kt gust (1 kt excursion)
      - still surfaces a genuine excursion from calm (0 sustained / 5 gust = real)
    raw_gust may be None (model gave no gust) -> returns w_spd.
    """
    if raw_gust is None:
        return w_spd
    try:
        rg = float(raw_gust)
    except (TypeError, ValueError):
        return w_spd
    if rg >= w_spd + GUST_FACTOR_MIN_KT:
        return rg
    return w_spd


def hazard_lvl(h_str: str) -> float:
    """FIX: Previous ordering checked 'SEV' before 'MOD-SEV', causing MOD-SEV
    to always return 3.0. Longest-match-first ordering is now correct."""
    h_str = h_str.upper()
    if "MOD-SEV" in h_str: return 2.5   # Must precede both "SEV" and "MOD"
    if "SEV"     in h_str: return 3
    if "MOD"     in h_str: return 2
    if "LGT"     in h_str: return 1
    return 0


def calc_tactical_visibility(vis_raw_m, rh: int, w_spd: float, wx: int) -> float:
    if vis_raw_m is not None:
        vis_sm = float(vis_raw_m) / 1609.34
    else:
        if rh >= 95:   vis_sm = 1.5
        elif rh >= 90: vis_sm = 3.0
        elif rh >= 80: vis_sm = 5.0
        else:          vis_sm = 10.0

    if wx >= 50: return vis_sm

    if wx in [45, 48] and rh < 85:
        if rh >= 75: return max(vis_sm, 4.0)
        else:        return max(vis_sm, 7.0)

    if vis_sm < 3.0 and w_spd >= 10.0 and wx not in [45, 48]: return max(vis_sm, 6.0)
    if vis_sm < 4.0 and rh < 85:                               return max(vis_sm, 7.0)
    if vis_sm < 3.0 and wx <= 3 and rh < 95:                   return max(vis_sm, 4.0)
    return vis_sm


def _pct_rank(value: float, pcts: dict) -> int:
    """Computes percentile rank (1-99) from cached percentile distribution.

    Handles values outside the [p10, p99] range by clamping.
    Correctly handles negative-valued distributions (winter temperatures, etc.).
    """
    if pcts.get("n", 0) == 0:
        return 50

    p10 = pcts["p10"]
    p99 = pcts["p99"]

    # Clamp extremes — never return > 99 or < 1
    if value <= p10:
        return 1 if value < p10 else 10
    if value >= p99:
        return 99

    # Interpolate between known percentile anchors
    anchors = [
        (p10,          10),
        (pcts["p25"],  25),
        (pcts["p50"],  50),
        (pcts["p75"],  75),
        (pcts["p90"],  90),
        (p99,          99),
    ]

    for i in range(len(anchors) - 1):
        lo_val, lo_pct = anchors[i]
        hi_val, hi_pct = anchors[i + 1]
        if lo_val <= value <= hi_val:
            span = hi_val - lo_val
            if span == 0:
                return lo_pct
            frac = (value - lo_val) / span
            return max(1, min(99, int(lo_pct + frac * (hi_pct - lo_pct))))

    return 50


def _pct_label(p: int) -> tuple[str, str]:
    """Returns (label_text, hex_color) for a percentile value."""
    if p >= 90:   return f"P{p} — Anomalous",    "#ff4b4b"
    elif p >= 75: return f"P{p} — Elevated",      "#E58E26"
    elif p <= 10: return f"P{p} — Unusually low",  "#ff4b4b"
    elif p <= 25: return f"P{p} — Below avg",      "#E58E26"
    else:         return f"P{p} — Normal",          "#2abf2a"


def _build_da_distribution(climate: dict, elevation_ft: float) -> dict:
    """Synthesizes a density altitude percentile distribution from the cached
    temp and pressure anchors.

    Rationale: higher temp → higher DA; lower pressure → higher DA. We pair
    correlated extremes (cold+high_p → lowest DA; hot+low_p → highest DA)
    to get a physically meaningful DA distribution without re-querying the API.
    """
    t = climate["temp"]
    p = climate["pressure"]
    if t["n"] == 0 or p["n"] == 0:
        return {"p10": 0, "p25": 0, "p50": 0, "p75": 0, "p90": 0, "p99": 0, "n": 0, "mean": 0}

    # Pair correlated extremes: cold + high pressure → lowest DA; hot + low pressure → highest DA
    pairs = [
        (t["p10"], p["p90"]),   # coldest + highest pressure = lowest DA
        (t["p25"], p["p75"]),
        (t["p50"], p["p50"]),   # median conditions = median DA
        (t["p75"], p["p25"]),
        (t["p90"], p["p10"]),
        (t["p99"], p["p10"]),   # hottest + lowest pressure = highest DA
    ]

    da_values = [calculate_density_altitude(elevation_ft, t_val, p_val) for t_val, p_val in pairs]

    return {
        "p10":  da_values[0],
        "p25":  da_values[1],
        "p50":  da_values[2],
        "p75":  da_values[3],
        "p90":  da_values[4],
        "p99":  da_values[5],
        "mean": da_values[2],
        "n":    t["n"],
    }


# =============================================================================
# SPATIAL ENGINES & CACHED DATA FETCH
# =============================================================================

@st.cache_data(ttl=86400)
def get_location_name(user_lat: float, user_lon: float) -> str:
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?lat={user_lat}&lon={user_lon}&format=json"
        req = urllib.request.Request(url, headers={'User-Agent': 'VectorCheck-App/2.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))

        address  = data.get('address', {})
        region   = address.get('city', address.get('town', address.get('county', address.get('village', address.get('region', 'Unknown Region')))))
        province = address.get('state', address.get('country', 'Unknown'))

        if region != 'Unknown Region' and province != 'Unknown': return f"{region}, {province}"
        elif province != 'Unknown': return province
        else: return f"Coord: {user_lat:.2f}, {user_lon:.2f}"
    except Exception:
        return f"Coord: {user_lat:.2f}, {user_lon:.2f}"


@st.cache_data(ttl=3600)
def get_nearest_icao_station(user_lat: float, user_lon: float) -> dict:
    try:
        min_lat, max_lat = user_lat - 1.0, user_lat + 1.0
        min_lon, max_lon = user_lon - 1.0, user_lon + 1.0
        url = f"https://aviationweather.gov/api/data/taf?bbox={min_lat},{min_lon},{max_lat},{max_lon}&format=json"
        req = urllib.request.Request(url, headers={'User-Agent': 'VectorCheck-App/2.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))

        best_station = {"icao": "NONE", "dist": float('inf'), "dir": ""}
        seen_icaos   = set()

        for taf in data:
            if 'icaoId' not in taf or 'lat' not in taf or 'lon' not in taf: continue
            icao_code = taf['icaoId']
            if icao_code in seen_icaos: continue
            seen_icaos.add(icao_code)

            stn_lat, stn_lon = float(taf['lat']), float(taf['lon'])
            R    = 6371.0
            lat1 = math.radians(user_lat);  lon1 = math.radians(user_lon)
            lat2 = math.radians(stn_lat);   lon2 = math.radians(stn_lon)

            dlat = lat2 - lat1;  dlon = lon2 - lon1
            a    = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
            c    = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
            dist = R * c

            if dist <= 50.0 and dist < best_station["dist"]:
                y       = math.sin(dlon) * math.cos(lat2)
                x       = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
                bearing = (math.degrees(math.atan2(y, x)) + 360) % 360
                dirs    = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
                best_station = {"icao": icao_code, "dist": dist, "dir": dirs[int(round(bearing / 45)) % 8]}

        if best_station["icao"] != "NONE": return best_station
    except Exception:
        pass
    return {"icao": "NONE", "dist": None, "dir": ""}


@st.cache_data(ttl=900)
def fetch_weather_payload(fetch_lat: float, fetch_lon: float, model_label: str) -> dict:
    """Fetches the forecast payload via the provider routing layer with failover.

    Args:
        fetch_lat, fetch_lon: site coordinates
        model_label: display name of the model (key into _all_models registry)

    Returns:
        dict containing the forecast data in Open-Meteo response shape, plus:
          "_run_info":      run-cycle metadata (cycle, age_hours, etc.)
          "_served_by":     "meteomatics" or "open-meteo" — which provider actually served
          "_primary_failed": True if the primary provider failed (even if fallback succeeded)
          "_primary_error":  error message from primary (None if it succeeded)
        On total failure both providers errored — caller sees the standard
        {"error": True, "message": ...} shape.
    """
    from modules.data_ingest import fetch_forecast_with_fallback

    entry = _all_models.get(model_label)
    if entry is None:
        return {"error": True, "message": f"Unknown model: {model_label}"}
    route, run_id, _in_coverage = entry

    result = fetch_forecast_with_fallback(route, fetch_lat, fetch_lon)
    if not result.ok:
        # Surface the most informative error to the caller. Prefer the primary
        # error message since that's the one the operator selected.
        msg = result.primary_error or "All providers failed"
        return {
            "error": True,
            "message": msg,
            "_served_by": None,
            "_primary_failed": True,
            "_primary_error": result.primary_error,
        }

    payload = result.data
    if not isinstance(payload, dict):
        return {"error": True, "message": "Provider returned non-dict payload"}

    # Augment payload with routing metadata
    payload["_served_by"] = result.served_by
    payload["_primary_failed"] = result.primary_failed
    payload["_primary_error"] = result.primary_error

    # Attach run info — if the payload already carries it (Meteomatics provider
    # sets it from dateGenerated), use that; else fetch from the run-info
    # endpoint (Open-Meteo path).
    if not payload.get("_run_info"):
        try:
            # Determine which URL to use for run-info lookup. For Meteomatics
            # the dateGenerated has already been parsed by the provider; if
            # missing we leave _run_info empty rather than hitting Open-Meteo's
            # metadata endpoint with a model that doesn't match.
            if result.served_by == "open-meteo":
                # Run-info expects an Open-Meteo URL
                om_target = route.primary[1] if route.primary[0] == "open-meteo" else (
                    route.fallback[1] if route.fallback and route.fallback[0] == "open-meteo" else None
                )
                if om_target:
                    payload["_run_info"] = get_model_run_info(om_target, model_id=run_id) or {}
                else:
                    payload["_run_info"] = {}
            else:
                payload["_run_info"] = payload.get("_run_info") or {}
        except Exception:
            payload["_run_info"] = {}

    return payload


@st.cache_data(ttl=900)
def fetch_metar_taf(fetch_icao: str) -> tuple:
    return get_aviation_weather(fetch_icao)


@st.cache_data(ttl=10800)
def fetch_space_weather_cached(dt_iso_str: str) -> dict:
    dt_utc = datetime.fromisoformat(dt_iso_str).replace(tzinfo=timezone.utc)
    return get_kp_index(dt_utc)


@st.cache_data(ttl=86400)
def fetch_astronomy_cached(lat_val: float, lon_val: float, dt_iso_str: str, tz_name: str, tz_abbr_str: str) -> dict:
    dt_utc   = datetime.fromisoformat(dt_iso_str).replace(tzinfo=timezone.utc)
    local_tz = pytz.timezone(tz_name) if tz_name else timezone.utc
    return get_astronomical_data(lat_val, lon_val, dt_utc, local_tz, tz_abbr_str)


@st.cache_data(ttl=86400, show_spinner="Loading 25-year climate context...")
def fetch_climate_context_cached(lat_val: float, lon_val: float, month_val: int) -> dict:
    """Fetches 25-year climate context with 24-hour cache.

    Uses tiered fallback: ECCC station (Tier 1) -> NASA POWER (Tier 2).
    Both sources are FREE with no API key required.

    Returns a dict (not a dataclass) because st.cache_data requires
    serializable return types.
    """
    sb = _safe_get_supabase()

    ctx = get_climate_context(lat_val, lon_val, month_val, sb_client=sb)

    return {
        "lat_bin": ctx.lat_bin, "lon_bin": ctx.lon_bin,
        "month": ctx.month, "years_range": ctx.years_range,
        "error": ctx.error, "cached": ctx.cached,
        "source": ctx.source,
        "source_label": ctx.source_label,
        "source_distance_km": ctx.source_distance_km,
        "wind": {"p10": ctx.wind.p10, "p25": ctx.wind.p25, "p50": ctx.wind.p50,
                 "p75": ctx.wind.p75, "p90": ctx.wind.p90, "p99": ctx.wind.p99,
                 "mean": ctx.wind.mean, "n": ctx.wind.sample_count},
        "temp": {"p10": ctx.temp.p10, "p25": ctx.temp.p25, "p50": ctx.temp.p50,
                 "p75": ctx.temp.p75, "p90": ctx.temp.p90, "p99": ctx.temp.p99,
                 "mean": ctx.temp.mean, "n": ctx.temp.sample_count},
        "pressure": {"p10": ctx.pressure.p10, "p25": ctx.pressure.p25, "p50": ctx.pressure.p50,
                     "p75": ctx.pressure.p75, "p90": ctx.pressure.p90, "p99": ctx.pressure.p99,
                     "mean": ctx.pressure.mean, "n": ctx.pressure.sample_count},
        "rh": {"p10": ctx.rh.p10, "p25": ctx.rh.p25, "p50": ctx.rh.p50,
               "p75": ctx.rh.p75, "p90": ctx.rh.p90, "p99": ctx.rh.p99,
               "mean": ctx.rh.mean, "n": ctx.rh.sample_count},
        "wind_rose": [
            {"dir": wr.direction, "total": wr.total_pct,
             "calm": wr.calm_pct, "mod": wr.moderate_pct, "strong": wr.strong_pct,
             "avg_spd": wr.avg_speed_kt}
            for wr in ctx.wind_rose
        ],
        "prevailing_dir": ctx.prevailing_dir,
        "prevailing_pct": ctx.prevailing_pct,
    }


# =============================================================================
# IMPACT MATRIX — PRECOMPUTED & CACHED
# FIX: Previously, the full 48-hour atmospheric physics loop ran on EVERY
# Streamlit rerender (every slider move, every button click, every UI interaction).
# This function is now @st.cache_data, meaning the 48 full atmospheric column
# computations only execute when the underlying data payload, operator constraints,
# or terrain environment actually change. All other rerenders read from cache.
# =============================================================================

@st.cache_data(ttl=900, show_spinner=False)
def compute_impact_matrix(
    h_data:       dict,
    nearest_idx:  int,
    max_idx:      int,
    sfc_elevation: float,
    k_conv:       float,
    t_wind:       int,
    t_ceil:       int,
    t_vis:        float,
    t_turb:       str,
    t_ice:        str,
    terrain_env:  str,
    tz_str:       str,
    # Enable flags for each constraint (default True for backward compat)
    en_wind:      bool = True,
    en_ceil:      bool = True,
    en_vis:       bool = True,
    en_turb:      bool = True,
    en_ice:       bool = True,
    # New optional constraints (default disabled)
    en_tmax:      bool = False,
    t_tmax:       float = 40.0,
    en_tmin:      bool = False,
    t_tmin:       float = -20.0,
    en_rhmax:     bool = False,
    t_rhmax:      int = 95,
) -> tuple[list, list, list]:
    """
    Evaluates Go/No-Go status for every hour in the 72-hour forecast window.

    Returns:
        x_labels    — bar chart x-axis labels (e.g. "T12", "T13" …)
        color_vals  — "#1E8449" (Go) or "#B82E2E" (No-Go) per slot
        hover_texts — human-readable status string per slot
    """
    local_tz_mat = pytz.timezone(tz_str) if tz_str else timezone.utc
    x_labels:    list = []
    color_vals:  list = []
    hover_texts: list = []

    for mat_i in range(nearest_idx, max_idx + 1):
        failures: list = []

        # --- Surface wind ---
        w_raw  = h_data.get('wind_speed_10m', [0])[mat_i]
        w_spd  = (float(w_raw) if w_raw is not None else 0.0) * k_conv
        sfc_dir_raw = h_data.get('wind_direction_10m', [0])[mat_i]
        sfc_dir_mat = float(sfc_dir_raw) if sfc_dir_raw is not None else 0.0

        g_raw_list = h_data.get('wind_gusts_10m')
        g_raw      = (float(g_raw_list[mat_i]) * k_conv) if (g_raw_list and len(g_raw_list) > mat_i and g_raw_list[mat_i] is not None) else None
        gst        = resolve_gust(w_spd, g_raw)

        # --- Weather code ---
        wx_raw = h_data.get('weather_code', [0])
        wx     = int(wx_raw[mat_i]) if (wx_raw and len(wx_raw) > mat_i and wx_raw[mat_i] is not None) else 0

        # --- Temperature / moisture ---
        t_temp_raw = h_data.get('temperature_2m', [0])[mat_i]
        t_temp     = float(t_temp_raw) if t_temp_raw is not None else 0.0
        rh_raw_mat = h_data.get('relative_humidity_2m', [0])[mat_i]
        rh_v       = int(rh_raw_mat) if rh_raw_mat is not None else 0
        td_mat     = calc_td(t_temp, rh_v)
        sfc_spread = t_temp - td_mat

        # --- Snow depth (metres) — now fetched from API; gate is live ---
        sn_depth_raw = h_data.get('snow_depth', [0])
        sn_depth     = float(sn_depth_raw[mat_i]) if sn_depth_raw and len(sn_depth_raw) > mat_i and sn_depth_raw[mat_i] is not None else 0.0

        # --- Visibility ---
        vis_raw_list = h_data.get('visibility')
        vis_raw_val  = vis_raw_list[mat_i] if vis_raw_list and len(vis_raw_list) > mat_i else None
        vis_sm       = calc_tactical_visibility(vis_raw_val, rh_v, w_spd, wx)

        # --- 15-Layer Thermodynamic Column ---
        profile = [{'h': sfc_elevation, 't': t_temp, 'td': td_mat, 'spread': sfc_spread, 'rh': rh_v}]
        for p in ALL_P_LEVELS:
            gh_list = h_data.get(f'geopotential_height_{p}hPa')
            t_list  = h_data.get(f'temperature_{p}hPa')
            rh_list = h_data.get(f'relative_humidity_{p}hPa')
            if gh_list and t_list and rh_list and len(gh_list) > mat_i:
                if gh_list[mat_i] is not None and t_list[mat_i] is not None and rh_list[mat_i] is not None:
                    p_gh = float(gh_list[mat_i]) * 3.28084
                    if p_gh > profile[-1]['h']:
                        p_t  = float(t_list[mat_i])
                        p_rh = int(rh_list[mat_i])
                        p_td = calc_td(p_t, p_rh)
                        profile.append({'h': p_gh, 't': p_t, 'td': p_td, 'spread': p_t - p_td, 'rh': p_rh})

        # --- Convective assessment ---
        t_925_list = h_data.get('temperature_925hPa')
        t_925      = float(t_925_list[mat_i]) if (t_925_list and len(t_925_list) > mat_i and t_925_list[mat_i] is not None) else t_temp
        is_convective = (wx >= 80) or ((t_temp - t_925) >= 7.5 and t_temp >= 10.0)

        precip_raw_top = h_data.get('precipitation', [0])
        precip_val_top = float(precip_raw_top[mat_i]) if precip_raw_top and len(precip_raw_top) > mat_i and precip_raw_top[mat_i] is not None else 0.0

        # --- Cloud depth scan ---
        cb_v = ct_v = None
        for layer in profile:
            if layer['spread'] <= 4.0:
                if cb_v is None: cb_v = layer['h']
                ct_v = layer['h']
        c_depth = (ct_v - cb_v) if cb_v and ct_v else 0

        # --- Thermal Phase Gate (Precipitation Veto) ---
        if 50 <= wx <= 59 and (c_depth >= 2500 or precip_val_top >= 0.5 or is_convective):
            frz_raw_list_mat = h_data.get('freezing_level_height')
            frz_raw_mat      = float(frz_raw_list_mat[mat_i]) if frz_raw_list_mat and len(frz_raw_list_mat) > mat_i and frz_raw_list_mat[mat_i] is not None else 0.0
            frz_agl_mat      = max(0, (frz_raw_mat * 3.28084) - sfc_elevation)

            warm_nose = any(layer['t'] > 0 for layer in profile[1:])
            is_heavy  = wx in [54, 55, 57]

            if t_temp <= 0:
                wx = (67 if is_heavy else 66) if warm_nose else (73 if is_heavy else 71)
            elif 0 < t_temp <= 2.5 and frz_agl_mat < 1500:
                wx = 69 if is_heavy else 68
            else:
                wx = 63 if is_heavy else 61

        # --- BLSN / DRSN Kinetic Split ---
        # Single authoritative implementation in modules/atmosphere.py. Returns
        # the BLSN trigger flag (used downstream for hazard banding), the DRSN
        # trigger flag, and a vis_sm value adjusted for blowing snow obscuration.
        blsn_trigger, _drsn_trigger, vis_sm = evaluate_blsn(
            wx=wx, t_temp=t_temp, w_spd=w_spd, gst=gst,
            sn_depth=sn_depth, vis_sm=vis_sm,
        )

        # --- Cloud base cascade ---
        c_base_agl = 99999
        c_amt      = "CLR"

        search_profile = profile[1:] if len(profile) > 1 else profile

        for layer in search_profile:
            h_agl = max(0, layer['h'] - sfc_elevation)
            if layer['spread'] <= 3.0:
                if h_agl < 1000 and sfc_spread <= 3.0 and vis_sm >= 1.5 and wx < 50: continue
                c_base_agl = int(round(h_agl, -2))
                c_amt      = "OVC" if layer['spread'] <= 1.0 else "BKN"
                if c_base_agl == 0:
                    if vis_sm > 0.62 and wx not in [45, 48]: c_base_agl = 100
                    else:                                      c_amt      = "VV"
                break

        if c_amt == "CLR":
            for layer in search_profile:
                h_agl = max(0, layer['h'] - sfc_elevation)
                if layer['spread'] <= 5.0:
                    if h_agl < 1000 and sfc_spread <= 3.0 and vis_sm >= 1.5 and wx < 50: continue
                    c_base_agl = int(round(h_agl, -2))
                    c_amt      = "SCT"
                    if c_base_agl == 0 and vis_sm > 0.62 and wx not in [45, 48]: c_base_agl = 100
                    break

        if c_amt == "CLR":
            for layer in search_profile:
                h_agl = max(0, layer['h'] - sfc_elevation)
                if layer['spread'] <= 7.0:
                    if h_agl < 1000 and sfc_spread <= 3.0 and vis_sm >= 1.5 and wx < 50: continue
                    c_base_agl = int(round(h_agl, -2))
                    c_amt      = "FEW"
                    if c_base_agl == 0 and vis_sm > 0.62 and wx not in [45, 48]: c_base_agl = 100
                    break

        if is_convective and c_amt == "CLR":
            ccl_base = int(round(max(0, sfc_spread * CONVECTIVE_CCL_MULTIPLIER), -2))
            if ccl_base < 10000:
                c_base_agl = ccl_base
                c_amt      = "BKN" if wx >= 80 else "SCT"

        # --- 400ft AGL hazard evaluation ---
        alt_msl    = sfc_elevation + 400
        alt_t, alt_rh = get_interp_thermals(alt_msl, profile)
        icing_cond    = calculate_icing_profile(h_data, mat_i, wx)

        w_120_list = h_data.get('wind_speed_120m')
        w_120_val  = w_120_list[mat_i] if w_120_list and len(w_120_list) > mat_i else None

        if w_120_val is not None:
            s_c = float(w_120_val) * k_conv
        else:
            u_v_list = h_data.get('wind_speed_1000hPa')
            if u_v_list and len(u_v_list) > mat_i and u_v_list[mat_i] is not None:
                u_v_mat = float(u_v_list[mat_i]) * k_conv
                u_h_list = h_data.get('geopotential_height_1000hPa')
                u_h_mat  = float(u_h_list[mat_i]) if (u_h_list and len(u_h_list) > mat_i and u_h_list[mat_i] is not None) else 110.0
            else:
                u_v_mat, u_h_mat = w_spd, 10.0
            s_c = w_spd + (u_v_mat - w_spd) * (math.log(max(1, 400 * 0.3048) / 10) / math.log(max(1.1, u_h_mat / 10)))

        g_c       = s_c + max(0, gst - w_spd)
        turb, ice = get_turb_ice(400, s_c, w_spd, g_c, wx, is_convective, icing_cond, alt_t, alt_rh, terrain_env, c_base_agl)

        # --- Go / No-Go decision ---
        # Each constraint is checked only if its enable flag is True.
        # Operators toggle constraints in/out of effect from the UI.
        max_wind_val = max(w_spd, gst)
        if en_wind and max_wind_val > t_wind:
            failures.append(f"Wind ({int(max_wind_val)}KT)")
        if en_vis and vis_sm < t_vis:
            failures.append(f"Vis ({vis_sm:.1f}SM)")
        if en_ceil and c_base_agl < t_ceil and c_amt in ["BKN", "OVC", "VV"]:
            failures.append(f"Ceil ({c_base_agl}ft)")
        if en_turb and hazard_lvl(turb) > hazard_lvl(t_turb):
            failures.append(f"Turb ({turb})")
        if en_ice and hazard_lvl(ice) > hazard_lvl(t_ice):
            failures.append(f"Ice ({ice})")
        # New optional constraints (opt-in via toggle)
        if en_tmax and t_temp > t_tmax:
            failures.append(f"Temp ({t_temp:.0f}\u00b0C > {t_tmax:.0f})")
        if en_tmin and t_temp < t_tmin:
            failures.append(f"Temp ({t_temp:.0f}\u00b0C < {t_tmin:.0f})")
        if en_rhmax and rh_v > t_rhmax:
            failures.append(f"RH ({rh_v}% > {t_rhmax}%)")

        dt_local = datetime.fromisoformat(h_data["time"][mat_i]).replace(tzinfo=timezone.utc).astimezone(local_tz_mat)
        time_str = dt_local.strftime('%H:%M')

        x_labels.append(f"T{mat_i}")

        if not failures:
            color_vals.append("#1E8449")
            hover_texts.append(f"{time_str} | FLIGHT AUTHORIZED")
        else:
            color_vals.append("#B82E2E")
            hover_texts.append(f"{time_str} | " + ", ".join(failures))

    return x_labels, color_vals, hover_texts


# =============================================================================
# SIDEBAR CONFIGURATION
# =============================================================================

# FIX: Logo now served from the local bundled asset instead of a hardcoded
# raw GitHub URL, which breaks if the repo moves, renames, or goes private.
LOGO_LOCAL = "VCAG_Inc_-_Logo_Final.png"
LOGO_URL   = "https://raw.githubusercontent.com/VectorCheck/vector-check-intel/main/VCAG%20Inc%20-%20Logo%20Final.png"

try:
    if os.path.exists(LOGO_LOCAL):
        st.sidebar.image(LOGO_LOCAL, use_container_width=True)
    else:
        st.sidebar.image(LOGO_URL, use_container_width=True)
except Exception:
    st.sidebar.title("Vector Check")
    st.sidebar.caption("Aerial Group Inc.")

st.sidebar.header("Mission Parameters")

# Anti-Ghosting EXPLICIT Variable Injection
s_lat = st.session_state.get('input_lat', 44.1628)
s_lon = st.session_state.get('input_lon', -77.3832)

lat = st.sidebar.number_input("Latitude",  value=float(s_lat), format="%.4f")
lon = st.sidebar.number_input("Longitude", value=float(s_lon), format="%.4f")

st.session_state['input_lat'] = lat
st.session_state['input_lon'] = lon

regional_name = get_location_name(lat, lon)

# Fetch site elevation for sidebar display. Cached at module level so this is
# only a real call once per unique (lat, lon) per process lifetime. Prefers
# Meteomatics' elevation:m if credentials exist (we're paying for it anyway),
# falls back to Open-Meteo's free endpoint if not.
@st.cache_data(ttl=86400)   # 24h cache — elevations don't change
def _get_site_elevation_m(elev_lat: float, elev_lon: float) -> float:
    try:
        from modules.meteomatics_provider import (
            has_credentials as _mm_has_creds,
            fetch_meteomatics_elevation,
        )
        if _mm_has_creds():
            elev = fetch_meteomatics_elevation(elev_lat, elev_lon)
            if elev > 0:
                return elev
    except Exception:
        pass
    # Fallback — Open-Meteo's elevation endpoint. The paid customer endpoint
    # is more reliable (no IP-based blocks) but the same path works on both.
    try:
        from modules.http_client import fetch_json
        from modules.open_meteo_endpoints import base_url as _om_base, append_apikey
        elev_url = append_apikey(f"{_om_base()}/v1/elevation?latitude={elev_lat}&longitude={elev_lon}")
        payload = fetch_json(elev_url, timeout=5, retries=1)
        elevs = payload.get("elevation") or []
        if elevs and isinstance(elevs[0], (int, float)):
            return float(elevs[0])
    except Exception:
        pass
    return 0.0


_site_elev_m = _get_site_elevation_m(lat, lon)
_site_elev_ft = _site_elev_m * 3.28084

# Build the regional display: "Belleville, Ontario · 76 m / 249 ft"
if _site_elev_m > 0:
    _region_display = f"{regional_name} \u00b7 {int(round(_site_elev_m))} m / {int(round(_site_elev_ft)):,} ft"
else:
    _region_display = regional_name

st.sidebar.markdown(
    f"<div style='color: #8E949E; font-size: 0.9rem; margin-top: -10px; margin-bottom: 20px;'>{_region_display}</div>",
    unsafe_allow_html=True,
)

station_data = get_nearest_icao_station(lat, lon)
icao     = station_data["icao"]
stn_dist = station_data["dist"]
stn_dir  = station_data["dir"]

st.sidebar.text_input("Nearest Valid ICAO (Auto-Locked)", value=(icao if icao != "NONE" else "N/A"), disabled=True)
if icao == "NONE":
    st.sidebar.markdown("<div style='font-size: 0.85rem; color: #8E949E; margin-bottom: 15px;'>No TAF-issuing station within 50km.</div>", unsafe_allow_html=True)

terrain_env = st.sidebar.selectbox("Terrain Environment:", options=["Land", "Water", "Mountains", "Urban"])

# ---------------------------------------------------------------------------
# Model selector — coverage-aware
# Each model has a defined coverage area. We only expose models that actually
# cover the operator's location; outside coverage they're listed in the caption
# below the dropdown so the operator can see what isn't available and why.
# ---------------------------------------------------------------------------

# Coverage gates (lat min, lat max, lon min, lon max)
_HRDPS_BOUNDS = (40.0, 75.0, -145.0, -50.0)    # Canada + northern US strip
_HRRR_NAM_BOUNDS = (21.0, 50.0, -134.0, -60.0) # CONUS
_ICONEU_BOUNDS = (29.0, 71.0, -23.0, 45.0)     # Europe
_GLOBAL_BOUNDS = (-90.0, 90.0, -180.0, 180.0)  # everywhere

def _in_bounds(b):
    return (b[0] <= lat <= b[1]) and (b[2] <= lon <= b[3])


# =============================================================================
# PROVIDER ROUTING REGISTRY
# =============================================================================
# Each model declares (primary_provider, target) and an optional fallback.
# Provider names: "meteomatics" or "open-meteo".
# Target for meteomatics: the model id (e.g. "mix", "ecmwf-ifs").
# Target for open-meteo: the full URL endpoint.
#
# Availability of each Meteomatics model was verified against the live API
# (see /pages/99_Meteomatics_Check.py verification log, 2026-05-29):
#   mix, ecmwf-ifs, ecmwf-aifs, ncep-gfs, ncep-hrrr → reachable
#   dwd-icon → NOT available on this subscription → ICON Global routes via
#              Open-Meteo only.
# Models that have no Meteomatics equivalent (HRDPS, NAM, ACCESS-G) stay
# Open-Meteo-primary with no fallback.

from modules.data_ingest import ProviderRoute
from modules.meteomatics_provider import has_credentials as _has_mm_credentials
from modules.open_meteo_endpoints import build_url as _om_url, has_paid_subscription as _om_paid

_MM_AVAILABLE = _has_mm_credentials()    # config-time check; doesn't hit the API
_OM_PAID = _om_paid()                    # paid Open-Meteo tier detection

# Display label → (ProviderRoute, run_info_id, in_coverage)
_all_models: dict[str, tuple] = {}


def _register_model(label: str, primary: tuple, fallback: tuple | None,
                    run_id: str, in_coverage: bool) -> None:
    """Adds a model to the routing registry. Primary providers using
    Meteomatics are silently re-routed to the fallback if Meteomatics
    credentials aren't configured."""
    if primary[0] == "meteomatics" and not _MM_AVAILABLE:
        # Demote: if no Meteomatics credentials and there's a fallback, use it
        # as the primary; if no fallback either, the model is unavailable.
        if fallback is None:
            in_coverage = False
            primary = ("meteomatics", primary[1])    # keep so we error sensibly
        else:
            primary, fallback = fallback, None
    _all_models[label] = (
        ProviderRoute(primary=primary, fallback=fallback, model_label=label),
        run_id,
        in_coverage,
    )


# Meteomatics MIX — Meteomatics-only, no fallback (proprietary blend)
_register_model(
    "Meteomatics MIX",
    primary=("meteomatics", "mix"),
    fallback=None,
    run_id="mix",
    in_coverage=_MM_AVAILABLE,
)

# ECMWF IFS — Meteomatics primary, Open-Meteo fallback
_register_model(
    "ECMWF IFS (Global 9km)",
    primary=("meteomatics", "ecmwf-ifs"),
    fallback=("open-meteo", _om_url("ecmwf")),
    run_id="ecmwf",
    in_coverage=True,
)

# ECMWF AIFS — Meteomatics only (AI model, no Open-Meteo equivalent)
_register_model(
    "ECMWF AIFS (AI model)",
    primary=("meteomatics", "ecmwf-aifs"),
    fallback=None,
    run_id="ecmwf-aifs",
    in_coverage=_MM_AVAILABLE,
)

# GFS — Meteomatics primary, Open-Meteo fallback
_register_model(
    "GFS (Global 13km)",
    primary=("meteomatics", "ncep-gfs"),
    fallback=("open-meteo", _om_url("gfs")),
    run_id="gfs",
    in_coverage=True,
)

# HRDPS — Open-Meteo only (Meteomatics doesn't carry it)
_register_model(
    "HRDPS (Canada 2.5km)",
    primary=("open-meteo", _om_url("gem")),
    fallback=None,
    run_id="hrdps",
    in_coverage=_in_bounds(_HRDPS_BOUNDS),
)

# HRRR CONUS — Meteomatics primary, Open-Meteo fallback
_register_model(
    "HRRR (CONUS 3km)",
    primary=("meteomatics", "ncep-hrrr"),
    fallback=("open-meteo", _om_url("gfs?models=ncep_hrrr_conus")),
    run_id="hrrr",
    in_coverage=_in_bounds(_HRRR_NAM_BOUNDS),
)

# NAM CONUS — Open-Meteo only (Meteomatics doesn't carry it)
_register_model(
    "NAM (CONUS 3km)",
    primary=("open-meteo", _om_url("gfs?models=ncep_nam_conus")),
    fallback=None,
    run_id="nam",
    in_coverage=_in_bounds(_HRRR_NAM_BOUNDS),
)

# ICON Global — Open-Meteo only (DWD ICON not in current Meteomatics subscription)
_register_model(
    "ICON (Global 13km)",
    primary=("open-meteo", _om_url("dwd-icon")),
    fallback=None,
    run_id="icon",
    in_coverage=True,
)

# ICON-EU — Open-Meteo only (regional Europe)
_register_model(
    "ICON-EU (Europe 7km)",
    primary=("open-meteo", _om_url("dwd-icon")),
    fallback=None,
    run_id="icon-eu",
    in_coverage=_in_bounds(_ICONEU_BOUNDS),
)

# Build dropdown of in-coverage models
_in_coverage_models = [name for name, (_route, _id, ok) in _all_models.items() if ok]
_out_of_coverage = [name for name, (_route, _id, ok) in _all_models.items() if not ok]

# Priority ordering — Meteomatics MIX surfaces first as the operational default;
# then highest-resolution regional models, then global models.
_priority = [
    "Meteomatics MIX",                  # default first-load (best 0-24h)
    "HRDPS (Canada 2.5km)",             # Canada regional
    "HRRR (CONUS 3km)",                 # CONUS rapid update
    "NAM (CONUS 3km)",                  # CONUS longer horizon
    "ICON-EU (Europe 7km)",             # Europe regional
    "ECMWF IFS (Global 9km)",           # global high-quality
    "ECMWF AIFS (AI model)",            # global AI
    "GFS (Global 13km)",                # global American
    "ICON (Global 13km)",               # global German
]
_model_options = sorted(_in_coverage_models,
                        key=lambda n: _priority.index(n) if n in _priority else 99)

# Default = Meteomatics MIX when available, otherwise the first in-coverage model
_default_model = "Meteomatics MIX" if "Meteomatics MIX" in _model_options else (_model_options[0] if _model_options else None)
_default_idx = _model_options.index(_default_model) if _default_model in _model_options else 0

model_choice = st.sidebar.selectbox(
    "Forecast Model:",
    options=_model_options,
    index=_default_idx,
)

# Show what's NOT available so the operator understands the gap
if _out_of_coverage:
    _unavail_str = ", ".join(name.split(" (")[0] for name in _out_of_coverage)
    st.sidebar.caption(f"Not available at this location: {_unavail_str}")


def log_refresh_callback():
    """Selectively invalidates forecast/observation caches without nuking
    expensive long-TTL caches (climate context: 24h TTL, astronomy: 24h TTL).

    Targeted caches:
      - fetch_weather_payload (forecast + run-info, 15min TTL)
      - fetch_metar_taf (5min TTL)
      - Model performance scorecard (30min TTL) — clears via its own decorator
    """
    for cache_fn_name in (
        "fetch_weather_payload",
        "fetch_metar_taf",
        "fetch_space_weather_cached",
    ):
        fn = globals().get(cache_fn_name)
        if fn is not None and hasattr(fn, "clear"):
            try:
                fn.clear()
            except Exception:
                pass
    try:
        log_action(st.session_state.get("active_operator", "UNKNOWN"),
                   lat, lon, icao, "MANUAL_REFRESH")
    except Exception:
        pass


st.sidebar.button("Force Manual Data Refresh", on_click=log_refresh_callback)

# Fetch the forecast via the provider routing layer. The model registry
# resolves the primary/fallback providers internally based on model_choice.
data = fetch_weather_payload(lat, lon, model_choice)

# -----------------------------------------------------------------------------
# PROVIDER HEALTH TRACKING
# -----------------------------------------------------------------------------
# Track Meteomatics health across the session so a failure that triggered
# fallback to Open-Meteo earlier in this session remains visible even after
# the operator switches to a model that's Open-Meteo-only (and thus wouldn't
# naturally surface a Meteomatics error).
if "_mm_health" not in st.session_state:
    st.session_state["_mm_health"] = {
        "last_failure_at": None,    # UTC datetime of most recent Meteomatics failure
        "last_failure_msg": None,
        "last_success_at": None,    # UTC datetime of most recent successful Meteomatics call
        "failed_models": set(),     # set of model labels where Meteomatics failed during this session
    }

_health = st.session_state["_mm_health"]

# Update health state based on this fetch result. Only meaningful when the
# selected model actually has Meteomatics as primary — otherwise this fetch
# tells us nothing about Meteomatics' state.
_selected_entry = _all_models.get(model_choice)
if _selected_entry is not None:
    _selected_route, _, _ = _selected_entry
    _selected_primary_is_mm = _selected_route.primary[0] == "meteomatics"
    if _selected_primary_is_mm and isinstance(data, dict):
        if data.get("_primary_failed"):
            _health["last_failure_at"] = datetime.now(timezone.utc)
            _health["last_failure_msg"] = data.get("_primary_error") or "unknown error"
            _health["failed_models"].add(model_choice)
        elif data.get("_served_by") == "meteomatics":
            _health["last_success_at"] = datetime.now(timezone.utc)
            # If we just had a successful Meteomatics call, clear the model from
            # the failed set — failover is no longer "active" for this model.
            _health["failed_models"].discard(model_choice)

# Provider chip — shown in sidebar under model dropdown, summarizing what
# actually served the current selection.
if isinstance(data, dict) and data.get("_served_by"):
    _provider = data["_served_by"]
    if data.get("_primary_failed"):
        # Failover scenario — amber chip
        _chip_color = "#D97706"
        _chip_text = f"via {_provider} (failover)"
        _chip_title = (data.get("_primary_error") or "").replace('"', "'")
    else:
        # Primary served cleanly — slate chip
        _chip_color = "#6B7280"
        _chip_text = f"via {_provider}"
        _chip_title = "Primary provider served this forecast."
    st.sidebar.markdown(
        f"<div style='font-size: 0.78rem; color: {_chip_color}; "
        f"margin-top: -8px; margin-bottom: 10px;' title=\"{_chip_title}\">"
        f"&#x2937; {_chip_text}</div>",
        unsafe_allow_html=True,
    )

if icao != "NONE": metar_raw, taf_raw = fetch_metar_taf(icao)
else:              metar_raw, taf_raw = "NIL", "NIL"

st.title("Atmospheric Risk Management")
st.caption(f"Vector Check Aerial Group Inc. - SYSTEM ACTIVE | OPERATOR: {st.session_state.get('active_operator', 'UNKNOWN')}")

# -----------------------------------------------------------------------------
# METEOMATICS FAILURE BANNER
# -----------------------------------------------------------------------------
# Persistent amber banner shown when Meteomatics has failed during this
# session. Stays visible until the next successful Meteomatics fetch clears
# the failed_models set.
if _health["failed_models"] and _health["last_failure_at"] is not None:
    _failed_list = ", ".join(sorted(_health["failed_models"]))
    _failure_time_str = _health["last_failure_at"].strftime("%H:%M UTC")
    _last_success_str = (_health["last_success_at"].strftime("%H:%M UTC")
                         if _health["last_success_at"] else "never this session")
    st.warning(
        f"⚠ **Meteomatics unavailable since {_failure_time_str}.** "
        f"Failover active for: {_failed_list}.",
        icon="⚠️",
    )
    with st.expander("Show Meteomatics failure details"):
        st.markdown(f"**Last Meteomatics error:** `{_health['last_failure_msg']}`")
        st.markdown(f"**First detected:** {_failure_time_str}")
        st.markdown(f"**Last successful Meteomatics fetch:** {_last_success_str}")
        st.caption(
            "Affected models have automatically failed over to Open-Meteo "
            "where a fallback is configured. Models without an Open-Meteo "
            "fallback (Meteomatics MIX, ECMWF AIFS) are temporarily unavailable. "
            "The banner clears automatically when the next Meteomatics call succeeds."
        )

st.divider()

if data is None:
    st.error("⚠️ CRITICAL: Atmospheric Data API Offline.")
    st.stop()
elif "error" in data and data["error"]:
    _err_msg = data.get('message', 'Unknown Error')
    st.error(f"⚠️ CRITICAL API REJECTION: {_err_msg}")
    _route_for_msg = _all_models.get(model_choice, (None, None, None))[0]
    _has_fallback = _route_for_msg is not None and _route_for_msg.fallback is not None
    if data.get("_primary_failed") and _has_fallback:
        # Both providers failed
        st.caption(
            f"Model **{model_choice}** failed to return data from both its primary "
            "provider and the configured fallback. This usually indicates a wider "
            "outage across multiple weather data services rather than a problem "
            "with a single provider. Try selecting a different model from the "
            "sidebar — models routed through different providers will be unaffected."
        )
    elif data.get("_primary_failed"):
        # No fallback configured — Meteomatics-only model failed
        st.caption(
            f"Model **{model_choice}** is served exclusively by Meteomatics and "
            "has no Open-Meteo fallback. Meteomatics returned an error for this "
            "request. Try a different model (e.g. ECMWF IFS or GFS) which can "
            "fail over to Open-Meteo."
        )
    else:
        # Generic / Open-Meteo-primary failure
        st.caption(
            f"Model **{model_choice}** failed to return data for these coordinates. "
            "This may be due to (1) a temporary outage in that model's data feed, "
            "(2) the location falling outside the model's resolved grid, or "
            "(3) a specific variable being unsupported by this model. "
            "Try selecting a different model from the sidebar (e.g. ECMWF or GFS as a stable fallback)."
        )
    st.stop()
elif "hourly" not in data:
    st.error("⚠️ CRITICAL: Malformed data payload received from server.")
    st.stop()


# =============================================================================
# TIME PARSING
# =============================================================================
tf     = TimezoneFinder()
tz_str = tf.timezone_at(lng=lon, lat=lat)
local_tz = pytz.timezone(tz_str) if tz_str else timezone.utc
tz_abbr  = datetime.now(local_tz).tzname() if tz_str else "UTC"

h         = data["hourly"]
# Robust wind-unit detection. Defaults to knots since Open-Meteo is now asked
# to serve knots directly via &wind_speed_unit=kn (see data_ingest.py).
# Handles all four units Open-Meteo can emit, so a missing or unexpected
# wind_speed_unit flag never silently produces wrong wind values.
_wind_unit_raw = data.get("hourly_units", {}).get("wind_speed_10m", "kn").lower()
if "km/h" in _wind_unit_raw:
    k_conv = 0.539957     # km/h -> kt
elif "m/s" in _wind_unit_raw:
    k_conv = 1.943844     # m/s -> kt
elif "mph" in _wind_unit_raw:
    k_conv = 0.868976     # mph -> kt
else:
    k_conv = 1.0          # already knots
is_kmh    = (k_conv == 0.539957)   # retained for backward compatibility with downstream
raw_wind_unit  = "KT"
# Defensive: providers may return None (vs absent) for elevation. Treat both
# the same — `data.get("elevation", 0)` returns None when the key is present
# but unset, which breaks the multiplication below. The data_ingest layer
# normally backfills elevation from Open-Meteo's free endpoint, but if that
# also fails (e.g. simultaneous outage), default to 0 m and continue.
sfc_elevation  = (data.get('elevation') or 0) * 3.28084

times_display = []
for t_str in h["time"]:
    dt_u = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
    dt_l = dt_u.astimezone(local_tz)
    times_display.append(f"{dt_u.strftime('%d %b %H:%M')} Z | {dt_l.strftime('%H:%M')} {tz_abbr}")

now_utc      = datetime.now(timezone.utc)
time_diffs   = [abs((datetime.fromisoformat(t).replace(tzinfo=timezone.utc) - now_utc).total_seconds()) for t in h["time"]]
nearest_idx  = time_diffs.index(min(time_diffs))
max_idx      = min(len(h["time"]) - 1, nearest_idx + 72)
valid_times_display = times_display[nearest_idx : max_idx + 1]

if "forecast_slider" not in st.session_state or st.session_state.forecast_slider not in valid_times_display:
    st.session_state.forecast_slider = valid_times_display[0]


# =============================================================================
# IMPACT MATRIX UI
# =============================================================================

# Run cycle info is now embedded in the cached weather payload so it can never
# drift out of sync with the forecast data on screen. See fetch_weather_payload.
_run_info = data.get("_run_info", {}) if isinstance(data, dict) else {}

# Build model title with run cycle indicator
if _run_info and _run_info.get("run_cycle_z"):
    _run_age = _run_info.get("age_hours", 0)
    _run_age_str = f"{_run_age}h ago" if _run_age < 24 else f"{_run_age // 24}d {_run_age % 24}h ago"
    _matrix_title = f"Impact Matrix \u2014 {model_choice.split(' ')[0]} {_run_info['run_cycle_z']} run"
    _matrix_sub = f"Initialized {_run_info['run_date']} {_run_info['run_cycle_z']} \u00b7 {_run_age_str} \u00b7 72h tactical window"
else:
    _matrix_title = f"Impact Matrix \u2014 {model_choice.split(' ')[0]}"
    _matrix_sub = "72h tactical window"

st.subheader(_matrix_title)
st.caption(_matrix_sub)

with st.expander("Configure Operational Constraints"):
    # Two-row layout: each constraint has an enable toggle + threshold input.
    # Toggles let the operator turn off any constraint that doesn't apply to
    # the current op (e.g. ignore icing for a daylight summer flight, or
    # add a Max Temp gate for battery-temperature-sensitive RPAS work).
    st.caption("Toggle constraints on/off; disabled constraints are skipped in the Impact Matrix.")

    # Row 1: existing constraints
    r1c1, r1c2, r1c3, r1c4 = st.columns(4)

    with r1c1:
        en_wind = st.toggle("Max Wind/Gust", value=st.session_state.get('en_wind', True), key='en_wind')
        t_wind = st.number_input("KT", value=int(st.session_state.get('input_wind', 30)),
                                  key='input_wind', label_visibility="collapsed",
                                  disabled=not en_wind)
    with r1c2:
        en_ceil = st.toggle("Min Ceiling", value=st.session_state.get('en_ceil', True), key='en_ceil')
        t_ceil = st.number_input("ft AGL", value=int(st.session_state.get('input_ceil', 500)),
                                  step=100, key='input_ceil', label_visibility="collapsed",
                                  disabled=not en_ceil)
    with r1c3:
        en_vis = st.toggle("Min Visibility", value=st.session_state.get('en_vis', True), key='en_vis')
        t_vis = st.number_input("SM", value=float(st.session_state.get('input_vis', 3.0)),
                                 step=0.5, key='input_vis', label_visibility="collapsed",
                                 disabled=not en_vis)
    with r1c4:
        en_turb = st.toggle("Max Turbulence", value=st.session_state.get('en_turb', True), key='en_turb')
        turb_opts = ["NIL", "LGT", "MOD", "SEV"]
        _turb_default = st.session_state.get('input_turb', 'MOD')
        turb_idx = turb_opts.index(_turb_default) if _turb_default in turb_opts else 2
        t_turb = st.selectbox("Turb", turb_opts, index=turb_idx, key='input_turb',
                               label_visibility="collapsed", disabled=not en_turb)

    # Row 2: existing icing + new temp/RH constraints
    r2c1, r2c2, r2c3, r2c4 = st.columns(4)

    with r2c1:
        en_ice = st.toggle("Max Icing", value=st.session_state.get('en_ice', True), key='en_ice')
        ice_opts = ["NIL", "LGT", "MOD", "SEV"]
        _ice_default = st.session_state.get('input_ice', 'NIL')
        ice_idx = ice_opts.index(_ice_default) if _ice_default in ice_opts else 0
        t_ice = st.selectbox("Ice", ice_opts, index=ice_idx, key='input_ice',
                              label_visibility="collapsed", disabled=not en_ice)
    with r2c2:
        # Max temperature: triggers when surface temp exceeds this value.
        # Default disabled — opt-in for battery-sensitive ops or hot-weather limits.
        en_tmax = st.toggle("Max Temperature", value=st.session_state.get('en_tmax', False), key='en_tmax')
        t_tmax = st.number_input("\u00b0C", value=float(st.session_state.get('input_tmax', 40.0)),
                                   step=1.0, key='input_tmax', label_visibility="collapsed",
                                   disabled=not en_tmax)
    with r2c3:
        # Min temperature: triggers when surface temp drops below this value.
        # Default disabled — opt-in for cold-weather battery and structural limits.
        en_tmin = st.toggle("Min Temperature", value=st.session_state.get('en_tmin', False), key='en_tmin')
        t_tmin = st.number_input("\u00b0C", value=float(st.session_state.get('input_tmin', -20.0)),
                                   step=1.0, key='input_tmin', label_visibility="collapsed",
                                   disabled=not en_tmin)
    with r2c4:
        # Max relative humidity: triggers when RH exceeds this value.
        # Useful for condensation / optical sensor degradation in payload-heavy missions.
        en_rhmax = st.toggle("Max Relative Humidity", value=st.session_state.get('en_rhmax', False), key='en_rhmax')
        t_rhmax = st.number_input("%", value=int(st.session_state.get('input_rhmax', 95)),
                                    step=5, key='input_rhmax', label_visibility="collapsed",
                                    disabled=not en_rhmax)

# Run the cached physics computation — only re-executes when inputs change
x_labels, color_vals, hover_texts = compute_impact_matrix(
    h_data        = h,
    nearest_idx   = nearest_idx,
    max_idx       = max_idx,
    sfc_elevation = sfc_elevation,
    k_conv        = k_conv,
    t_wind        = t_wind,
    t_ceil        = t_ceil,
    t_vis         = t_vis,
    t_turb        = t_turb,
    t_ice         = t_ice,
    terrain_env   = terrain_env,
    tz_str        = tz_str or "UTC",
    en_wind       = en_wind,
    en_ceil       = en_ceil,
    en_vis        = en_vis,
    en_turb       = en_turb,
    en_ice        = en_ice,
    en_tmax       = en_tmax,
    t_tmax        = t_tmax,
    en_tmin       = en_tmin,
    t_tmin        = t_tmin,
    en_rhmax      = en_rhmax,
    t_rhmax       = t_rhmax,
)

tick_vals  = x_labels[::4]
tick_texts = []
_last_tick_date = None
for val in tick_vals:
    tick_idx    = nearest_idx + x_labels.index(val)
    dt_local_tk = datetime.fromisoformat(h["time"][tick_idx]).replace(tzinfo=timezone.utc).astimezone(local_tz)
    _this_date = dt_local_tk.strftime('%d %b')
    # Show the date on the first tick of each day; hour-only on subsequent ticks within the same day
    if _this_date != _last_tick_date:
        tick_texts.append(f"{_this_date}<br>{dt_local_tk.strftime('%H:%M')}")
        _last_tick_date = _this_date
    else:
        tick_texts.append(dt_local_tk.strftime('%H:%M'))

fig_matrix = go.Figure(data=go.Bar(
    x=x_labels,
    y=[1] * len(x_labels),
    marker_color=color_vals,
    customdata=hover_texts,
    hovertemplate="%{customdata}<extra></extra>",
    width=1
))

current_selected = st.session_state.forecast_slider
try:
    selected_idx    = valid_times_display.index(current_selected)
    selected_x_label = x_labels[selected_idx]
except ValueError:
    selected_x_label = x_labels[0]

fig_matrix.add_trace(go.Scatter(
    x=[selected_x_label],
    y=[-0.15],
    mode="markers",
    marker=dict(symbol="line-ew", color="#E58E26", size=14, line=dict(width=4, color="#E58E26")),
    hoverinfo="skip"
))

fig_matrix.update_layout(
    height=85,
    margin=dict(l=0, r=0, t=0, b=45),
    plot_bgcolor="#1B1E23",
    paper_bgcolor="#1B1E23",
    xaxis=dict(
        tickmode='array', tickvals=tick_vals, ticktext=tick_texts,
        tickangle=0, tickfont=dict(color="#A0A4AB", size=10, family="Source Sans Pro, sans-serif"),
        showgrid=False, zeroline=False, fixedrange=True
    ),
    yaxis=dict(showticklabels=False, showgrid=False, zeroline=False, range=[-0.25, 1], fixedrange=True),
    dragmode=False, showlegend=False
)

try:
    event = st.plotly_chart(fig_matrix, use_container_width=True, on_select="rerun", selection_mode="points", key="impact_matrix_chart", config={'displayModeBar': False})
    if event and "selection" in event and "points" in event["selection"] and len(event["selection"]["points"]) > 0:
        point_data  = event["selection"]["points"][0]
        clicked_idx = point_data.get("point_index", point_data.get("pointIndex", None))
        if clicked_idx is not None:
            target_time = valid_times_display[clicked_idx]
            if st.session_state.get("forecast_slider") != target_time:
                st.session_state.forecast_slider = target_time
                st.rerun()
except Exception:
    pass

st.markdown("<div style='margin-bottom: 20px;'></div>", unsafe_allow_html=True)


# =============================================================================
# FORECAST DASHBOARD EXECUTION (SINGLE HOUR)
# =============================================================================

def update_time(offset: int) -> None:
    current_val = st.session_state.forecast_slider
    try:
        current_idx_in_valid = valid_times_display.index(current_val)
        new_idx_in_valid     = max(0, min(len(valid_times_display) - 1, current_idx_in_valid + offset))
        st.session_state.forecast_slider = valid_times_display[new_idx_in_valid]
    except ValueError:
        st.session_state.forecast_slider = valid_times_display[0]


selected_time_str = st.sidebar.select_slider("Forecast Time:", options=valid_times_display, key="forecast_slider")
forecast_idx      = times_display.index(selected_time_str)   # renamed from idx → forecast_idx
relative_hr       = valid_times_display.index(selected_time_str)

nav_col1, nav_col2, nav_col3 = st.sidebar.columns([1, 2, 1])
nav_col1.button("◄", on_click=update_time, args=(-1,), use_container_width=True)
nav_col2.markdown(f"<div style='text-align: center; font-size: 1.1rem; font-weight: bold; color: #E58E26; margin-top: 5px;'>+ {relative_hr} HR</div>", unsafe_allow_html=True)
nav_col3.button("►", on_click=update_time, args=(1,), use_container_width=True)

st.sidebar.divider()


# --- 1. CORE EXTRACTIONS ---
t_temp_raw = h.get('temperature_2m', [0])[forecast_idx]
t_temp     = float(t_temp_raw) if t_temp_raw is not None else 0.0

rh_raw = h.get('relative_humidity_2m', [0])[forecast_idx]
rh     = int(rh_raw) if rh_raw is not None else 0

w_spd_raw = h.get('wind_speed_10m', [0])[forecast_idx]
w_spd     = (float(w_spd_raw) if w_spd_raw is not None else 0.0) * k_conv

wx_list = h.get('weather_code', [0])
wx      = int(wx_list[forecast_idx]) if (wx_list and len(wx_list) > forecast_idx and wx_list[forecast_idx] is not None) else 0

td         = calc_td(t_temp, rh)
sfc_spread = t_temp - td

sn_depth_raw = h.get('snow_depth', [0])
sn_depth     = float(sn_depth_raw[forecast_idx]) if sn_depth_raw and len(sn_depth_raw) > forecast_idx and sn_depth_raw[forecast_idx] is not None else 0.0

sfc_dir_raw = h.get('wind_direction_10m', [0])[forecast_idx]
sfc_dir     = format_dir(float(sfc_dir_raw) if sfc_dir_raw is not None else 0.0, w_spd)

vis_raw_list = h.get('visibility')
vis_raw_val  = vis_raw_list[forecast_idx] if vis_raw_list and len(vis_raw_list) > forecast_idx else None
vis_sm       = calc_tactical_visibility(vis_raw_val, rh, w_spd, wx)


# --- 2. THERMAL PROFILE ---
thermal_profile = [{'h': sfc_elevation, 't': t_temp, 'td': td, 'spread': sfc_spread, 'rh': rh}]
for p in ALL_P_LEVELS:
    gh_list = h.get(f'geopotential_height_{p}hPa')
    t_list  = h.get(f'temperature_{p}hPa')
    rh_list = h.get(f'relative_humidity_{p}hPa')
    if gh_list and t_list and rh_list and len(gh_list) > forecast_idx:
        if gh_list[forecast_idx] is not None and t_list[forecast_idx] is not None and rh_list[forecast_idx] is not None:
            p_gh = float(gh_list[forecast_idx]) * 3.28084
            p_t  = float(t_list[forecast_idx])
            p_rh = int(rh_list[forecast_idx])
            p_td = calc_td(p_t, p_rh)
            if p_gh > thermal_profile[-1]['h']:
                thermal_profile.append({'h': p_gh, 't': p_t, 'td': p_td, 'spread': p_t - p_td, 'rh': p_rh})


# --- 3. ADVANCED METRICS ---
sfc_press_raw = h.get('surface_pressure')
if sfc_press_raw and len(sfc_press_raw) > forecast_idx and sfc_press_raw[forecast_idx] is not None:
    sfc_press = float(sfc_press_raw[forecast_idx])
else:
    sfc_press = 1013.25

density_alt = calculate_density_altitude(sfc_elevation, t_temp, sfc_press)

pop_raw = h.get('precipitation_probability', [0])
pop     = int(pop_raw[forecast_idx]) if pop_raw and len(pop_raw) > forecast_idx and pop_raw[forecast_idx] is not None else 0

precip_raw = h.get('precipitation', [0])
precip     = float(precip_raw[forecast_idx]) if precip_raw and len(precip_raw) > forecast_idx and precip_raw[forecast_idx] is not None else 0.0

cape_raw = h.get('cape', [0])
cape     = int(cape_raw[forecast_idx]) if cape_raw and len(cape_raw) > forecast_idx and cape_raw[forecast_idx] is not None else 0

frz_raw_list = h.get('freezing_level_height')
if frz_raw_list and len(frz_raw_list) > forecast_idx and frz_raw_list[forecast_idx] is not None:
    frz_raw  = float(frz_raw_list[forecast_idx])
    frz_disp = "SFC" if t_temp <= 0 else f"{int(round(frz_raw * 3.28084, -2)):,} ft"
else:
    if t_temp <= 0:
        frz_disp = "SFC"
    else:
        frz_disp = "> 10,000 ft"
        for k in range(1, len(thermal_profile)):
            lower, upper = thermal_profile[k - 1], thermal_profile[k]
            if upper['t'] <= 0:
                t_diff   = lower['t'] - upper['t']
                frz_h    = lower['h'] + (lower['t'] / t_diff) * (upper['h'] - lower['h']) if t_diff > 0 else lower['h']
                frz_disp = f"{int(round(frz_h, -2)):,} ft"
                break

t_925_list    = h.get('temperature_925hPa')
t_925         = float(t_925_list[forecast_idx]) if (t_925_list and len(t_925_list) > forecast_idx and t_925_list[forecast_idx] is not None) else t_temp
lapse_rate_temp_drop = t_temp - t_925
is_convective = (wx >= 80) or (lapse_rate_temp_drop >= 7.5 and t_temp >= 10.0)


# --- THERMAL PHASE GATE (PRECIPITATION VETO) ---
cb_v = ct_v = None
for layer in thermal_profile:
    if layer['spread'] <= 4.0:
        if cb_v is None: cb_v = layer['h']
        ct_v = layer['h']
c_depth = (ct_v - cb_v) if cb_v and ct_v else 0

if 50 <= wx <= 59 and (c_depth >= 2500 or precip >= 0.5 or is_convective):
    frz_raw_sh  = float(frz_raw_list[forecast_idx]) if frz_raw_list and len(frz_raw_list) > forecast_idx and frz_raw_list[forecast_idx] is not None else 0.0
    frz_agl_sh  = max(0, (frz_raw_sh * 3.28084) - sfc_elevation)
    warm_nose   = any(layer['t'] > 0 for layer in thermal_profile[1:])
    is_heavy    = wx in [54, 55, 57]

    if t_temp <= 0:
        wx = (67 if is_heavy else 66) if warm_nose else (73 if is_heavy else 71)
    elif 0 < t_temp <= 2.5 and frz_agl_sh < 1500:
        wx = 69 if is_heavy else 68
    else:
        wx = 63 if is_heavy else 61


# --- CLOUD BASE CASCADE ---
c_base_agl  = 99999
c_amt       = "CLR"
c_base_disp = "CLR"

search_profile = thermal_profile[1:] if len(thermal_profile) > 1 else thermal_profile

for layer in search_profile:
    h_agl = max(0, layer['h'] - sfc_elevation)
    if layer['spread'] <= 3.0:
        if h_agl < 1000 and sfc_spread <= 3.0 and vis_sm >= 1.5 and wx < 50: continue
        c_base_agl = int(round(h_agl, -2))
        c_amt      = "OVC" if layer['spread'] <= 1.0 else "BKN"
        if c_base_agl == 0:
            if vis_sm > 0.62 and wx not in [45, 48]: c_base_agl = 100
            else:                                      c_amt      = "VV"
        break

if c_amt == "CLR":
    for layer in search_profile:
        h_agl = max(0, layer['h'] - sfc_elevation)
        if layer['spread'] <= 5.0:
            if h_agl < 1000 and sfc_spread <= 3.0 and vis_sm >= 1.5 and wx < 50: continue
            c_base_agl = int(round(h_agl, -2))
            c_amt      = "SCT"
            if c_base_agl == 0 and vis_sm > 0.62 and wx not in [45, 48]: c_base_agl = 100
            break

if c_amt == "CLR":
    for layer in search_profile:
        h_agl = max(0, layer['h'] - sfc_elevation)
        if layer['spread'] <= 7.0:
            if h_agl < 1000 and sfc_spread <= 3.0 and vis_sm >= 1.5 and wx < 50: continue
            c_base_agl = int(round(h_agl, -2))
            c_amt      = "FEW"
            if c_base_agl == 0 and vis_sm > 0.62 and wx not in [45, 48]: c_base_agl = 100
            break

if is_convective and c_amt == "CLR":
    ccl_base = int(round(max(0, sfc_spread * CONVECTIVE_CCL_MULTIPLIER), -2))
    if ccl_base < 10000:
        c_base_agl = ccl_base
        c_amt      = "BKN" if wx >= 80 else "SCT"

c_base_disp = f"{c_base_agl:,} ft {c_amt}" if c_amt != "CLR" else "CLR"


# --- GUST & BLSN/DRSN ---
raw_gst_list = h.get('wind_gusts_10m')
raw_gst      = (float(raw_gst_list[forecast_idx]) * k_conv) if (raw_gst_list and len(raw_gst_list) > forecast_idx and raw_gst_list[forecast_idx] is not None) else None
gst          = resolve_gust(w_spd, raw_gst)

# BLSN / DRSN kinetic gate — single authoritative implementation in
# modules/atmosphere.py. Returns trigger flags + a vis_sm adjusted for blowing
# snow obscuration (the dashboard previously set the flag but forgot to
# apply the visibility reduction).
blsn_trigger, drsn_trigger, vis_sm = evaluate_blsn(
    wx=wx, t_temp=t_temp, w_spd=w_spd, gst=gst,
    sn_depth=sn_depth, vis_sm=vis_sm,
)

weather_str = get_weather_element(wx, w_spd)

if wx in [45, 48]:
    if rh < 85:
        weather_str = "HAZE (HZ)" if rh >= 75 else "CLEAR"
    else:
        weather_str = "FREEZING FOG (FZFG)" if t_temp <= 0 else "FOG (FG)"
elif wx < 40 and vis_sm < 7.0:
    if rh >= 85:
        if vis_sm <= 0.62: weather_str = "FREEZING FOG (FZFG)" if t_temp <= 0 else "FOG (FG)"
        else:              weather_str = "MIST (BR)"
    elif rh >= 75:
        weather_str = "HAZE (HZ)"

# Pre-compute is_snowing from the same wx codes evaluate_blsn() uses
# internally. Needed so we can render "SNOW & BLSN" vs plain "BLSN" depending
# on whether snow is also actively falling. Codes match WMO ww:
#   71/73/75 — light/moderate/heavy snow
#   77       — snow grains
#   85/86    — snow showers (light/heavy)
#   68/69    — sleet (rain & snow mixed)
is_snowing = wx in [71, 73, 75, 77, 85, 86, 68, 69]

if blsn_trigger:
    weather_str = f"{weather_str} & BLSN" if is_snowing else "BLOWING SNOW (BLSN)"
elif drsn_trigger:
    weather_str = "DRIFTING SNOW (DRSN)"

vis_disp = "> 7 SM" if vis_sm > 7 else f"{vis_sm:.1f} SM"


# --- 4. EXACT AGL INJECTION FOR TACTICAL STACK ---
w_80_raw  = h.get('wind_speed_80m',   [None])[forecast_idx]
w_120_raw = h.get('wind_speed_120m',  [None])[forecast_idx]
d_80_raw  = h.get('wind_direction_80m',  [None])[forecast_idx]
d_120_raw = h.get('wind_direction_120m', [None])[forecast_idx]

has_agl_data = w_80_raw is not None and w_120_raw is not None

if has_agl_data:
    w_80  = float(w_80_raw)  * k_conv
    w_120 = float(w_120_raw) * k_conv
    d_80  = float(d_80_raw)  if d_80_raw  is not None else sfc_dir
    d_120 = float(d_120_raw) if d_120_raw is not None else d_80
    # These scalars anchor the extended trajectory baseline
    u_v, u_h, u_dir = w_120, 120.0, d_120
else:
    # Fallback: derive upper reference from pressure-level data
    u_v_list = h.get('wind_speed_1000hPa')
    if u_v_list and len(u_v_list) > forecast_idx and u_v_list[forecast_idx] is not None:
        u_v   = float(u_v_list[forecast_idx]) * k_conv
        u_dir = int(h.get('wind_direction_1000hPa', [0])[forecast_idx])
        u_h_list = h.get('geopotential_height_1000hPa')
        u_h   = float(u_h_list[forecast_idx]) if (u_h_list and len(u_h_list) > forecast_idx and u_h_list[forecast_idx] is not None) else 110.0
    else:
        u_v_list = h.get('wind_speed_925hPa')
        if u_v_list and len(u_v_list) > forecast_idx and u_v_list[forecast_idx] is not None:
            u_v   = float(u_v_list[forecast_idx]) * k_conv
            u_dir = int(h.get('wind_direction_925hPa', [0])[forecast_idx])
            u_h_list = h.get('geopotential_height_925hPa')
            u_h   = float(u_h_list[forecast_idx]) if (u_h_list and len(u_h_list) > forecast_idx and u_h_list[forecast_idx] is not None) else 760.0
        else:
            u_v, u_dir, u_h = w_spd, sfc_dir, 10.0

icing_cond       = calculate_icing_profile(h, forecast_idx, wx)
dt_utc_exact_iso = h["time"][forecast_idx]
astro            = fetch_astronomy_cached(lat, lon, dt_utc_exact_iso, tz_str, tz_abbr)
space_data       = fetch_space_weather_cached(dt_utc_exact_iso)

sun_pos_display  = f"{astro['sun_dir']} | Elev: {astro['sun_alt']}°" if astro['sun_alt'] > 0 else "NIL"
moon_pos_display = f"{astro['moon_dir']} | Elev: {astro['moon_alt']}°" if astro['moon_alt'] > 0 else "NIL"

if int(w_spd) == 0:   sfc_dir_disp, sfc_spd_disp = "CALM", "0"
elif int(w_spd) <= 3: sfc_dir_disp, sfc_spd_disp = "VRB",  "3"
else:                  sfc_dir_disp, sfc_spd_disp = f"{sfc_dir:03d}°", str(int(w_spd))


# =============================================================================
# UI RENDERING
# =============================================================================

st.subheader("Forecasted Surface Data")
c = st.columns(9)
c[0].metric("Temp",        f"{t_temp}\u00b0C")
c[1].metric("RH",          f"{rh}%")
c[2].metric("Wind Dir",    sfc_dir_disp)
c[3].metric("Wind Spd",    f"{sfc_spd_disp} {raw_wind_unit}")
# Gust column. resolve_gust() returns w_spd when there's no meaningful gust
# (gust factor below the 3 kt threshold), so when gst == w_spd we show no
# gust rather than echoing the sustained wind.
_gust_disp = f"{int(gst)} {raw_wind_unit}" if gst > w_spd else "\u2014"
c[4].metric("Gust",        _gust_disp)
c[5].metric("Weather",     weather_str)
c[6].metric("Visibility",  vis_disp)
c[7].metric("Freezing LVL", frz_disp)
c[8].metric("Cloud Base",  c_base_disp)

st.divider()

st.subheader("Tactical Hazard Stack (0-400ft AGL)")
stack_tactical = []
gust_delta     = max(0, gst - w_spd)

for alt in [400, 300, 200, 100]:
    alt_m = alt * 0.3048

    if has_agl_data:
        if alt_m <= 80:
            frac  = (alt_m - 10) / (80 - 10) if alt_m > 10 else 0
            s_c   = w_spd + frac * (w_80 - w_spd)
            d_c_raw = sfc_dir + frac * ((d_80 - sfc_dir + 180) % 360 - 180)
        elif alt_m <= 120:
            frac  = (alt_m - 80) / (120 - 80)
            s_c   = w_80 + frac * (w_120 - w_80)
            d_c_raw = d_80 + frac * ((d_120 - d_80 + 180) % 360 - 180)
        else:
            s_c   = w_120
            d_c_raw = d_120
    else:
        s_c     = w_spd + (u_v - w_spd) * (math.log(max(1, alt_m) / 10) / math.log(max(1.1, u_h / 10)))
        d_c_raw = sfc_dir + ((u_dir - sfc_dir + 180) % 360 - 180) * (min(alt_m, u_h) / max(0.1, u_h))

    g_c     = s_c + gust_delta
    d_c     = format_dir(d_c_raw % 360, s_c)
    alt_msl = sfc_elevation + alt
    alt_t, alt_rh = get_interp_thermals(alt_msl, thermal_profile)
    turb, ice     = get_turb_ice(alt, s_c, w_spd, g_c, wx, is_convective, icing_cond, alt_t, alt_rh, terrain_env, c_base_agl)

    if int(s_c) == 0:   mat_dir, mat_spd = "CALM", "0"
    elif int(s_c) <= 3: mat_dir, mat_spd = "VRB",  "3"
    else:                mat_dir, mat_spd = f"{d_c:03d}°", str(int(s_c))

    stack_tactical.append({
        "Alt (AGL)": f"{alt}ft",
        "Dir": mat_dir,
        f"Spd ({raw_wind_unit})": mat_spd,
        f"Gust ({raw_wind_unit})": (str(int(g_c)) if gust_delta > 0 else "\u2014"),
        "Temp (\u00b0C)": f"{alt_t:.0f}" if alt_t is not None else "N/A",
        "Turbulence": turb,
        "Icing": ice,
    })

df_tactical = pd.DataFrame(stack_tactical).set_index("Alt (AGL)")
st.table(df_tactical)

st.subheader("Extended Trajectory (1,000-5,000ft AGL)")
p_levels_traj = [1000, 925, 850, 700]

p_profile = []
for p in p_levels_traj:
    ws_list = h.get(f'wind_speed_{p}hPa')
    wd_list = h.get(f'wind_direction_{p}hPa')
    gh_list = h.get(f'geopotential_height_{p}hPa')
    if ws_list and wd_list and gh_list and len(ws_list) > forecast_idx:
        ws, wd, gh = ws_list[forecast_idx], wd_list[forecast_idx], gh_list[forecast_idx]
        if ws is not None and wd is not None and gh is not None:
            p_profile.append({'h': float(gh) * 3.28, 's': float(ws) * k_conv, 'd': int(wd)})

p_profile  = sorted(p_profile, key=lambda x: x['h'])
stack_ext  = []

if not p_profile:
    for alt in [5000, 4000, 3000, 2000, 1000]:
        stack_ext.append({
            "Alt (AGL)": f"{alt}ft",
            "Dir": "N/A",
            f"Spd ({raw_wind_unit})": "N/A",
            f"Gust ({raw_wind_unit})": "N/A",
            "Temp (\u00b0C)": "N/A",
            "Turbulence": "N/A",
            "Icing": "N/A",
        })
else:
    for alt in [5000, 4000, 3000, 2000, 1000]:
        pts = [{'h': u_h * 3.28, 's': u_v, 'd': u_dir}] + p_profile
        blw, abv = pts[0], pts[-1]
        for p_i in range(len(pts) - 1):        # FIX: renamed loop var from i → p_i
            if pts[p_i]['h'] <= alt <= pts[p_i + 1]['h']:
                blw, abv = pts[p_i], pts[p_i + 1]
                break

        frac    = (alt - blw['h']) / (abv['h'] - blw['h']) if abv['h'] != blw['h'] else 0
        s_e     = blw['s'] + frac * (abv['s'] - blw['s'])
        d_e_raw = (blw['d'] + ((abv['d'] - blw['d'] + 180) % 360 - 180) * frac) % 360
        d_e     = format_dir(d_e_raw, s_e)
        g_e     = s_e + attenuate_gust_delta(gust_delta, alt)

        alt_msl = sfc_elevation + alt
        alt_t, alt_rh = get_interp_thermals(alt_msl, thermal_profile)
        turb, ice     = get_turb_ice(alt, s_e, w_spd, g_e, wx, is_convective, icing_cond, alt_t, alt_rh, terrain_env, c_base_agl)

        if int(s_e) == 0:   mat_dir_ext, mat_spd_ext = "CALM", "0"
        elif int(s_e) <= 3: mat_dir_ext, mat_spd_ext = "VRB",  "3"
        else:                mat_dir_ext, mat_spd_ext = f"{d_e:03d}°", str(int(s_e))

        stack_ext.append({
            "Alt (AGL)": f"{alt}ft",
            "Dir": mat_dir_ext,
            f"Spd ({raw_wind_unit})": mat_spd_ext,
            f"Gust ({raw_wind_unit})": (str(int(g_e)) if gust_delta > 0 else "\u2014"),
            "Temp (\u00b0C)": f"{alt_t:.0f}" if alt_t is not None else "N/A",
            "Turbulence": turb,
            "Icing": ice,
        })

df_ext = pd.DataFrame(stack_ext).set_index("Alt (AGL)")
st.table(df_ext)

st.divider()

st.subheader(f"Light Profile ({astro['tz']})")
ac1, ac2, ac3, ac4, ac5 = st.columns(5)
ac1.metric("Dawn (Civil)", astro['dawn'])
ac2.metric("Sunrise",      astro['sunrise'])
ac3.metric("Sunset",       astro['sunset'])
ac4.metric("Dusk (Civil)", astro['dusk'])
ac5.metric("Sun Pos",      sun_pos_display)

mc1, mc2, mc3, mc4, mc5 = st.columns(5)
mc1.metric("Moonrise",     astro['moonrise'])
mc2.metric("Moonset",      astro['moonset'])
mc3.metric("Illumination", f"{astro['moon_ill']}%")
mc4.metric("Moon Pos",     moon_pos_display)
mc5.empty()

# ---------------------------------------------------------------------------
# 7-DAY LIGHT PLANNING — cycle of darkness, civil twilight, moon up/down
# ---------------------------------------------------------------------------
st.markdown(
    '<div style="font-size:0.78rem;color:#9CA3AF;text-transform:uppercase;'
    'letter-spacing:0.5px;margin:14px 0 8px;font-weight:500;">Light Planning</div>',
    unsafe_allow_html=True,
)

_lp_c1, _lp_c2, _lp_c3 = st.columns([1.2, 1, 2.4])
with _lp_c1:
    _lp_start = st.date_input(
        "Start date",
        value=datetime.now(local_tz).date(),
        key="lightplan_start",
        help="First night of the planning window. The chart spans this date "
             "forward.",
    )
with _lp_c2:
    _lp_days = st.selectbox(
        "Nights",
        options=[7, 10, 14],
        index=0,
        key="lightplan_days",
    )

@st.cache_data(ttl=3600, show_spinner=False)
def _light_plan_cached(lp_lat: float, lp_lon: float, lp_start_iso: str,
                       lp_n: int, lp_tz: str) -> list:
    """Computes the multi-night light-planning window. Cached 1h per
    (site, start date, length, tz)."""
    _tz = pytz.timezone(lp_tz) if lp_tz else timezone.utc
    _start = datetime.fromisoformat(lp_start_iso).date()
    return get_light_planning_window(lp_lat, lp_lon, _start, lp_n, _tz)

_lp_rows = _light_plan_cached(
    lat, lon, _lp_start.isoformat(), int(_lp_days), tz_str or "UTC"
)

# Build the sleek SVG timeline (18:00 → 06:00 frame). Renders via components
# so the gradients/markers display exactly as designed.
def _render_light_plan_svg(rows: list, tz_abbr_str: str) -> str:
    import json as _json
    data_js = _json.dumps([
        {
            "d": r["day_abbr"], "n": r["day_num"],
            "ll": r["last_light"], "fl": r["first_light"],
            "mr": r["moonrise"], "ms": r["moonset"],
            "ill": r["moon_ill"], "allnight": r["moon_up_all_night"],
        }
        for r in rows
    ])
    n_rows = len(rows)
    # Height: rows * (rowH+gap) + top + axis label band
    svg_h = 6 + n_rows * (26 + 5) + 20
    total_h = svg_h + 60   # + legend
    return f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<div id="lpwrap" style="width:100%;"></div>
<div style="display:flex;gap:18px;align-items:center;flex-wrap:wrap;font-size:11px;color:#9CA3AF;margin-top:10px;padding-left:4px;">
<span style="display:flex;align-items:center;gap:6px;"><span style="width:20px;height:7px;border-radius:4px;background:linear-gradient(90deg,#85B7EB,#0a0a0f);"></span>first / last light</span>
<span style="display:flex;align-items:center;gap:6px;"><span style="width:12px;height:7px;border-radius:4px;background:#050507;border:0.5px solid #2A3038;"></span>cycle of darkness</span>
<span style="display:flex;align-items:center;gap:6px;"><span style="width:12px;height:7px;border-radius:4px;background:#EADfae;"></span>moon up</span>
<span style="color:#6B7280;margin-left:auto;">local time ({tz_abbr_str})</span>
</div>
</div>
<script>
(function(){{
  const days={data_js};
  const W0=18,W1=30,span=W1-W0;
  const L=56,R=16,top=6,rowH=26,gap=5;
  const VBW=680;
  const innerW=VBW-L-R;
  const xc=(h)=>L+((h-W0)/span)*innerW;
  const H={svg_h};
  let svg='<svg viewBox="0 0 '+VBW+' '+H+'" width="100%" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Light planning timeline">';
  svg+='<defs>';
  days.forEach((r,i)=>{{
    if(r.ll==null||r.fl==null){{ svg+='<linearGradient id="g'+i+'"><stop offset="0" stop-color="#85B7EB" stop-opacity="0.42"/></linearGradient>'; return; }}
    const llo=((xc(r.ll)-L)/innerW), flo=((xc(r.fl)-L)/innerW);
    svg+='<linearGradient id="g'+i+'" x1="0" x2="1" y1="0" y2="0">'+
      '<stop offset="0" stop-color="#85B7EB" stop-opacity="0.42"/>'+
      '<stop offset="'+Math.max(0,llo-0.05).toFixed(3)+'" stop-color="#85B7EB" stop-opacity="0.5"/>'+
      '<stop offset="'+Math.max(0,llo).toFixed(3)+'" stop-color="#0a0a12"/>'+
      '<stop offset="'+Math.min(1,flo).toFixed(3)+'" stop-color="#050507"/>'+
      '<stop offset="'+Math.min(1,flo+0.05).toFixed(3)+'" stop-color="#85B7EB" stop-opacity="0.5"/>'+
      '<stop offset="1" stop-color="#85B7EB" stop-opacity="0.42"/></linearGradient>';
  }});
  svg+='</defs>';
  const tickH=[18,19,20,21,22,23,24,25,26,27,28,29,30];
  const tickL=['18','19','20','21','22','23','00','01','02','03','04','05','06'];
  tickH.forEach((th,i)=>{{
    const px=xc(th); const major=(th%3===0);
    svg+='<line x1="'+px+'" y1="'+top+'" x2="'+px+'" y2="'+(top+days.length*(rowH+gap)-gap)+'" stroke="rgba(128,128,128,'+(major?0.14:0.06)+')" stroke-width="1"/>';
    svg+='<text x="'+px+'" y="'+(H-5)+'" text-anchor="middle" font-size="'+(major?9.5:8.5)+'" fill="rgba(150,150,150,'+(major?0.8:0.55)+')">'+tickL[i]+'</text>';
  }});
  days.forEach((r,i)=>{{
    const y=top+i*(rowH+gap); const cy=y+rowH/2;
    svg+='<rect x="'+L+'" y="'+y+'" width="'+innerW+'" height="'+rowH+'" rx="7" fill="url(#g'+i+')"/>';
    if(r.mr!=null||r.ms!=null){{
      const mrx=xc(Math.max(r.mr==null?W0:r.mr,W0));
      const msx=xc(Math.min(r.ms==null?W1:r.ms,W1));
      if(msx>mrx){{
        const op=(r.ill/100*0.55+0.15).toFixed(2);
        svg+='<rect x="'+mrx+'" y="'+(y+rowH-6)+'" width="'+(msx-mrx)+'" height="3.5" rx="1.75" fill="#EADfae" opacity="'+op+'"/>';
        if(r.mr!=null && r.mr>=W0 && r.mr<=W1){{
          const rad=2.2+r.ill/100*3;
          svg+='<circle cx="'+mrx+'" cy="'+(y+rowH-4.25)+'" r="'+rad+'" fill="#EADfae" opacity="'+(r.ill/100*0.6+0.3).toFixed(2)+'"/>';
        }}
        if(r.ms!=null && r.ll!=null && r.ms>r.ll && r.ms<r.fl){{
          svg+='<line x1="'+msx+'" y1="'+(y+2)+'" x2="'+msx+'" y2="'+(y+rowH-2)+'" stroke="#EADfae" stroke-width="1" stroke-dasharray="2 2" opacity="0.4"/>';
        }}
      }}
    }}
    svg+='<text x="'+(L-10)+'" y="'+(cy-1)+'" text-anchor="end" font-size="11" font-weight="500" fill="#E5E7EB">'+r.n+'</text>';
    svg+='<text x="'+(L-10)+'" y="'+(cy+10)+'" text-anchor="end" font-size="8" fill="#6B7280" letter-spacing="0.5">'+r.d+'</text>';
  }});
  svg+='</svg>';
  document.getElementById('lpwrap').innerHTML=svg;
}})();
</script>
"""

with _lp_c3:
    st.empty()

if _lp_rows:
    import streamlit.components.v1 as _components
    _lp_html = _render_light_plan_svg(_lp_rows, astro['tz'])
    _lp_height = 6 + len(_lp_rows) * (26 + 5) + 20 + 50
    _components.html(_lp_html, height=_lp_height, scrolling=False)
else:
    st.caption("Light planning data unavailable for this location/date.")

st.divider()
st.subheader("Space Weather (GNSS & C2 Link)")

risk_color = "#ff4b4b" if space_data['risk'] in ["HIGH (G1)", "SEVERE (G2+)"] else "#D1D5DB"
st.markdown(f"""
<div style="background-color: #1B1E23; padding: 15px; border-radius: 5px;">
    <div class="obs-text">
        <strong style="color: #8E949E;">PLANETARY KP INDEX:</strong>
        <span style="color: #E58E26; font-size: 1.1rem; font-weight: bold;">{space_data['kp']}</span>
        &nbsp;&nbsp;&nbsp;|&nbsp;&nbsp;&nbsp;
        <strong style="color: #8E949E;">GNSS RISK:</strong>
        <span style="color: {risk_color}; font-weight: bold; font-size: 1.1rem;">{space_data['risk']}</span>
        <br><br>
        <strong style="color: #8E949E;">OPERATIONAL IMPACT:</strong><br>
        {space_data['impact']}
    </div>
</div>
""", unsafe_allow_html=True)

st.divider()

if icao == "NONE":
    st.subheader("Station Actuals")
    st.markdown('<div class="obs-text">No METAR/TAF information within a 50km radius.</div>', unsafe_allow_html=True)
    clean_metar = "NIL"
    clean_taf   = "NIL"
else:
    clean_metar = re.sub('<[^<]+>', '', metar_raw)

    raw_taf_no_html = re.sub('<[^<]+>', '', taf_raw)
    raw_taf_no_html = raw_taf_no_html.replace(" RMK ", "\nRMK ")
    taf_lines = [line.strip() for line in raw_taf_no_html.split('\n') if line.strip()]

    ui_taf_lines  = []
    csv_taf_lines = []

    # FIX: renamed loop variable from i → taf_line_i to prevent shadowing forecast_idx
    for taf_line_i, line in enumerate(taf_lines):
        if taf_line_i == 0 or taf_line_i == len(taf_lines) - 1 or line.startswith("FM") or line.startswith("RMK"):
            ui_taf_lines.append(line)
            csv_taf_lines.append(line)
        else:
            ui_taf_lines.append("&nbsp;&nbsp;&nbsp;&nbsp;" + line)
            csv_taf_lines.append("    " + line)

    ui_rebuilt_taf = '\n'.join(ui_taf_lines)
    clean_taf      = '\n'.join(csv_taf_lines)

    metar_disp = apply_tactical_highlights(clean_metar)
    taf_disp   = apply_tactical_highlights(ui_rebuilt_taf)
    taf_disp   = taf_disp.replace('\n', '<br>')

    st.subheader(f"Station Actuals: {icao} | {stn_dist:.1f} km {stn_dir} of AO")
    st.markdown(f'''
    <div style="background-color: #1B1E23; padding: 15px; border-radius: 5px;">
        <div class="obs-text">
            <strong style="color: #8E949E;">METAR/SPECI</strong><br>
            <div style="line-height: 1.3; margin-bottom: 15px; margin-top: 5px;">
                {metar_disp}
            </div>
            <strong style="color: #8E949E;">TAF</strong><br>
            <div style="line-height: 1.3; font-size: 0.95rem; margin-top: 5px;">
                {taf_disp}
            </div>
        </div>
    </div>
    ''', unsafe_allow_html=True)

st.divider()


# =============================================================================
# ENTERPRISE PDF EXPORT ENGINE
# FIX: Previously, generate_pdf_report() was called directly inside
# st.download_button(data=...), causing a full PDF build on EVERY Streamlit
# rerender. The PDF is now cached in session_state and only regenerated when
# the forecast index, coordinates, model, or ICAO actually change.
# =============================================================================

def generate_pdf_report() -> bytes:
    stn_display_str = f"{icao} | {stn_dist:.1f} km {stn_dir} of AO" if icao != "NONE" else "No valid ICAO within 50km."

    pdf = FPDF()
    pdf.add_page()

    def safe_txt(txt: str) -> str:
        return str(txt).replace('°', ' deg').encode('latin-1', 'replace').decode('latin-1')

    pdf.set_font("helvetica", "B", 14)
    pdf.cell(0, 8, "VECTOR CHECK AERIAL GROUP INC.", border=0, new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("helvetica", "I", 10)
    pdf.cell(0, 6, "Atmospheric Risk Assessment (Operational Flight Briefing)", border=0, new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(5)

    pdf.set_font("helvetica", "B", 10)
    pdf.cell(40, 6, "Target Coordinates:", border=0)
    pdf.set_font("helvetica", "", 10)
    pdf.cell(0, 6, safe_txt(f"{lat}, {lon}"), border=0, new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("helvetica", "B", 10)
    pdf.cell(40, 6, "Regional Area:", border=0)
    pdf.set_font("helvetica", "", 10)
    pdf.cell(0, 6, safe_txt(regional_name), border=0, new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("helvetica", "B", 10)
    pdf.cell(40, 6, "Reference Station:", border=0)
    pdf.set_font("helvetica", "", 10)
    pdf.cell(0, 6, safe_txt(stn_display_str), border=0, new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("helvetica", "B", 10)
    pdf.cell(40, 6, "Model / Valid Time:", border=0)
    pdf.set_font("helvetica", "", 10)
    _pdf_run_label = f" ({_run_info['run_cycle_z']} run)" if _run_info.get("run_cycle_z") else ""
    pdf.cell(0, 6, safe_txt(f"{model_choice}{_pdf_run_label} | {selected_time_str}"), border=0, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    pdf.set_font("helvetica", "B", 12)
    pdf.cell(0, 8, "FORECASTED SURFACE CONDITIONS", border=0, new_x="LMARGIN", new_y="NEXT")
    _pdf_gust_clause = f" (Gusts: {int(gst)} {raw_wind_unit})" if gst > w_spd else ""
    pdf.set_font("helvetica", "", 10)
    pdf.multi_cell(0, 6, safe_txt(
        f"Temperature: {t_temp}C | RH: {rh}% | Dewpoint: {td:.1f}C\n"
        f"Wind: {sfc_dir_disp} @ {sfc_spd_disp} {raw_wind_unit}{_pdf_gust_clause}\n"
        f"Weather: {weather_str} | Visibility: {vis_disp}\n"
        f"Cloud Base: {c_base_disp} | Freezing Level: {frz_disp}"
    ))
    pdf.ln(5)

    pdf.set_font("helvetica", "B", 12)
    pdf.cell(0, 8, "ASTRONOMICAL & SPACE WEATHER", border=0, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 10)
    pdf.multi_cell(0, 6, safe_txt(
        f"Sun ({astro['tz']}): Rise {astro['sunrise']} | Set {astro['sunset']} | Civil Dawn {astro['dawn']} | Civil Dusk {astro['dusk']}\n"
        f"Moon ({astro['tz']}): Rise {astro['moonrise']} | Set {astro['moonset']} | Illum {astro['moon_ill']}%\n"
        f"Space Weather: Kp Index {space_data['kp']} | GNSS Risk: {space_data['risk']}"
    ))
    pdf.ln(5)

    if icao != "NONE":
        pdf.set_font("helvetica", "B", 12)
        pdf.cell(0, 8, f"STATION ACTUALS ({icao})", border=0, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("helvetica", "B", 10)
        pdf.cell(0, 6, "METAR:", border=0, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("helvetica", "", 9)
        pdf.multi_cell(0, 5, safe_txt(clean_metar))
        pdf.ln(2)
        pdf.set_font("helvetica", "B", 10)
        pdf.cell(0, 6, "TAF:", border=0, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("helvetica", "", 9)
        pdf.multi_cell(0, 5, safe_txt(clean_taf))
        pdf.ln(5)

    def draw_table(title: str, df: pd.DataFrame) -> None:
        pdf.set_font("helvetica", "B", 12)
        pdf.cell(0, 8, title, border=0, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("helvetica", "B", 9)
        col_names = ["Alt (AGL)"] + list(df.columns)

        # Dynamic column widths: the index column ("Alt (AGL)") gets a fixed
        # width; the remaining data columns split the available page width
        # evenly. This adapts automatically as columns are added/removed
        # (e.g. the new Temp column) so the table can never run off the end
        # of col_widths the way a hardcoded list did.
        n_cols      = len(col_names)
        first_width = 25
        page_width  = pdf.w - 2 * pdf.l_margin          # usable width
        rest_width  = max(18, (page_width - first_width) / max(1, n_cols - 1))
        col_widths  = [first_width] + [rest_width] * (n_cols - 1)

        for col_i, col in enumerate(col_names):
            pdf.cell(col_widths[col_i], 8, safe_txt(str(col)), border=1, align='C')
        pdf.ln(8)
        pdf.set_font("helvetica", "", 9)
        for row_label, row in df.iterrows():
            pdf.cell(col_widths[0], 8, safe_txt(str(row_label)), border=1, align='C')
            for val_i, val in enumerate(row):
                pdf.cell(col_widths[val_i + 1], 8, safe_txt(str(val)), border=1, align='C')
            pdf.ln(8)
        pdf.ln(5)

    draw_table("TACTICAL HAZARD STACK (0-400ft AGL)",      df_tactical)
    draw_table("EXTENDED TRAJECTORY (1,000-5,000ft AGL)",  df_ext)

    pdf.set_font("helvetica", "B", 12)
    pdf.cell(0, 8, "THERMODYNAMIC & AERODYNAMIC PROFILE", border=0, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 10)
    pdf.multi_cell(0, 6, safe_txt(
        f"Precipitation Risk: {pop}% ({precip} mm)\n"
        f"Density Altitude (DA): {density_alt:,} ft\n"
        f"Convective Available Potential Energy (CAPE): {cape} J/kg"
    ))
    pdf.ln(5)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp_name = tmp.name

    try:
        pdf.output(tmp_name)
        with open(tmp_name, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


# Cache the PDF in session_state — only rebuild when the briefing state changes
_pdf_cache_key = f"{lat}_{lon}_{forecast_idx}_{model_choice}_{icao}"
if st.session_state.get("_pdf_cache_key") != _pdf_cache_key:
    st.session_state["_pdf_bytes"]     = generate_pdf_report()
    st.session_state["_pdf_cache_key"] = _pdf_cache_key


def log_download_callback() -> None:
    try: log_action(st.session_state.get("active_operator", "UNKNOWN"), lat, lon, icao, "DOWNLOAD_PDF")
    except Exception: pass


st.download_button(
    label     = "Download Flight Briefing (PDF)",
    data      = st.session_state["_pdf_bytes"],
    file_name = f"VCAG_Briefing_{lat}_{lon}_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
    mime      = "application/pdf",
    on_click  = log_download_callback,
)

st.divider()

# =============================================================================
# CLIMATE CONTEXT PANEL
# 30-year ERA5 reanalysis normals with percentile positioning and wind
# direction frequency bars. Cached in Supabase after first computation.
# =============================================================================

st.subheader("Climatology")

_climate_month = datetime.fromisoformat(h["time"][forecast_idx]).month
climate = fetch_climate_context_cached(lat, lon, _climate_month)

if climate["error"]:
    st.warning(f"Climate data unavailable: {climate['error']}")
else:
    _month_names = ["", "January", "February", "March", "April", "May", "June",
                    "July", "August", "September", "October", "November", "December"]
    _month_name = _month_names[climate["month"]]

    st.caption(f"25-year hourly normals ({climate['years_range']}) \u00b7 {regional_name} \u00b7 {_month_name}")

    # Source badge — green for ECCC station observations, blue for ERA5
    # reanalysis (preferred reanalysis source), amber for NASA POWER (fallback)
    _src = climate.get("source", "")
    _src_label = climate.get("source_label", "Unknown source")
    if _src == "ECCC":
        _src_color = "#2abf2a"
        _src_bg = "rgba(42, 191, 42, 0.12)"
        _src_desc = "station observations"
    elif _src == "ERA5_MM":
        _src_color = "#3b82f6"
        _src_bg = "rgba(59, 130, 246, 0.12)"
        _src_desc = "ECMWF reanalysis \u00b7 downscaled"
    elif _src == "ERA5":
        _src_color = "#3b82f6"
        _src_bg = "rgba(59, 130, 246, 0.12)"
        _src_desc = "ECMWF reanalysis"
    elif _src == "NASA_POWER":
        _src_color = "#E58E26"
        _src_bg = "rgba(229, 142, 38, 0.12)"
        _src_desc = "gridded reanalysis"
    else:
        _src_color = "#8E949E"
        _src_bg = "rgba(142, 148, 158, 0.12)"
        _src_desc = ""

    # Source badge — minimal: thin border + dot, no fill
    _src = climate.get("source", "")
    _src_label = climate.get("source_label", "Unknown source")
    if _src == "ECCC":
        _src_dot = "#4ade80"
        _src_desc = "station observations"
    elif _src == "ERA5_MM":
        _src_dot = "#3b82f6"
        _src_desc = "ECMWF reanalysis \u00b7 downscaled"
    elif _src == "ERA5":
        _src_dot = "#3b82f6"
        _src_desc = "ECMWF reanalysis"
    elif _src == "NASA_POWER":
        _src_dot = "#94a3b8"
        _src_desc = "gridded reanalysis"
    else:
        _src_dot = "#64748b"
        _src_desc = ""

    _badge_html = (
        f'<div style="display:inline-flex;align-items:center;gap:8px;'
        f'border:1px solid #2A2F38;border-radius:4px;padding:4px 10px;margin-bottom:14px;">'
        f'<span style="width:6px;height:6px;border-radius:50%;background:{_src_dot};display:inline-block;"></span>'
        f'<span style="font-size:0.7rem;color:#A0A4AB;font-weight:400;letter-spacing:0.2px;">{_src_label}</span>'
        f'<span style="font-size:0.65rem;color:#5C6370;">\u00b7 {_src_desc}</span>'
        f'</div>'
    )
    st.markdown(_badge_html, unsafe_allow_html=True)

    # Build synthetic density altitude distribution from cached temp + pressure anchors
    _da_dist = _build_da_distribution(climate, sfc_elevation)

    # Color helper: muted gray for normal, amber for elevated, red for anomalous
    def _accent(p):
        if p >= 90 or p <= 10:
            return "#ff6b4a"   # red — anomalous
        elif p >= 75 or p <= 25:
            return "#E58E26"   # amber — elevated
        else:
            return "#A0A4AB"   # neutral gray — normal

    clim_left, clim_right = st.columns([3, 2])

    with clim_left:
        # --- Metric Cards: forecast value + 25-year average + small percentile chip ---
        cc1, cc2, cc3, cc4 = st.columns(4)

        def _card(label, value_html, avg_html, pct):
            chip_color = _accent(pct)
            return (
                f'<div style="background:#161A1F;border:1px solid #2A2F38;padding:11px 13px;border-radius:6px;">'
                f'<div style="font-size:0.65rem;color:#6B7280;text-transform:uppercase;letter-spacing:0.6px;font-weight:500;">{label}</div>'
                f'<div style="display:flex;align-items:baseline;justify-content:space-between;gap:6px;margin-top:5px;">'
                f'<span style="font-size:1.3rem;font-weight:600;color:#E58E26;font-variant-numeric:tabular-nums;">{value_html}</span>'
                f'<span style="font-size:0.65rem;color:{chip_color};font-weight:600;">P{pct}</span>'
                f'</div>'
                f'<div style="font-size:0.65rem;color:#6B7280;margin-top:3px;">avg <span style="color:#9CA3AF;font-variant-numeric:tabular-nums;">{avg_html}</span></div>'
                f'</div>'
            )

        _w_pct = _pct_rank(w_spd, climate["wind"])
        cc1.markdown(_card("Wind", f"{int(w_spd)} kt",
                           f'{climate["wind"]["p50"]:.0f} kt', _w_pct),
                     unsafe_allow_html=True)

        _t_pct = _pct_rank(t_temp, climate["temp"])
        cc2.markdown(_card("Temperature", f"{t_temp}\u00b0C",
                           f'{climate["temp"]["p50"]:.1f}\u00b0C', _t_pct),
                     unsafe_allow_html=True)

        _p_pct = _pct_rank(sfc_press, climate["pressure"])
        cc3.markdown(_card("Pressure", f"{sfc_press:.0f} hPa",
                           f'{climate["pressure"]["p50"]:.0f} hPa', _p_pct),
                     unsafe_allow_html=True)

        _da_pct = _pct_rank(density_alt, _da_dist)
        cc4.markdown(_card("Density Alt", f"{density_alt:,} ft",
                           f'{_da_dist["p50"]:,} ft', _da_pct),
                     unsafe_allow_html=True)

        # --- Percentile Bars: visible track, bolder marker ---
        st.markdown("<div style='margin-top:18px;'></div>", unsafe_allow_html=True)
        st.markdown(
            "<div style='font-size:0.7rem;color:#9CA3AF;text-transform:uppercase;letter-spacing:0.6px;margin-bottom:10px;font-weight:500;'>"
            "Position vs 25-Year Normals</div>",
            unsafe_allow_html=True,
        )

        _rh_pct = _pct_rank(rh, climate["rh"])

        _bar_data = [
            ("Wind",     _w_pct),
            ("Temp",     _t_pct),
            ("Pressure", _p_pct),
            ("RH",       _rh_pct),
            ("DA",       _da_pct),
        ]

        for _bar_label, _bar_pct in _bar_data:
            _marker_clr = _accent(_bar_pct)
            # When normal, use a brighter neutral so the marker is still visible
            if _marker_clr == "#A0A4AB":
                _marker_clr = "#CBD5E1"
            _pct_clr = _accent(_bar_pct)
            if _pct_clr == "#A0A4AB":
                _pct_clr = "#9CA3AF"
            _bar_html = (
                f'<div style="display:flex;align-items:center;gap:10px;margin:6px 0;">'
                f'<span style="font-size:0.72rem;color:#9CA3AF;min-width:54px;text-align:right;">{_bar_label}</span>'
                f'<div style="flex:1;height:8px;background:#2A2F38;border-radius:4px;position:relative;">'
                # median tick — slightly brighter so it reads
                f'<div style="position:absolute;left:50%;top:-3px;width:1px;height:14px;background:#3F4651;"></div>'
                # forecast marker — 3px wide, capped, full color
                f'<div style="position:absolute;left:{_bar_pct}%;top:-3px;width:3px;height:14px;background:{_marker_clr};border-radius:1.5px;transform:translateX(-50%);box-shadow:0 0 4px rgba(0,0,0,0.4);"></div>'
                f'</div>'
                f'<span style="font-size:0.72rem;color:{_pct_clr};min-width:34px;font-variant-numeric:tabular-nums;font-weight:500;">P{_bar_pct}</span>'
                f'</div>'
            )
            st.markdown(_bar_html, unsafe_allow_html=True)

        st.markdown(
            '<div style="display:flex;gap:14px;margin-top:12px;font-size:0.62rem;color:#6B7280;letter-spacing:0.3px;">'
            '<span>P25\u2013P75 normal</span>'
            '<span style="color:#E58E26;">\u00b7 elevated</span>'
            '<span style="color:#ff6b4a;">\u00b7 anomalous</span>'
            '</div>',
            unsafe_allow_html=True,
        )

    with clim_right:
        st.markdown(
            f"<div style='font-size:0.7rem;color:#9CA3AF;text-transform:uppercase;letter-spacing:0.6px;margin-bottom:2px;font-weight:500;'>"
            f"Wind Direction \u2014 {_month_name}</div>",
            unsafe_allow_html=True,
        )
        st.markdown(
            "<div style='font-size:0.65rem;color:#6B7280;margin-bottom:10px;'>Frequency over 25 years</div>",
            unsafe_allow_html=True,
        )

        _cur_dir_name = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int(round(sfc_dir / 45.0)) % 8] if w_spd > 0 else ""

        _max_pct = max((wr["total"] for wr in climate["wind_rose"]), default=20)
        _bar_scale = 88.0 / max(1, _max_pct)

        for _wr in climate["wind_rose"]:
            _is_current = (_wr["dir"] == _cur_dir_name) and w_spd > 0

            _strong_w = _wr["strong"] * _bar_scale
            _mod_strong_w = (_wr["mod"] + _wr["strong"]) * _bar_scale
            _total_w = _wr["total"] * _bar_scale

            # Three distinct shades — calm gray, mid steel-blue, strong amber
            # Strong-wind segment (20+kt) gets the accent because it's operationally relevant
            _label_clr = "#F1F5F9" if _is_current else "#9CA3AF"
            _label_weight = "600" if _is_current else "500"
            _row_bg = "background:#1C2128;" if _is_current else ""

            _row_html = (
                f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;padding:3px 5px;border-radius:3px;{_row_bg}">'
                f'<span style="font-size:0.72rem;font-weight:{_label_weight};color:{_label_clr};width:24px;text-align:right;font-variant-numeric:tabular-nums;">{_wr["dir"]}</span>'
                f'<div style="flex:1;height:10px;background:#2A2F38;border-radius:2px;position:relative;overflow:hidden;">'
                # Total bar — calm winds, neutral gray-blue
                f'<div style="position:absolute;left:0;top:0;width:{_total_w}%;height:100%;background:#5B6573;"></div>'
                # Mod+Strong overlay — lighter gray
                f'<div style="position:absolute;left:0;top:0;width:{_mod_strong_w}%;height:100%;background:#94A3B8;"></div>'
                # Strong overlay — amber to flag operationally relevant winds
                f'<div style="position:absolute;left:0;top:0;width:{_strong_w}%;height:100%;background:#E58E26;"></div>'
                f'</div>'
                f'<span style="font-size:0.68rem;color:#9CA3AF;min-width:28px;text-align:right;font-variant-numeric:tabular-nums;">{_wr["total"]:.0f}%</span>'
                f'</div>'
            )
            st.markdown(_row_html, unsafe_allow_html=True)

        # Three-shade legend
        st.markdown(
            '<div style="display:flex;gap:14px;margin-top:10px;font-size:0.62rem;color:#9CA3AF;align-items:center;">'
            '<span style="display:inline-flex;align-items:center;gap:5px;">'
            '<span style="width:12px;height:7px;background:#5B6573;border-radius:1px;"></span>0\u201310 kt</span>'
            '<span style="display:inline-flex;align-items:center;gap:5px;">'
            '<span style="width:12px;height:7px;background:#94A3B8;border-radius:1px;"></span>10\u201320 kt</span>'
            '<span style="display:inline-flex;align-items:center;gap:5px;">'
            '<span style="width:12px;height:7px;background:#E58E26;border-radius:1px;"></span>20+ kt</span>'
            '</div>',
            unsafe_allow_html=True,
        )

        # --- Bottom stat row: prevailing, avg, now ---
        _delta = int(w_spd - climate["wind"]["p50"])
        _delta_str = f"+{_delta}" if _delta >= 0 else str(_delta)
        _delta_clr = "#ff6b4a" if abs(_delta) >= 8 else "#E58E26" if abs(_delta) >= 4 else "#9CA3AF"

        _stats_html = (
            '<div style="display:flex;gap:1px;margin-top:14px;background:#2A2F38;border-radius:6px;overflow:hidden;border:1px solid #2A2F38;">'

            '<div style="flex:1;background:#161A1F;padding:9px 10px;text-align:center;">'
            '<div style="font-size:0.58rem;color:#6B7280;text-transform:uppercase;letter-spacing:0.6px;font-weight:500;">Prevailing</div>'
            f'<div style="font-size:0.9rem;font-weight:600;color:#F1F5F9;margin-top:4px;font-variant-numeric:tabular-nums;">{climate["prevailing_dir"]}</div>'
            f'<div style="font-size:0.58rem;color:#6B7280;margin-top:2px;">{climate["prevailing_pct"]:.0f}% of hours</div>'
            '</div>'

            '<div style="flex:1;background:#161A1F;padding:9px 10px;text-align:center;">'
            '<div style="font-size:0.58rem;color:#6B7280;text-transform:uppercase;letter-spacing:0.6px;font-weight:500;">25-yr Avg</div>'
            f'<div style="font-size:0.9rem;font-weight:600;color:#F1F5F9;margin-top:4px;font-variant-numeric:tabular-nums;">{climate["wind"]["p50"]:.0f} kt</div>'
            f'<div style="font-size:0.58rem;color:#6B7280;margin-top:2px;">{_month_name}</div>'
            '</div>'

            '<div style="flex:1;background:#161A1F;padding:9px 10px;text-align:center;">'
            '<div style="font-size:0.58rem;color:#6B7280;text-transform:uppercase;letter-spacing:0.6px;font-weight:500;">Now</div>'
            f'<div style="font-size:0.9rem;font-weight:600;color:#E58E26;margin-top:4px;font-variant-numeric:tabular-nums;">{int(sfc_dir)}\u00b0 / {int(w_spd)} kt</div>'
            f'<div style="font-size:0.58rem;color:{_delta_clr};margin-top:2px;font-variant-numeric:tabular-nums;font-weight:500;">{_delta_str} kt vs normal</div>'
            '</div>'

            '</div>'
        )
        st.markdown(_stats_html, unsafe_allow_html=True)

st.divider()


# =============================================================================
# MODEL ANALYSIS — Multi-Model Ensemble Consensus
# =============================================================================

st.subheader("Model Analysis")

@st.cache_data(ttl=3600, show_spinner="Fetching multi-model ensemble...")
def _fetch_ensemble_cached(e_lat: float, e_lon: float, e_tz: str = None) -> dict:
    """Fetches all NWP models and computes ensemble analysis + comparison
    matrix. Cached 1 hour."""
    models = fetch_all_models(e_lat, e_lon)
    if not models:
        return {"error": "No models returned data."}

    blocks = compute_ensemble_blocks(models)
    risks = identify_risk_windows(blocks)

    # Build the side-by-side comparison matrix (24 hourly columns) and its
    # terse agreement/divergence callouts. tz_str enables local-time labels.
    matrix = build_model_matrix(models, n_hours=48, start_offset=0, tz_str=e_tz)
    matrix_notes = summarize_matrix(matrix)

    return {
        "model_count": len(models),
        "models_used": [m.name for m in models],
        "matrix": matrix,
        "matrix_notes": matrix_notes,
        "blocks": [
            {
                "block_label": b.block_label, "start_hour": b.start_hour,
                "wind_mean": b.wind_mean, "wind_min": b.wind_min,
                "wind_max": b.wind_max, "wind_spread": b.wind_spread,
                "wind_dir_mean": b.wind_dir_mean, "wind_dir_spread": b.wind_dir_spread,
                "gust_mean": b.gust_mean, "gust_max": b.gust_max, "gust_spread": b.gust_spread,
                "temp_mean": b.temp_mean, "temp_min": b.temp_min,
                "temp_max": b.temp_max, "temp_spread": b.temp_spread,
                "pressure_mean": b.pressure_mean, "pressure_spread": b.pressure_spread,
                "precip_prob_max": b.precip_prob_max,
                "confidence": b.confidence, "model_count": b.model_count,
            }
            for b in blocks
        ],
        "risks": [
            {"label": r.start_label, "var": r.variable, "spread": r.spread,
             "detail": r.detail, "severity": r.severity}
            for r in risks
        ],
        "consensus": "",
        "wind_summary": "",
        "precip_summary": "",
        "confidence_summary": "",
        "anomaly_flags": [],
        "overall_confidence": "HIGH",
    }

_ens = _fetch_ensemble_cached(lat, lon, tz_str)

if _ens.get("error"):
    st.warning(f"Model analysis unavailable: {_ens['error']}")
else:
    # Generate the briefing text using climate context if available
    from modules.ensemble_analysis import BlockStats, RiskWindow, ModelForecast, EnsembleBriefing
    _ens_blocks = [BlockStats(**{k: v for k, v in b.items()}) for b in _ens["blocks"]]
    _ens_risks = [RiskWindow(start_label=r["label"], variable=r["var"], spread=r["spread"],
                              detail=r["detail"], severity=r["severity"]) for r in _ens["risks"]]
    _ens_models = [ModelForecast(name=n, valid=True) for n in _ens["models_used"]]

    _climate_for_ens = climate if not climate.get("error") else None
    _ens_brief = generate_briefing(_ens_models, _ens_blocks, _ens_risks, _climate_for_ens)

    # =========================================================================
    # SECTION 1 — MODEL ANALYSIS (Forecast View)
    # What the models are predicting; where they agree and disagree.
    # =========================================================================

    _conf_colors = {"HIGH": "#4ade80", "MODERATE": "#E58E26", "LOW": "#ff6b4a"}
    _conf_clr = _conf_colors.get(_ens_brief.overall_confidence, "#9CA3AF")

    # Compute the expected ensemble size: 4 globally, 6 in CONUS (HRRR + NAM added).
    # The lat/lon check mirrors _is_conus_coverage() in ensemble_analysis.
    _expected_ensemble_size = 6 if (21.0 <= lat <= 50.0 and -134.0 <= lon <= -60.0) else 4

    # Header strip: model count and overall confidence badge — full width
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;">'
        f'<span style="font-size:0.82rem;color:#9CA3AF;">'
        f'{_ens_brief.model_count}/{_expected_ensemble_size} models reporting: '
        f'<span style="color:#D1D5DB;font-weight:500;">{", ".join(_ens_brief.models_used)}</span></span>'
        f'<span style="font-size:0.72rem;color:{_conf_clr};font-weight:600;'
        f'border:1px solid {_conf_clr};border-radius:3px;padding:3px 10px;">'
        f'{_ens_brief.overall_confidence} CONFIDENCE</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # =========================================================================
    # SIDE-BY-SIDE MODEL COMPARISON MATRIX
    # One table per variable. Hours run left→right as columns; each model is a
    # row. Wind shows a direction arrow + speed; temp/RH/vis show the value
    # with deviation-based coloring. Brief callouts sit above the tables.
    # =========================================================================
    _mtx = _ens.get("matrix") or {}
    _mtx_models = _mtx.get("models") or []
    _hour_labels = _mtx.get("hour_labels") or []
    _consensus = _mtx.get("consensus") or {}

    if not _mtx_models or not _hour_labels:
        st.info("Comparison matrix unavailable — insufficient aligned model data.")
    else:
        # ---- Terse callouts (a couple points only, per design) ----
        _notes = _ens.get("matrix_notes") or []
        if _notes:
            _note_html = ""
            for _sev, _txt in _notes:
                _n_clr = "#ff6b4a" if _sev == "alert" else "#9CA3AF"
                _n_icon = "\u26a0" if _sev == "alert" else "\u25cf"
                _note_html += (
                    f'<div style="font-size:0.78rem;color:{_n_clr};margin:3px 0;line-height:1.4;">'
                    f'<span style="font-size:0.6rem;margin-right:7px;vertical-align:middle;">{_n_icon}</span>'
                    f'{_txt}</div>'
                )
            st.markdown(
                f'<div style="margin-bottom:16px;padding:10px 14px;background:#13171C;'
                f'border-radius:6px;">{_note_html}</div>',
                unsafe_allow_html=True,
            )

        # Show the full 48-hour horizon. Tables scroll horizontally rather
        # than truncating, so no operator interaction is needed to see the
        # whole forecast window.
        _ncol = min(48, len(_hour_labels))

        # Shared column template: model-name column + N hour columns. Columns
        # use a FIXED width (not fractional) so the table extends past the
        # container and the wrapper scrolls horizontally. The model-name column
        # is sticky (see _scroll_open) so it stays visible while scrolling.
        _name_w = 78
        _col_w = 38   # px per hour column — fixed so 48h overflows and scrolls
        _grid = f"{_name_w}px repeat({_ncol}, {_col_w}px)"

        # Horizontal-scroll wrapper. The model-name column is pinned via
        # position:sticky so it doesn't scroll away with the hour columns.
        def _scroll_open():
            return ('<div style="overflow-x:auto;overflow-y:hidden;'
                    'padding-bottom:6px;">')
        def _scroll_close():
            return '</div>'

        def _wind_arrow(deg):
            """Unicode arrow pointing in the direction the wind is blowing TO.
            Meteorological convention: wind_dir is the direction FROM, so the
            arrow points toward (deg + 180)."""
            if deg is None:
                return "\u00b7"
            # 8-point compass arrows. Arrow shows flow direction (FROM+180).
            arrows = ["\u2193", "\u2199", "\u2190", "\u2196",
                      "\u2191", "\u2197", "\u2192", "\u2198"]
            # deg is FROM direction; index by (deg) since ↓ at 0° = wind from N blowing S
            idx = int(((deg % 360) + 22.5) // 45) % 8
            return arrows[idx]

        _local_labels = _mtx.get("local_labels") or []
        _local_abbr = _mtx.get("local_tz_abbr") or ""
        _has_local = len(_local_labels) >= _ncol

        def _hdr_row(title):
            # Header label cell. When local time is available, show two lines:
            # local hour on top, Zulu beneath, so operators can read either.
            if _has_local:
                _title_sub = f'{_local_abbr}/Z' if _local_abbr else 'Local/Z'
                cells = (f'<div style="font-size:0.62rem;color:#9CA3AF;font-weight:600;'
                         f'text-transform:uppercase;letter-spacing:0.3px;padding:4px 4px;'
                         f'line-height:1.25;position:sticky;left:0;background:#0E1117;'
                         f'z-index:2;">{title}'
                         f'<div style="font-size:0.5rem;color:#4B5563;font-weight:400;">'
                         f'{_title_sub}</div></div>')
                for _ci in range(_ncol):
                    _loc = _local_labels[_ci]
                    _zul = _hour_labels[_ci].replace("Z", "")
                    cells += (f'<div style="font-size:0.58rem;color:#6B7280;text-align:center;'
                              f'padding:4px 2px;font-variant-numeric:tabular-nums;line-height:1.25;">'
                              f'<span style="color:#9CA3AF;">{_loc}</span>'
                              f'<div style="font-size:0.5rem;color:#4B5563;">{_zul}z</div></div>')
            else:
                cells = (f'<div style="font-size:0.62rem;color:#9CA3AF;font-weight:600;'
                         f'text-transform:uppercase;letter-spacing:0.3px;padding:4px 4px;'
                         f'position:sticky;left:0;background:#0E1117;z-index:2;">{title}</div>')
                for hl in _hour_labels[:_ncol]:
                    cells += (f'<div style="font-size:0.58rem;color:#6B7280;text-align:center;'
                              f'padding:4px 2px;font-variant-numeric:tabular-nums;">{hl}</div>')
            return (f'<div style="display:grid;grid-template-columns:{_grid};gap:1px;'
                    f'margin-bottom:1px;min-width:max-content;">{cells}</div>')

        def _model_name_cell(name):
            return (f'<div style="font-size:0.72rem;color:#D1D5DB;padding:4px 4px;'
                    f'background:#161A1F;white-space:nowrap;overflow:hidden;'
                    f'text-overflow:ellipsis;position:sticky;left:0;z-index:1;">{name}</div>')

        def _row_open():
            return (f'<div style="display:grid;grid-template-columns:{_grid};gap:1px;'
                    f'margin-bottom:1px;min-width:max-content;">')

        # ============ WIND TABLE (arrow + speed, gust on hover via title) ====
        st.markdown(
            '<div style="font-size:0.74rem;color:#6B7280;text-transform:uppercase;'
            'letter-spacing:0.5px;margin:6px 0 6px;font-weight:500;">Wind '
            '<span style="color:#4B5563;text-transform:none;letter-spacing:0;">'
            '(arrow = flow direction, number = kt)</span></div>',
            unsafe_allow_html=True,
        )
        _wind_html = _hdr_row("Model")
        for _mm in _mtx_models:
            _row = _model_name_cell(_mm["name"])
            for _hi in range(_ncol):
                _w = _mm["wind_kt"][_hi]
                _d = _mm["wind_dir"][_hi]
                _g = _mm["gust_kt"][_hi]
                _spread = _consensus.get("wind_spread", [0] * _ncol)
                _sp = _spread[_hi] if _hi < len(_spread) else 0
                # Color the cell background subtly by cross-model spread at this
                # hour: green calm agreement, amber moderate, red high.
                if _sp >= 10:    _bg = "rgba(255,107,74,0.14)"
                elif _sp >= 6:   _bg = "rgba(229,142,38,0.12)"
                else:            _bg = "#161A1F"
                if _w is None:
                    _cell = (f'<div style="background:{_bg};padding:4px 2px;text-align:center;'
                             f'font-size:0.7rem;color:#4B5563;">\u00b7</div>')
                else:
                    _arr = _wind_arrow(_d)
                    _ttl = f"{_w:.0f} kt" + (f" G{_g:.0f}" if (_g is not None and _g >= _w + 3) else "")
                    _w_clr = "#ff6b4a" if _w >= 25 else "#E58E26" if _w >= 15 else "#D1D5DB"
                    _cell = (f'<div title="{_ttl}" style="background:{_bg};padding:4px 2px;'
                             f'text-align:center;font-variant-numeric:tabular-nums;">'
                             f'<span style="font-size:0.78rem;color:#7DA3C9;">{_arr}</span>'
                             f'<span style="font-size:0.72rem;color:{_w_clr};margin-left:1px;">{_w:.0f}</span>'
                             f'</div>')
                _row += _cell
            _wind_html += f'{_row_open()}{_row}</div>'
        st.markdown(_scroll_open() + _wind_html + _scroll_close(), unsafe_allow_html=True)

        # ============ TEMPERATURE TABLE =====================================
        st.markdown(
            '<div style="font-size:0.74rem;color:#6B7280;text-transform:uppercase;'
            'letter-spacing:0.5px;margin:16px 0 6px;font-weight:500;">Temperature '
            '<span style="color:#4B5563;text-transform:none;letter-spacing:0;">'
            '(\u00b0C)</span></div>',
            unsafe_allow_html=True,
        )
        _temp_html = _hdr_row("Model")
        for _mm in _mtx_models:
            _row = _model_name_cell(_mm["name"])
            for _hi in range(_ncol):
                _t = _mm["temp_c"][_hi]
                _tspread = _consensus.get("temp_spread", [0] * _ncol)
                _tsp = _tspread[_hi] if _hi < len(_tspread) else 0
                if _tsp >= 5:    _bg = "rgba(229,142,38,0.12)"
                else:            _bg = "#161A1F"
                if _t is None:
                    _cell = (f'<div style="background:{_bg};padding:4px 2px;text-align:center;'
                             f'font-size:0.7rem;color:#4B5563;">\u00b7</div>')
                else:
                    # Color by absolute temperature (cold blue → hot red)
                    if _t <= 0:     _t_clr = "#7DA3C9"
                    elif _t <= 10:  _t_clr = "#9CB8D4"
                    elif _t <= 20:  _t_clr = "#D1D5DB"
                    elif _t <= 28:  _t_clr = "#E58E26"
                    else:           _t_clr = "#ff6b4a"
                    _cell = (f'<div style="background:{_bg};padding:4px 2px;text-align:center;'
                             f'font-size:0.72rem;color:{_t_clr};font-variant-numeric:tabular-nums;">'
                             f'{_t:.0f}</div>')
                _row += _cell
            _temp_html += f'{_row_open()}{_row}</div>'
        st.markdown(_scroll_open() + _temp_html + _scroll_close(), unsafe_allow_html=True)

        # ============ RELATIVE HUMIDITY TABLE ===============================
        st.markdown(
            '<div style="font-size:0.74rem;color:#6B7280;text-transform:uppercase;'
            'letter-spacing:0.5px;margin:16px 0 6px;font-weight:500;">Relative Humidity '
            '<span style="color:#4B5563;text-transform:none;letter-spacing:0;">'
            '(%)</span></div>',
            unsafe_allow_html=True,
        )
        _rh_html = _hdr_row("Model")
        for _mm in _mtx_models:
            _row = _model_name_cell(_mm["name"])
            for _hi in range(_ncol):
                _r = _mm["rh"][_hi]
                if _r is None:
                    _cell = (f'<div style="background:#161A1F;padding:4px 2px;text-align:center;'
                             f'font-size:0.7rem;color:#4B5563;">\u00b7</div>')
                else:
                    # High RH (fog/precip risk) amber→red; dry neutral
                    if _r >= 95:    _r_clr = "#ff6b4a"
                    elif _r >= 85:  _r_clr = "#E58E26"
                    elif _r >= 60:  _r_clr = "#D1D5DB"
                    else:           _r_clr = "#9CA3AF"
                    _cell = (f'<div style="background:#161A1F;padding:4px 2px;text-align:center;'
                             f'font-size:0.72rem;color:{_r_clr};font-variant-numeric:tabular-nums;">'
                             f'{_r:.0f}</div>')
                _row += _cell
            _rh_html += f'{_row_open()}{_row}</div>'
        st.markdown(_scroll_open() + _rh_html + _scroll_close(), unsafe_allow_html=True)

        # ============ VISIBILITY TABLE ======================================
        # Visibility is a diagnostic field most NWP models don't output. On our
        # providers only a subset carry it: NAM/ICON/GFS via Open-Meteo's own
        # derivation, and MIX via Meteomatics. ECMWF-IFS, AIFS, HRDPS and the
        # raw Meteomatics models return no visibility. Rather than show a table
        # full of dashes, we render ONLY the models that actually have vis data
        # so the table stays dense and honest.
        _vis_models = [
            _mm for _mm in _mtx_models
            if any(v is not None for v in _mm["visibility_sm"][:_ncol])
        ]
        if _vis_models:
            _omitted = [_mm["name"] for _mm in _mtx_models if _mm not in _vis_models]
            _omit_note = ""
            if _omitted:
                _omit_note = (
                    f'<span style="color:#4B5563;text-transform:none;letter-spacing:0;'
                    f'font-weight:400;"> \u00b7 not output by {", ".join(_omitted)}</span>'
                )
            st.markdown(
                f'<div style="font-size:0.74rem;color:#6B7280;text-transform:uppercase;'
                f'letter-spacing:0.5px;margin:16px 0 6px;font-weight:500;">Visibility '
                f'<span style="color:#4B5563;text-transform:none;letter-spacing:0;">'
                f'(SM)</span>{_omit_note}</div>',
                unsafe_allow_html=True,
            )
            _vis_html = _hdr_row("Model")
            for _mm in _vis_models:
                _row = _model_name_cell(_mm["name"])
                for _hi in range(_ncol):
                    _v = _mm["visibility_sm"][_hi]
                    if _v is None:
                        _cell = (f'<div style="background:#161A1F;padding:4px 2px;text-align:center;'
                                 f'font-size:0.7rem;color:#4B5563;">\u00b7</div>')
                    else:
                        # Aviation-relevant thresholds: <1 red, <3 amber, else ok
                        if _v < 1.0:    _v_clr = "#ff6b4a"
                        elif _v < 3.0:  _v_clr = "#E58E26"
                        else:           _v_clr = "#9CA3AF"
                        _vtxt = f"{_v:.0f}" if _v >= 10 else f"{_v:.1f}"
                        _cell = (f'<div style="background:#161A1F;padding:4px 2px;text-align:center;'
                                 f'font-size:0.72rem;color:{_v_clr};font-variant-numeric:tabular-nums;">'
                                 f'{_vtxt}</div>')
                    _row += _cell
                _vis_html += f'{_row_open()}{_row}</div>'
            st.markdown(_scroll_open() + _vis_html + _scroll_close(), unsafe_allow_html=True)

        # Legend
        st.markdown(
            '<div style="font-size:0.64rem;color:#6B7280;margin-top:14px;line-height:1.5;">'
            '48-hour horizon \u2014 scroll horizontally to see later hours. Columns show '
            'local time over Zulu. Models are ordered finest resolution first. '
            'Highlighted cells flag cross-model disagreement at that hour '
            '(wind \u2265 6 kt amber / \u2265 10 kt red spread; temp \u2265 5\u00b0C amber). '
            'Hover a wind cell for exact speed and gust.'
            '</div>',
            unsafe_allow_html=True,
        )

    st.divider()

    # =========================================================================
    # SECTION 2 — MODEL PERFORMANCE (Retrospective)
    # How each model has scored against actual observations in the last 24h.
    # =========================================================================

    st.subheader("Model Performance")
    st.caption("Trailing 24-hour MAE of each model against the surface observation network.")

    @st.cache_data(ttl=1800, show_spinner="Scoring models against actuals…")
    def _fetch_scorecard_cached(sc_lat: float, sc_lon: float, sc_icao: str) -> dict:
        """Fetches trailing 24h performance scorecard. Cached 30 min."""
        sb = _safe_get_supabase()
        # Optional Synoptic API token (falls back to demotoken if absent).
        # Configure via SECRETS_TOML:  [synoptic] \n token = "..."
        try:
            synoptic_tok = st.secrets["synoptic"]["token"]
        except Exception:
            synoptic_tok = None
        return compute_performance_scorecard(
            sc_lat, sc_lon, sc_icao,
            sb_client=sb,
            synoptic_token=synoptic_tok,
            mesonet_radius_km=75.0,
        )

    _sc = _fetch_scorecard_cached(lat, lon, icao)

    if not _sc.get("has_data"):
        st.markdown(
            f'<div style="font-size:0.85rem;color:#9CA3AF;line-height:1.5;">'
            f'{_sc.get("message", "No surface observations available for scoring.")}</div>',
            unsafe_allow_html=True,
        )
    else:
        # --- Window + source summary block ---
        _ws = _sc.get("window_start_utc")
        _we = _sc.get("window_end_utc")
        _tf_str = "trailing 24 hours"
        if _ws and _we:
            try:
                _ws_local = _ws.astimezone(local_tz)
                _we_local = _we.astimezone(local_tz)
                _tz_abbr = _ws_local.strftime('%Z') or 'UTC'
                _tf_str = (
                    f"{_ws_local.strftime('%d %b %H:%M')} \u2192 "
                    f"{_we_local.strftime('%d %b %H:%M')} {_tz_abbr}"
                )
            except Exception:
                pass

        # Source summary — break out METAR / MADIS / Kestrel
        _metar_n = _sc.get("metar_count", 0)
        _metar_stns = _sc.get("metar_stations", []) or []
        _madis_total = _sc.get("mesonet_count", 0)
        _cwop_n = _sc.get("cwop_count", 0)
        _madis_stns = _sc.get("mesonet_stations", []) or []
        _kestrel_n = _sc.get("kestrel_count", 0)

        _src_chips = []
        if _metar_n > 0:
            _src_chips.append(
                f'<span style="background:#1E2530;border:1px solid #2A3038;'
                f'border-radius:3px;padding:3px 9px;color:#D1D5DB;">'
                f'<b>{_metar_n}</b> METAR obs from <b>{len(_metar_stns)}</b> stations</span>'
            )
        if _madis_total > 0:
            _madis_label = f'<b>{_madis_total}</b> MADIS obs from <b>{len(_madis_stns)}</b> stations'
            if _cwop_n > 0:
                _madis_label += f' ({_cwop_n} CWOP)'
            _src_chips.append(
                f'<span style="background:#1E2530;border:1px solid #2A3038;'
                f'border-radius:3px;padding:3px 9px;color:#D1D5DB;">{_madis_label}</span>'
            )
        else:
            # MADIS returned nothing. The demotoken consistently returns zero
            # stations in practice, so we hide the badge entirely when we're
            # using the demotoken — it was just noise. We still show a chip
            # for genuine HTTP errors and for the "no stations in range" case
            # when a registered token is configured (those tell the operator
            # something real).
            _mstat = _sc.get("mesonet_status", {}) or {}
            if _mstat.get("using_demo"):
                # Suppress entirely — demotoken empty results aren't actionable
                _madis_status_chip = None
            elif _mstat.get("http_error"):
                _madis_status_chip = (
                    f'<span style="background:#2A1F1B;border:1px solid #7C2D12;'
                    f'border-radius:3px;padding:3px 9px;color:#fb923c;" '
                    f'title="{_mstat.get("message","")}">'
                    f'MADIS: HTTP {_mstat["http_error"]}</span>'
                )
            else:
                _madis_status_chip = (
                    '<span style="background:#1E2530;border:1px solid #2A3038;'
                    'border-radius:3px;padding:3px 9px;color:#6B7280;">'
                    'MADIS: 0 stations in range</span>'
                )
            if _madis_status_chip is not None:
                _src_chips.append(_madis_status_chip)

        # find_station augmentation chip — non-METAR stations from Meteomatics
        _nonmetar_n = _sc.get("nonmetar_count", 0)
        _fs_status = _sc.get("find_station_status", {}) or {}
        _fs_state = _fs_status.get("state", "not_attempted")
        if _nonmetar_n > 0:
            _nonmetar_stns = _sc.get("nonmetar_stations", [])
            _nonmetar_label = (
                f'<b>{_nonmetar_n}</b> non-METAR obs from '
                f'<b>{len(_nonmetar_stns)}</b> stations'
            )
            _src_chips.append(
                f'<span style="background:#1E2530;border:1px solid #2A3038;'
                f'border-radius:3px;padding:3px 9px;color:#D1D5DB;" '
                f'title="{", ".join(s["id"] for s in _nonmetar_stns)}">'
                f'{_nonmetar_label}</span>'
            )
        elif _fs_state == "catalog_empty":
            _src_chips.append(
                '<span style="background:#1E2530;border:1px solid #2A3038;'
                'border-radius:3px;padding:3px 9px;color:#6B7280;" '
                'title="Meteomatics find_station returned no stations within '
                'verification radius. Non-METAR observation networks have '
                'sparse coverage in this region.">'
                'Non-METAR: none in range</span>'
            )
        elif _fs_state == "all_redundant":
            _fs_catalog_size = _fs_status.get("catalog_size", 0)
            _src_chips.append(
                f'<span style="background:#1E2530;border:1px solid #2A3038;'
                f'border-radius:3px;padding:3px 9px;color:#6B7280;" '
                f'title="{_fs_status.get("message","")}">'
                f'Non-METAR: {_fs_catalog_size} found, all already in METAR set</span>'
            )
        elif _fs_state == "error":
            _src_chips.append(
                f'<span style="background:#2A1F1B;border:1px solid #7C2D12;'
                f'border-radius:3px;padding:3px 9px;color:#fb923c;" '
                f'title="{_fs_status.get("message","")}">'
                f'Non-METAR: error</span>'
            )
        # not_attempted / no_credentials — show nothing (silent)
        if _kestrel_n > 0:
            _src_chips.append(
                f'<span style="background:#1E2530;border:1px solid #2A3038;'
                f'border-radius:3px;padding:3px 9px;color:#D1D5DB;">'
                f'<b>{_kestrel_n}</b> Kestrel sessions</span>'
            )

        _best = _sc.get("best_performer")
        _best_chip = ""
        if _best:
            _best_chip = (
                f'<span style="background:rgba(74,222,128,0.12);border:1px solid #4ade80;'
                f'border-radius:3px;padding:3px 9px;color:#4ade80;font-weight:600;">'
                f'Best: {_best}</span>'
            )

        st.markdown(
            f'<div style="font-size:0.78rem;color:#9CA3AF;margin-bottom:6px;">'
            f'<span style="color:#D1D5DB;font-weight:500;">Window:</span> {_tf_str}'
            f'</div>'
            f'<div style="display:flex;flex-wrap:wrap;gap:8px;font-size:0.74rem;'
            f'margin-bottom:10px;">'
            f'{"".join(_src_chips)} {_best_chip}'
            f'</div>',
            unsafe_allow_html=True,
        )

        # Explicit station provenance list — what the actuals are coming from.
        # Visible inline (not just in a tooltip) so the operator can
        # immediately see and verify the truth set.
        _provenance_lines = []
        if _metar_stns:
            _metar_str = ", ".join(_metar_stns)
            _provenance_lines.append(
                f'<div style="font-size:0.72rem;color:#9CA3AF;margin:2px 0;">'
                f'<span style="color:#6B7280;text-transform:uppercase;letter-spacing:0.4px;'
                f'font-size:0.65rem;font-weight:500;">METAR:</span> '
                f'<span style="color:#D1D5DB;font-family:monospace;">{_metar_str}</span>'
                f'</div>'
            )
        if _madis_stns:
            # Truncate long MADIS lists — they can get very long with mesonets
            _shown = _madis_stns[:20]
            _madis_str = ", ".join(_shown)
            if len(_madis_stns) > 20:
                _madis_str += f' \u2026 +{len(_madis_stns) - 20} more'
            _provenance_lines.append(
                f'<div style="font-size:0.72rem;color:#9CA3AF;margin:2px 0;">'
                f'<span style="color:#6B7280;text-transform:uppercase;letter-spacing:0.4px;'
                f'font-size:0.65rem;font-weight:500;">MADIS:</span> '
                f'<span style="color:#D1D5DB;font-family:monospace;">{_madis_str}</span>'
                f'</div>'
            )
        if _kestrel_n > 0:
            _provenance_lines.append(
                f'<div style="font-size:0.72rem;color:#9CA3AF;margin:2px 0;">'
                f'<span style="color:#6B7280;text-transform:uppercase;letter-spacing:0.4px;'
                f'font-size:0.65rem;font-weight:500;">KESTREL:</span> '
                f'<span style="color:#D1D5DB;">'
                f'{_kestrel_n} session{"s" if _kestrel_n != 1 else ""} from local uploads'
                f'</span>'
                f'</div>'
            )

        if _provenance_lines:
            st.markdown(
                f'<div style="background:#161A1F;border-left:2px solid #2A3038;'
                f'padding:6px 10px;margin-bottom:14px;border-radius:0 3px 3px 0;">'
                f'{"".join(_provenance_lines)}'
                f'</div>',
                unsafe_allow_html=True,
            )

        # Variable selector for the trend chart — placed above the column split
        # so the table on the left and the chart on the right start at exactly
        # the same y-coordinate. Visibility intentionally omitted; mesonet
        # stations rarely report it and METAR-only sample sizes were producing
        # misleading scores.
        _trend_var_options = {
            "Wind speed":        ("wind_mae_kt", "kt"),
            "Wind direction":    ("dir_mae_deg", "\u00b0"),
            "Gusts":             ("gust_mae_kt", "kt"),
            "Temperature":       ("temp_mae_c", "\u00b0C"),
            "RH":                ("rh_mae_pct", "%"),
            "Pressure":          ("pressure_mae_hpa", "hPa"),
        }
        _sel_l, _sel_r = st.columns([1, 1])
        with _sel_r:
            _selected_var = st.selectbox(
                "Trend variable",
                options=list(_trend_var_options.keys()),
                index=0,
                key="model_trend_variable",
                label_visibility="collapsed",
            )
        _var_key, _var_unit = _trend_var_options[_selected_var]

        # --- Two-column layout: scorecard table left, trend chart always-visible right ---
        _perf_left, _perf_right = st.columns([1, 1])

        with _perf_left:
            st.markdown(
                '<div style="font-size:0.74rem;color:#6B7280;text-transform:uppercase;letter-spacing:0.5px;'
                'margin-bottom:8px;font-weight:500;">Mean absolute error by variable</div>',
                unsafe_allow_html=True,
            )

            _grid_template = "70px 50px 50px 50px 50px 45px 55px"

            _sc_header = (
                f'<div style="display:grid;grid-template-columns:{_grid_template};gap:1px;margin-bottom:1px;">'
                f'<div style="font-size:0.66rem;color:#6B7280;text-transform:uppercase;padding:5px 6px;letter-spacing:0.3px;">Model</div>'
                f'<div style="font-size:0.66rem;color:#6B7280;text-transform:uppercase;padding:5px 6px;text-align:center;">Wind</div>'
                f'<div style="font-size:0.66rem;color:#6B7280;text-transform:uppercase;padding:5px 6px;text-align:center;">Dir</div>'
                f'<div style="font-size:0.66rem;color:#6B7280;text-transform:uppercase;padding:5px 6px;text-align:center;">Gust</div>'
                f'<div style="font-size:0.66rem;color:#6B7280;text-transform:uppercase;padding:5px 6px;text-align:center;">Temp</div>'
                f'<div style="font-size:0.66rem;color:#6B7280;text-transform:uppercase;padding:5px 6px;text-align:center;">RH</div>'
                f'<div style="font-size:0.66rem;color:#6B7280;text-transform:uppercase;padding:5px 6px;text-align:center;">Press</div>'
                f'</div>'
            )
            st.markdown(_sc_header, unsafe_allow_html=True)

            # Sort models for display:
            #   1. Scorable models in ascending composite_score order (best first)
            #   2. Out-of-coverage models grouped at the bottom
            #   3. Unavailable models last
            # The composite_score is computed by _composite_score() in
            # modules/model_performance.py: wind×3 + gust×2 + temp×1 + press×0.5
            # + dir×0.05 + rh×0.05 — weighted for UAS operational impact.
            def _sort_key(m):
                _status = m.get("status", "OK")
                if _status == "OUT_OF_COVERAGE":
                    return (1, 0.0)
                if _status == "UNAVAILABLE":
                    return (2, 0.0)
                _cs = m.get("composite_score", float("inf"))
                if _cs is None or _cs == float("inf"):
                    return (2, 0.0)
                return (0, _cs)

            _sorted_models = sorted(_sc["models"], key=_sort_key)
            # Track the rank only among scorable models so OUT_OF_COVERAGE
            # and UNAVAILABLE rows don't consume a rank slot.
            _rank_counter = 0

            for _m in _sorted_models:
                _name = _m["name"]
                _is_best = (_name == _best)
                _row_bg = "#1E2530" if _is_best else "#161A1F"
                _name_style = "color:#4ade80;font-weight:600;" if _is_best else "color:#D1D5DB;"

                # Show the rank for scorable models. Rank label is grey, name
                # styled per the best-performer flag.
                _status = _m.get("status", "OK")
                if _status in ("OUT_OF_COVERAGE", "UNAVAILABLE"):
                    _rank_prefix = ""
                else:
                    _rank_counter += 1
                    _rank_prefix = (
                        f'<span style="color:#6B7280;font-size:0.70rem;'
                        f'font-variant-numeric:tabular-nums;margin-right:6px;">'
                        f'{_rank_counter}.</span>'
                    )

                if _m["status"] == "OUT_OF_COVERAGE":
                    # Distinct dimmed row + an explicit "Out of coverage" message
                    # spanning the data columns. This is much clearer than letting
                    # Open-Meteo silently substitute GFS data.
                    _msg_cell = (
                        f'<div style="font-size:0.7rem;color:#6B7280;padding:5px 6px;'
                        f'background:{_row_bg};text-align:center;font-style:italic;'
                        f'grid-column: span 6;">Outside coverage area</div>'
                    )
                    _sc_row = (
                        f'<div style="display:grid;grid-template-columns:{_grid_template};gap:1px;opacity:0.55;">'
                        f'<div style="font-size:0.82rem;color:#6B7280;padding:5px 6px;background:{_row_bg};">{_rank_prefix}{_name}</div>'
                        f'{_msg_cell}'
                        f'</div>'
                    )
                    st.markdown(_sc_row, unsafe_allow_html=True)
                    continue

                if _m["status"] == "UNAVAILABLE":
                    _empty_cell = (
                        f'<div style="font-size:0.78rem;color:#6B7280;padding:5px 6px;'
                        f'background:{_row_bg};text-align:center;">\u2014</div>'
                    )
                    _sc_row = (
                        f'<div style="display:grid;grid-template-columns:{_grid_template};gap:1px;">'
                        f'<div style="font-size:0.82rem;{_name_style}padding:5px 6px;background:{_row_bg};">{_rank_prefix}{_name}</div>'
                        f'{_empty_cell * 6}'
                        f'</div>'
                    )
                    st.markdown(_sc_row, unsafe_allow_html=True)
                    continue

                # Cells with grade-coded colors
                def _cell(mae, grade_fn, fmt="{:.1f}"):
                    if mae is None:
                        return ("#6B7280", "\u2014")
                    grade = grade_fn(mae)
                    clr = GRADE_COLORS.get(grade, "#9CA3AF")
                    return (clr, fmt.format(mae))

                _w_clr, _w_val = _cell(_m["wind_mae_kt"], grade_wind_mae)
                _d_clr, _d_val = _cell(_m["dir_mae_deg"], grade_dir_mae, "{:.0f}")
                _g_clr, _g_val = _cell(_m["gust_mae_kt"], grade_gust_mae)
                _t_clr, _t_val = _cell(_m["temp_mae_c"], grade_temp_mae)
                _rh_clr, _rh_val = _cell(_m["rh_mae_pct"], grade_rh_mae, "{:.0f}")
                _p_clr, _p_val = _cell(_m["pressure_mae_hpa"], grade_pressure_mae)

                def _datacell(clr, val, weight="400"):
                    return (
                        f'<div style="font-size:0.82rem;color:{clr};padding:5px 6px;'
                        f'background:{_row_bg};font-variant-numeric:tabular-nums;'
                        f'font-weight:{weight};text-align:center;">{val}</div>'
                    )

                _sc_row = (
                    f'<div style="display:grid;grid-template-columns:{_grid_template};gap:1px;">'
                    f'<div style="font-size:0.82rem;{_name_style}padding:5px 6px;background:{_row_bg};">{_rank_prefix}{_name}</div>'
                    f'{_datacell(_w_clr, _w_val, "500")}'
                    f'{_datacell(_d_clr, _d_val)}'
                    f'{_datacell(_g_clr, _g_val)}'
                    f'{_datacell(_t_clr, _t_val)}'
                    f'{_datacell(_rh_clr, _rh_val)}'
                    f'{_datacell(_p_clr, _p_val)}'
                    f'</div>'
                )
                st.markdown(_sc_row, unsafe_allow_html=True)

            # Legend
            st.markdown(
                '<div style="font-size:0.66rem;color:#6B7280;margin-top:10px;line-height:1.5;">'
                'MAE units: Wind/Gust kt \u00b7 Dir \u00b0 \u00b7 Temp \u00b0C \u00b7 '
                'RH % \u00b7 Press hPa. '
                '<span style="color:#4ade80;">green</span> = within tolerance, '
                '<span style="color:#E58E26;">amber</span> = drifting, '
                '<span style="color:#ff6b4a;">red</span> = systematically off.'
                '<br>Ranked by UAS-operational composite: wind \u00d73 + gust \u00d72 + '
                'temp \u00d71 + press \u00d70.5 + dir \u00d70.05/\u00b0 + rh \u00d70.05/%. '
                'Lowest sum = best forecast for ops.'
                '</div>',
                unsafe_allow_html=True,
            )

            # Cross-reference: if a drone sounding was verified this session,
            # surface its result here. The sounding verifies vertical structure
            # (a different question than the surface scorecard above), so a
            # disagreement between the two is operationally meaningful — it
            # means a model good at the surface may be poor through the layer.
            _sv = st.session_state.get("_last_sounding_verification")
            if _sv:
                _sv_best = _sv.get("best_model", "\u2014")
                _sv_sfc_best = _best  # surface scorecard's best
                _sv_agree = (_sv_best == _sv_sfc_best)
                _sv_launch = _sv.get("launch_time", "")[:16].replace("T", " ")
                _agree_txt = (
                    '<span style="color:#4ade80;">agrees with</span>' if _sv_agree
                    else '<span style="color:#E58E26;">differs from</span>'
                )
                # Build the interpretation line outside the f-string. Python
                # 3.11 forbids backslashes (here, the \u2014 em-dash escapes)
                # inside f-string expression braces, so this must be a plain
                # string assignment.
                _em = "\u2014"
                if _sv_agree:
                    _sv_interp = (f"Surface and profile agree {_em} high confidence "
                                  f"in this model for layered ops.")
                else:
                    _sv_interp = (f"Surface and profile disagree {_em} the surface-best "
                                  f"model may misrepresent the wind structure aloft. "
                                  f"Weight the profile result for climb/descent planning.")
                st.markdown(
                    f'<div style="margin-top:14px;padding:10px 12px;background:#161A1F;'
                    f'border-left:2px solid #3b82f6;border-radius:0 4px 4px 0;">'
                    f'<div style="font-size:0.64rem;color:#6B7280;text-transform:uppercase;'
                    f'letter-spacing:0.5px;margin-bottom:4px;">Drone sounding cross-check</div>'
                    f'<div style="font-size:0.72rem;color:#D1D5DB;line-height:1.5;">'
                    f'{_sv.get("aircraft","")} \u00b7 {_sv_launch}Z \u00b7 '
                    f'{_sv.get("span_ft",0):.0f} ft profile, {_sv.get("n_layers",0)} layers.'
                    f'<br>Best vertical-profile fit: <b style="color:#3b82f6;">{_sv_best}</b> '
                    f'\u2014 {_agree_txt} the surface scorecard\u2019s best ({_sv_sfc_best}).'
                    f'</div>'
                    f'<div style="font-size:0.6rem;color:#6B7280;margin-top:5px;">'
                    f'{_sv_interp}'
                    f'</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

        with _perf_right:
            st.markdown(
                '<div style="font-size:0.74rem;color:#6B7280;text-transform:uppercase;letter-spacing:0.5px;'
                'margin-bottom:8px;font-weight:500;">Error trend (6h rolling MAE)</div>',
                unsafe_allow_html=True,
            )

            _model_colors = {
                "HRDPS":      "#60a5fa",
                "ICON-EU":    "#60a5fa",
                "ACCESS-G":   "#60a5fa",
                "Best Match": "#60a5fa",
                "GFS":        "#f59e0b",
                "ECMWF":      "#10b981",
                "ICON":       "#a78bfa",
                "NAM":        "#ec4899",   # pink
                "HRRR":       "#06b6d4",   # cyan
            }

            _fig_trend = go.Figure()
            _has_any_data = False

            for _m in _sc["models"]:
                _name = _m["name"]
                _rolling = _m.get("rolling") or {}
                _centers = _rolling.get("window_centers", []) or []
                _values = _rolling.get(_var_key, []) or []
                if not _centers or not _values:
                    continue

                try:
                    _x_local = [c.astimezone(local_tz) for c in _centers]
                except Exception:
                    _x_local = _centers

                _line_color = _model_colors.get(_name, "#94a3b8")

                _fig_trend.add_trace(go.Scatter(
                    x=_x_local,
                    y=_values,
                    mode='lines+markers',
                    name=_name,
                    line=dict(color=_line_color, width=2),
                    marker=dict(size=4),
                    connectgaps=False,
                    hovertemplate=f"<b>{_name}</b><br>%{{x|%d %b %H:%M}}<br>MAE: %{{y:.1f}} {_var_unit}<extra></extra>",
                ))
                if any(v is not None for v in _values):
                    _has_any_data = True

            if _has_any_data:
                # Height tuned to align visually with the 4-row scorecard table
                # on the left (~150px including header). Plot area sits next to
                # the rows, legend bottom-anchored matches the legend strip below.
                _fig_trend.update_layout(
                    height=200,
                    margin=dict(l=44, r=8, t=4, b=28),
                    plot_bgcolor="#1B1E23",
                    paper_bgcolor="#1B1E23",
                    xaxis=dict(
                        showgrid=True, gridcolor="#2A3038",
                        tickfont=dict(color="#A0A4AB", size=9),
                        zeroline=False, fixedrange=True,
                        nticks=5,
                    ),
                    yaxis=dict(
                        title=dict(text=_var_unit, font=dict(color="#A0A4AB", size=9), standoff=4),
                        showgrid=True, gridcolor="#2A3038",
                        tickfont=dict(color="#A0A4AB", size=9),
                        zeroline=False, fixedrange=True, rangemode="tozero",
                    ),
                    hovermode="x unified",
                    legend=dict(
                        orientation="h", yanchor="top", y=-0.18,
                        xanchor="left", x=0.0,
                        font=dict(color="#D1D5DB", size=9),
                        bgcolor="rgba(0,0,0,0)",
                    ),
                    dragmode=False,
                )
                st.plotly_chart(_fig_trend, use_container_width=True, config={"displayModeBar": False})
            else:
                st.markdown(
                    '<div style="font-size:0.78rem;color:#9CA3AF;line-height:1.5;padding:20px 0;">'
                    'Insufficient paired observations to render a trend for this variable. '
                    'This is normal in the first few hours after a fresh model run, or for '
                    'variables not reported by the available stations.'
                    '</div>',
                    unsafe_allow_html=True,
                )

st.divider()


# =============================================================================
# DATA CAPTURE VERIFICATION — Kestrel 5500 Ground Truth
# =============================================================================

st.subheader("Data Capture Verification")

_vf_left, _vf_right = st.columns([1, 2])

with _vf_left:
    st.markdown(
        '<div style="font-size:0.7rem;color:#6B7280;margin-bottom:8px;">'
        'Upload a Kestrel 5500 CSV to compare ground-truth measurements against the active NWP forecast.'
        '</div>',
        unsafe_allow_html=True,
    )
    _kestrel_file = st.file_uploader(
        "Kestrel 5500 CSV",
        type=["csv"],
        key="kestrel_upload",
        label_visibility="collapsed",
    )

    # Magnetic declination — auto-computed from current coordinates using IGRF.
    # Operators can manually override if they have a more precise local value.
    _auto_dec = get_magnetic_declination(lat, lon)
    _mag_dec = st.number_input(
        "Magnetic declination (°, East positive)",
        value=float(_auto_dec),
        step=0.5,
        format="%.1f",
        help=(f"Auto-computed for {lat:.2f}, {lon:.2f} via IGRF model. "
              f"Applied to Kestrel wind direction for true north correction. "
              f"Override if you have a more precise local value."),
    )

if _kestrel_file is not None:
    try:
        _file_bytes = _kestrel_file.getvalue()
        _file_text = _file_bytes.decode("utf-8", errors="replace")
        _file_hash = compute_file_hash(_file_bytes)

        # Parse Kestrel CSV
        _observations = parse_kestrel_csv(_file_text, magnetic_declination=_mag_dec)

        if not _observations or len(_observations) < 3:
            st.warning("Could not parse enough data points from the CSV. Check the file format.")
        else:
            # Average the session
            _session = average_session(_observations, magnetic_declination=_mag_dec)
            _session.file_hash = _file_hash

            # Match against forecast
            _match = match_forecast_hour(_session, h["time"], h)

            if _match is None:
                st.warning("No forecast hour within 90 minutes of the Kestrel session. Ensure the forecast is loaded for the same date.")
            else:
                # Determine model name from model_choice
                _model_label = model_choice.split("/")[-1].replace("_", " ").upper() if model_choice else "NWP"

                # Compute verification
                _vr = compute_verification(
                    session=_session,
                    forecast=_match,
                    elevation_ft=sfc_elevation,
                    operator=st.session_state.get("active_operator", "UNKNOWN"),
                    lat=lat, lon=lon,
                    model_name=_model_label,
                )

                # Store in Supabase
                _sb = _safe_get_supabase()
                if _sb:
                    _stored = store_verification(_sb, _vr)
                    if _stored:
                        st.toast("Verification stored.", icon="\u2705")

                # --- DISPLAY RESULT ---
                with _vf_right:
                    # MVS Score and grade
                    _grade_colors = {"A": "#4ade80", "B": "#94a3b8", "C": "#E58E26", "F": "#ff6b4a"}
                    _gc = _grade_colors.get(_vr.grade, "#94a3b8")

                    st.markdown(
                        f'<div style="display:flex;align-items:center;gap:16px;margin-bottom:12px;">'
                        f'<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;'
                        f'width:64px;height:64px;border:2px solid {_gc};border-radius:8px;">'
                        f'<div style="font-size:1.4rem;font-weight:700;color:{_gc};font-variant-numeric:tabular-nums;">{_vr.mvs}</div>'
                        f'<div style="font-size:0.55rem;color:#6B7280;text-transform:uppercase;letter-spacing:0.5px;">MVS</div>'
                        f'</div>'
                        f'<div>'
                        f'<div style="font-size:0.85rem;color:#E5E7EB;font-weight:500;">{_vr.assessment}</div>'
                        f'<div style="font-size:0.65rem;color:#6B7280;margin-top:2px;">'
                        f'{_session.sample_count} samples \u00b7 {_session.duration_seconds}s session \u00b7 '
                        f'{_model_label} +{_vr.lead_time_hours}h</div>'
                        f'</div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

                    # Delta table
                    _vars = [
                        ("Wind Speed",  f"{_vr.actual_wind_kt:.0f} kt",  f"{_vr.fcst_wind_kt:.0f} kt",  f"{_vr.delta_wind_kt:+.1f} kt"),
                        ("Wind Dir",    f"{_vr.actual_wind_dir:.0f}\u00b0", f"{_vr.fcst_wind_dir:.0f}\u00b0", f"{_vr.delta_wind_dir:+.0f}\u00b0"),
                        ("Temperature", f"{_vr.actual_temp_c:.1f}\u00b0C",  f"{_vr.fcst_temp_c:.1f}\u00b0C",  f"{_vr.delta_temp_c:+.1f}\u00b0C"),
                        ("RH",          f"{_vr.actual_rh:.0f}%",          f"{_vr.fcst_rh:.0f}%",          f"{_vr.delta_rh:+.0f}%"),
                        ("Pressure",    f"{_vr.actual_pressure_hpa:.0f} hPa", f"{_vr.fcst_pressure_hpa:.0f} hPa", f"{_vr.delta_pressure_hpa:+.1f} hPa"),
                        ("Density Alt", f"{_vr.actual_density_alt_ft:,} ft", f"{_vr.fcst_density_alt_ft:,} ft", f"{_vr.delta_density_alt_ft:+,} ft"),
                    ]

                    _tbl_header = (
                        '<div style="display:grid;grid-template-columns:100px 90px 90px 90px;gap:1px;margin-bottom:1px;">'
                        '<div style="font-size:0.6rem;color:#6B7280;text-transform:uppercase;padding:4px 6px;letter-spacing:0.5px;"></div>'
                        '<div style="font-size:0.6rem;color:#6B7280;text-transform:uppercase;padding:4px 6px;letter-spacing:0.5px;">Kestrel</div>'
                        '<div style="font-size:0.6rem;color:#6B7280;text-transform:uppercase;padding:4px 6px;letter-spacing:0.5px;">Forecast</div>'
                        '<div style="font-size:0.6rem;color:#6B7280;text-transform:uppercase;padding:4px 6px;letter-spacing:0.5px;">Delta</div>'
                        '</div>'
                    )
                    st.markdown(_tbl_header, unsafe_allow_html=True)

                    for _vname, _actual, _fcst, _delta_str in _vars:
                        # Parse delta magnitude for coloring
                        try:
                            _dval = float(_delta_str.split()[0].replace(",", "").replace("\u00b0", ""))
                        except (ValueError, IndexError):
                            _dval = 0
                        if _vname == "Wind Speed":
                            _d_clr = "#ff6b4a" if abs(_dval) >= 5 else "#E58E26" if abs(_dval) >= 3 else "#9CA3AF"
                        elif _vname == "Wind Dir":
                            _d_clr = "#ff6b4a" if abs(_dval) >= 30 else "#E58E26" if abs(_dval) >= 15 else "#9CA3AF"
                        elif _vname == "Temperature":
                            _d_clr = "#ff6b4a" if abs(_dval) >= 3 else "#E58E26" if abs(_dval) >= 2 else "#9CA3AF"
                        else:
                            _d_clr = "#9CA3AF"

                        _tbl_row = (
                            f'<div style="display:grid;grid-template-columns:100px 90px 90px 90px;gap:1px;">'
                            f'<div style="font-size:0.72rem;color:#9CA3AF;padding:3px 6px;background:#161A1F;">{_vname}</div>'
                            f'<div style="font-size:0.72rem;color:#E5E7EB;padding:3px 6px;background:#161A1F;font-variant-numeric:tabular-nums;">{_actual}</div>'
                            f'<div style="font-size:0.72rem;color:#E5E7EB;padding:3px 6px;background:#161A1F;font-variant-numeric:tabular-nums;">{_fcst}</div>'
                            f'<div style="font-size:0.72rem;color:{_d_clr};padding:3px 6px;background:#161A1F;font-weight:500;font-variant-numeric:tabular-nums;">{_delta_str}</div>'
                            f'</div>'
                        )
                        st.markdown(_tbl_row, unsafe_allow_html=True)

                    # Flags
                    if _vr.flags:
                        _flags_html = '<div style="margin-top:10px;">'
                        for _flag in _vr.flags:
                            _flags_html += (
                                f'<div style="font-size:0.65rem;color:#E58E26;margin:2px 0;display:flex;align-items:center;gap:5px;">'
                                f'<span style="font-size:0.5rem;">\u26a0</span> {_flag}</div>'
                            )
                        _flags_html += '</div>'
                        st.markdown(_flags_html, unsafe_allow_html=True)

    except UnicodeDecodeError:
        st.error("File encoding error. The Kestrel CSV should be UTF-8 or ASCII.")
    except Exception as e:
        st.error(f"Verification failed: {e}")

# --- Trailing MVS History (shows even without upload) ---
_sb_vf = _safe_get_supabase()
if _sb_vf:
    _recent = load_recent_verifications(_sb_vf, lat, lon, days=90)
    if _recent:
        st.markdown(
            '<div style="font-size:0.7rem;color:#6B7280;text-transform:uppercase;letter-spacing:0.5px;'
            'margin-top:16px;margin-bottom:8px;">Recent Verifications</div>',
            unsafe_allow_html=True,
        )
        _hist_rows = ""
        for _rv in _recent[:10]:
            _rv_gc = _grade_colors.get(_rv.get("grade", "B"), "#94a3b8") if '_grade_colors' in dir() else "#94a3b8"
            _rv_ts = _rv.get("timestamp", "")[:16].replace("T", " ")
            _rv_mvs = _rv.get("mvs", 0)
            _rv_dw = _rv.get("delta_wind_kt", 0) or 0
            _rv_dt = _rv.get("delta_temp_c", 0) or 0
            _rv_model = _rv.get("model_name", "")
            _rv_op = _rv.get("operator", "")

            _gc_map = {"A": "#4ade80", "B": "#94a3b8", "C": "#E58E26", "F": "#ff6b4a"}
            _rv_clr = _gc_map.get(_rv.get("grade", "B"), "#94a3b8")

            _hist_rows += (
                f'<div style="display:grid;grid-template-columns:130px 50px 80px 70px 70px;gap:1px;margin-bottom:1px;">'
                f'<div style="font-size:0.65rem;color:#9CA3AF;padding:3px 6px;background:#161A1F;">{_rv_ts}</div>'
                f'<div style="font-size:0.65rem;color:{_rv_clr};padding:3px 6px;background:#161A1F;font-weight:600;font-variant-numeric:tabular-nums;">{_rv_mvs}</div>'
                f'<div style="font-size:0.65rem;color:#9CA3AF;padding:3px 6px;background:#161A1F;">{_rv_model}</div>'
                f'<div style="font-size:0.65rem;color:#9CA3AF;padding:3px 6px;background:#161A1F;font-variant-numeric:tabular-nums;">\u0394w {_rv_dw:+.0f}kt</div>'
                f'<div style="font-size:0.65rem;color:#9CA3AF;padding:3px 6px;background:#161A1F;font-variant-numeric:tabular-nums;">\u0394t {_rv_dt:+.1f}\u00b0</div>'
                f'</div>'
            )

        _hist_header = (
            '<div style="display:grid;grid-template-columns:130px 50px 80px 70px 70px;gap:1px;margin-bottom:1px;">'
            '<div style="font-size:0.55rem;color:#6B7280;padding:3px 6px;text-transform:uppercase;">Time</div>'
            '<div style="font-size:0.55rem;color:#6B7280;padding:3px 6px;text-transform:uppercase;">MVS</div>'
            '<div style="font-size:0.55rem;color:#6B7280;padding:3px 6px;text-transform:uppercase;">Model</div>'
            '<div style="font-size:0.55rem;color:#6B7280;padding:3px 6px;text-transform:uppercase;">Wind</div>'
            '<div style="font-size:0.55rem;color:#6B7280;padding:3px 6px;text-transform:uppercase;">Temp</div>'
            '</div>'
        )
        st.markdown(_hist_header + _hist_rows, unsafe_allow_html=True)

st.divider()


# =============================================================================
# DRONE SOUNDING VERIFICATION — Vertical Profile Ground Truth
# =============================================================================
# Ingests a meteo-drone ascent CSV (e.g. Meteomatics MM-670M) and verifies
# the full vertical profile against all 8 models, layer by layer. Unlike the
# Kestrel surface comparison, this scores the model's VERTICAL STRUCTURE —
# whether it captures boundary-layer wind shear, not just the surface value.

st.subheader("Drone Sounding Verification")

_ds_left, _ds_right = st.columns([1, 2])

with _ds_left:
    st.markdown(
        '<div style="font-size:0.7rem;color:#6B7280;margin-bottom:8px;">'
        'Upload a meteo-drone ascent CSV (MM-670 series) to verify the full '
        'vertical profile against all models, layer by layer. Captures '
        'boundary-layer structure that surface comparison misses.'
        '</div>',
        unsafe_allow_html=True,
    )
    _sounding_file = st.file_uploader(
        "Drone sounding CSV",
        type=["csv"],
        key="sounding_upload",
        label_visibility="collapsed",
    )
    _ds_bin = st.select_slider(
        "Layer bin size",
        options=[25, 50, 100, 150],
        value=50,
        format_func=lambda x: f"{x} ft",
        help="Vertical resolution of the comparison. Smaller bins = finer "
             "structure but fewer drone samples per layer.",
    )

if _sounding_file is not None:
    try:
        _ds_bytes = _sounding_file.getvalue()
        _ds_text = _ds_bytes.decode("utf-8", errors="replace")

        # Parse first so we can show profile metadata even if model fetch is slow
        _profile = parse_sounding_csv(_ds_text)

        if _profile is None:
            with _ds_right:
                st.error("Could not parse this CSV as a drone sounding. Expected "
                         "an MM-670-style semicolon-delimited file with altitude, "
                         "temperature, wind, and position columns.")
        else:
            # Determine CONUS coverage for HRRR/NAM gating at this location
            _ds_in_conus = (24.0 <= _profile.lat <= 50.0) and (-125.0 <= _profile.lon <= -66.0)

            with st.spinner(f"Verifying {_profile.n_samples}-sample ascent "
                            f"against all models..."):
                _pv = verify_sounding_csv(_ds_text, in_conus=_ds_in_conus, bin_ft=float(_ds_bin))

            # Stash the result so the Model Performance page can reference the
            # most recent sounding verification this session.
            if _pv is not None and _pv.model_scores:
                st.session_state["_last_sounding_verification"] = {
                    "aircraft": _pv.aircraft,
                    "launch_time": _pv.launch_time.isoformat(),
                    "lat": _pv.lat, "lon": _pv.lon,
                    "best_model": _pv.best_model,
                    "n_layers": _pv.n_layers,
                    "span_ft": _profile.span_ft,
                    "model_scores": _pv.model_scores,
                }

            with _ds_right:
                if _pv is None:
                    st.error("Verification failed during processing.")
                else:
                    # Profile metadata banner
                    _launch_str = _pv.launch_time.strftime("%d %b %Y %H:%M UTC")
                    st.markdown(
                        f'<div style="background:#161A1F;border-radius:6px;padding:10px 14px;'
                        f'margin-bottom:12px;">'
                        f'<div style="font-size:0.75rem;color:#D1D5DB;font-weight:600;">'
                        f'{_pv.aircraft} \u00b7 {_launch_str}</div>'
                        f'<div style="font-size:0.66rem;color:#6B7280;margin-top:3px;">'
                        f'{_pv.lat:.4f}, {_pv.lon:.4f} \u00b7 '
                        f'{_profile.span_ft:.0f} ft ascent \u00b7 {_pv.n_layers} layers '
                        f'\u00b7 {_pv.bin_ft:.0f} ft bins</div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

                    if not _pv.model_scores:
                        st.warning("Profile parsed successfully, but no model data "
                                   "could be fetched for this location/time. The "
                                   "models may not have archived data for this date, "
                                   "or the launch time is too far in the past.")
                    else:
                        # Best model badge
                        if _pv.best_model:
                            st.markdown(
                                f'<span style="display:inline-block;background:rgba(74,222,128,0.12);'
                                f'border:1px solid #4ade80;border-radius:4px;padding:3px 10px;'
                                f'font-size:0.7rem;color:#4ade80;font-weight:600;margin-bottom:10px;">'
                                f'Best profile fit: {_pv.best_model}</span>',
                                unsafe_allow_html=True,
                            )

                        # Profile-aggregate MAE table, ranked by composite
                        def _ds_composite(s):
                            sc = 0.0
                            if s["wind_mae"] is not None:  sc += s["wind_mae"] * 3.0
                            if s["temp_mae"] is not None:  sc += s["temp_mae"] * 1.0
                            if s["pressure_mae"] is not None: sc += s["pressure_mae"] * 0.5
                            if s["dir_mae"] is not None:   sc += s["dir_mae"] * 0.05
                            if s["rh_mae"] is not None:    sc += s["rh_mae"] * 0.05
                            return sc if any(s[k] is not None for k in ["wind_mae", "temp_mae"]) else float("inf")

                        _ranked = sorted(
                            _pv.model_scores.items(),
                            key=lambda kv: _ds_composite(kv[1])
                        )

                        def _ds_cell(val, unit, good, warn):
                            if val is None:
                                return '<div style="font-size:0.74rem;color:#4B5563;padding:5px 6px;text-align:center;">\u2014</div>'
                            clr = "#4ade80" if val <= good else ("#E58E26" if val <= warn else "#ff6b4a")
                            return (f'<div style="font-size:0.78rem;color:{clr};padding:5px 6px;'
                                    f'text-align:center;font-variant-numeric:tabular-nums;">{val:.1f}</div>')

                        _ds_grid = "minmax(70px,1.2fr) repeat(4, minmax(48px,1fr))"
                        _ds_header = (
                            f'<div style="display:grid;grid-template-columns:{_ds_grid};gap:1px;margin-bottom:1px;">'
                            f'<div style="font-size:0.6rem;color:#6B7280;text-transform:uppercase;padding:5px 6px;">Model</div>'
                            f'<div style="font-size:0.6rem;color:#6B7280;text-transform:uppercase;padding:5px 6px;text-align:center;">Wind</div>'
                            f'<div style="font-size:0.6rem;color:#6B7280;text-transform:uppercase;padding:5px 6px;text-align:center;">Dir</div>'
                            f'<div style="font-size:0.6rem;color:#6B7280;text-transform:uppercase;padding:5px 6px;text-align:center;">Temp</div>'
                            f'<div style="font-size:0.6rem;color:#6B7280;text-transform:uppercase;padding:5px 6px;text-align:center;">RH</div>'
                            f'</div>'
                        )
                        _ds_rows = ""
                        for _rank, (_m, _s) in enumerate(_ranked, 1):
                            _is_best = (_m == _pv.best_model)
                            _bg = "#1E2530" if _is_best else "#161A1F"
                            _nm_clr = "#4ade80" if _is_best else "#D1D5DB"
                            _ds_rows += (
                                f'<div style="display:grid;grid-template-columns:{_ds_grid};gap:1px;margin-bottom:1px;">'
                                f'<div style="font-size:0.78rem;color:{_nm_clr};padding:5px 6px;background:{_bg};">'
                                f'<span style="color:#6B7280;font-size:0.68rem;margin-right:5px;">{_rank}.</span>{_m}</div>'
                                f'<div style="background:{_bg};">{_ds_cell(_s["wind_mae"], "kt", 2.0, 4.0)}</div>'
                                f'<div style="background:{_bg};">{_ds_cell(_s["dir_mae"], "deg", 15, 30)}</div>'
                                f'<div style="background:{_bg};">{_ds_cell(_s["temp_mae"], "C", 1.5, 3.0)}</div>'
                                f'<div style="background:{_bg};">{_ds_cell(_s["rh_mae"], "pct", 5, 12)}</div>'
                                f'</div>'
                            )
                        st.markdown(_ds_header + _ds_rows, unsafe_allow_html=True)
                        st.markdown(
                            '<div style="font-size:0.62rem;color:#6B7280;margin-top:8px;">'
                            'Profile-mean MAE across all layers. Wind kt \u00b7 Dir \u00b0 '
                            '\u00b7 Temp \u00b0C \u00b7 RH %. Ranked by UAS-operational '
                            'composite (wind-weighted). This scores vertical structure: '
                            'a model can nail the surface yet miss the shear aloft.'
                            '</div>',
                            unsafe_allow_html=True,
                        )

                        # Wind profile chart: observed vs best model, by altitude
                        try:
                            import plotly.graph_objects as _go
                            _layers = bin_profile_by_alt(_profile, bin_ft=float(_ds_bin))
                            _obs_alt = [l.alt_agl_ft for l in _layers]
                            _obs_wind = [l.wind_speed_kt for l in _layers]

                            _fig_prof = _go.Figure()
                            _fig_prof.add_trace(_go.Scatter(
                                x=_obs_wind, y=_obs_alt, mode="lines+markers",
                                name="Drone obs", line=dict(color="#4ade80", width=3),
                                marker=dict(size=6),
                            ))
                            # Best model's interpolated wind per layer
                            if _pv.best_model:
                                _bm_wind = [
                                    ld.model_wind for ld in _pv.layer_details
                                    if ld.model == _pv.best_model and ld.model_wind is not None
                                ]
                                _bm_alt = [
                                    ld.alt_agl_ft for ld in _pv.layer_details
                                    if ld.model == _pv.best_model and ld.model_wind is not None
                                ]
                                if _bm_wind:
                                    _fig_prof.add_trace(_go.Scatter(
                                        x=_bm_wind, y=_bm_alt, mode="lines+markers",
                                        name=_pv.best_model,
                                        line=dict(color="#3b82f6", width=2, dash="dot"),
                                        marker=dict(size=5),
                                    ))
                            _fig_prof.update_layout(
                                title=dict(text="Wind speed profile \u2014 obs vs model",
                                           font=dict(size=12, color="#9CA3AF")),
                                xaxis_title="Wind speed (kt)",
                                yaxis_title="Altitude (ft AGL)",
                                height=320, margin=dict(l=10, r=10, t=36, b=10),
                                paper_bgcolor="rgba(0,0,0,0)",
                                plot_bgcolor="rgba(0,0,0,0)",
                                font=dict(color="#9CA3AF", size=10),
                                legend=dict(font=dict(size=9), orientation="h",
                                            yanchor="bottom", y=1.02, xanchor="right", x=1),
                                xaxis=dict(gridcolor="#2A2F36"),
                                yaxis=dict(gridcolor="#2A2F36"),
                            )
                            st.plotly_chart(_fig_prof, use_container_width=True,
                                            config={'displayModeBar': False})
                        except Exception as _chart_e:
                            st.caption(f"Profile chart unavailable: {_chart_e}")

    except UnicodeDecodeError:
        with _ds_right:
            st.error("File encoding error. The drone CSV should be UTF-8 or ASCII.")
    except Exception as _ds_err:
        with _ds_right:
            st.error(f"Sounding verification failed: {_ds_err}")

st.divider()



# =============================================================================
# THREE-PANEL INTERACTIVE VERTICAL PROFILE TREND
# Past hour, current hour, future hour — side by side. Each panel is a
# high-resolution interactive Skew-T with its own parcel-lift slider so the
# operator can analyze CAPE/CIN for parcels lifted from any level.
# =============================================================================

st.subheader("Vertical Profile Trend")

def _extract_sfc_at(idx_v):
    """Extract surface temp / dewpoint / pressure / wind at a given hourly index."""
    try:
        t_v = h.get('temperature_2m', [0])[idx_v]
        rh_v = h.get('relative_humidity_2m', [0])[idx_v]
        p_v = h.get('surface_pressure', [0])[idx_v]
        ws_v = h.get('wind_speed_10m', [0])[idx_v]
        wd_v = h.get('wind_direction_10m', [0])[idx_v]
        t_c = float(t_v) if t_v is not None else 0.0
        rh_p = int(rh_v) if rh_v is not None else 0
        td_c = calc_td(t_c, rh_p)
        p_h = float(p_v) if p_v is not None else 1013.25
        ws_kt = (float(ws_v) if ws_v is not None else 0.0) * k_conv
        wd_deg = float(wd_v) if wd_v is not None else 0.0
        return t_c, td_c, p_h, ws_kt, wd_deg
    except Exception:
        return None

_max_idx = len(h.get('time', [])) - 1
_idx_past = max(0, forecast_idx - 1)
_idx_now  = forecast_idx
_idx_fut  = min(_max_idx, forecast_idx + 1)

_panel_specs = [
    (_idx_past, "T \u2212 1 h", "#9CA3AF"),
    (_idx_now,  "Current",     "#E58E26"),
    (_idx_fut,  "T + 1 h",     "#9CA3AF"),
]

def _time_label(idx_v):
    try:
        t_iso = h['time'][idx_v]
        dt = datetime.fromisoformat(t_iso).replace(tzinfo=timezone.utc).astimezone(local_tz)
        return dt.strftime('%d %b %H:%M')
    except Exception:
        return ""

_sounding_cols = st.columns(3)
_any_rendered = False

for _panel_idx, (_col, (_pidx, _ptitle, _pcolor)) in enumerate(zip(_sounding_cols, _panel_specs)):
    with _col:
        _sfc = _extract_sfc_at(_pidx)
        if _sfc is None:
            st.warning(f"{_ptitle}: surface data unavailable.")
            continue
        _t, _td, _p, _ws, _wd = _sfc

        # Build the high-resolution profile for this hour
        _profile = extract_high_res_profile(h, _pidx, _t, _td, _p, wind_kt_scale=k_conv)
        if _profile is None:
            st.warning(f"{_ptitle}: insufficient pressure-level data.")
            continue

        # Inject the surface wind into the profile (index 0)
        _profile["wind_kt"][0] = _ws
        _profile["wind_dir"][0] = _wd

        # Pressure bounds for the parcel-lift slider — clamp to this profile's
        # actual data range so the operator can't lift from a non-existent level
        _p_levels = _profile["pressures"]
        _p_sfc = float(_p_levels.max())
        _p_min_data = float(_p_levels.min())
        _slider_top = max(500.0, _p_min_data)

        # Parcel analysis is opt-in. Default OFF — the sounding renders cleanly
        # as just the environmental profile + wind barbs unless the operator
        # explicitly enables parcel lifting for convective analysis.
        _show_parcel = st.checkbox(
            "Lift parcel (CAPE/CIN)",
            value=False,
            key=f"sounding_parcel_on_{_panel_idx}",
            help="Enable to compute and draw a saturated lifted parcel "
                 "(assumes immediate condensation), CAPE/CIN shading, and "
                 "convective diagnostics.",
        )

        # Slider always present (disabled when parcel is off) to keep the
        # panel layout vertically stable when toggling the checkbox.
        _lift_p = st.slider(
            "Lift parcel from (hPa)",
            min_value=int(_slider_top),
            max_value=int(_p_sfc),
            value=int(_p_sfc),
            step=5,
            key=f"sounding_lift_{_panel_idx}",
            disabled=(not _show_parcel),
            help="Drag to lift a saturated parcel from a different pressure "
                 "level and see how CAPE / CIN change. Enable the checkbox above.",
        )

        # Render the interactive Plotly sounding
        _fig, _diag = render_sounding_plotly(
            _profile,
            parcel_lift_p=float(_lift_p),
            title=f"{_ptitle} \u2014 {_time_label(_pidx)}",
            panel_color=_pcolor,
            sfc_elevation_ft=sfc_elevation,
            show_parcel=_show_parcel,
        )
        st.plotly_chart(_fig, use_container_width=True,
                        config={"displayModeBar": False},
                        key=f"sounding_chart_{_panel_idx}")
        _any_rendered = True

        # Diagnostics box — always rendered at constant height. Real values
        # when parcel is on; placeholder dashes when off. Keeps the column
        # vertically stable when toggling the checkbox.
        if _show_parcel and _diag:
            _cape = _diag["cape"]
            _cin = _diag["cin"]
            _lfc = _diag["lfc_hpa"]
            _el = _diag["el_hpa"]

            if _cape >= 2500:
                _cape_clr = "#ff6b4a"
            elif _cape >= 1000:
                _cape_clr = "#E58E26"
            elif _cape > 0:
                _cape_clr = "#4ade80"
            else:
                _cape_clr = "#6B7280"

            _lfc_str = f"{_lfc:.0f} hPa" if _lfc else "\u2014"
            _el_str = f"{_el:.0f} hPa" if _el else "\u2014"

            st.markdown(
                f'<div style="background:#161A1F;border-left:2px solid {_pcolor};'
                f'padding:7px 10px;margin-top:4px;border-radius:0 3px 3px 0;'
                f'font-size:0.72rem;line-height:1.6;min-height:42px;">'
                f'<span style="color:#6B7280;">CAPE</span> '
                f'<span style="color:{_cape_clr};font-weight:600;">{_cape:.0f} J/kg</span> &nbsp;'
                f'<span style="color:#6B7280;">CIN</span> '
                f'<span style="color:#60a5fa;font-weight:600;">{_cin:.0f} J/kg</span><br>'
                f'<span style="color:#6B7280;">LFC</span> '
                f'<span style="color:#D1D5DB;">{_lfc_str}</span> &nbsp;'
                f'<span style="color:#6B7280;">EL</span> '
                f'<span style="color:#D1D5DB;">{_el_str}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f'<div style="background:#161A1F;border-left:2px solid #2A3038;'
                f'padding:7px 10px;margin-top:4px;border-radius:0 3px 3px 0;'
                f'font-size:0.72rem;line-height:1.6;min-height:42px;color:#6B7280;">'
                f'CAPE \u2014 &nbsp; CIN \u2014<br>LFC \u2014 &nbsp; EL \u2014'
                f'</div>',
                unsafe_allow_html=True,
            )

if _any_rendered:
    st.markdown(
        '<div style="font-size:0.7rem;color:#6B7280;line-height:1.5;margin-top:10px;">'
        'High-resolution Skew-T \u2014 every model pressure level is plotted. '
        '<span style="color:#ff4b4b;">Red</span> = temperature, '
        '<span style="color:#2abf2a;">green</span> = dewpoint. '
        'Dry adiabats (warm diagonals), moist adiabats (dotted green), '
        'freezing isotherm (cyan dashed). Gray bands = saturated layers (T\u2212Td \u2264 2\u00b0C). '
        'Wind barbs use aviation-standard glyphs (half-barb = 5 kt, full barb = 10 kt, '
        'pennant = 50 kt, circle = calm) with the staff pointing in the direction '
        'the wind is coming FROM. ASL altitude on the right. '
        'Enable the <b>Lift parcel (CAPE/CIN)</b> checkbox per panel to compute '
        'a saturated lifted parcel \u2014 the parcel is assumed saturated at the '
        'lift level (immediate condensation) and follows the moist adiabat upward. '
        'Red shading = CAPE (positive buoyancy), blue = CIN (negative buoyancy).'
        '</div>',
        unsafe_allow_html=True,
    )


st.markdown("<div style='margin-top: 15px;'></div>", unsafe_allow_html=True)

# SENSOR DEGRADATION METRICS
c2 = st.columns(3)
c2[0].metric("Precipitation Risk",  f"{pop}% ({precip} mm)")
c2[1].metric("Density Altitude (DA)", f"{density_alt:,} ft")
c2[2].metric("CAPE (Instability)",   f"{cape} J/kg")

st.divider()
st.markdown("""
<div style="text-align: center; color: #8E949E; font-size: 0.85rem; padding: 20px;">
<strong>⚠️ FOR SITUATIONAL AWARENESS ONLY</strong><br>
This system translates raw meteorological model data for uncrewed systems. It does not replace official flight weather briefings from the designated civil aviation authority in the operator's jurisdiction. The Pilot in Command (PIC) retains ultimate authority and responsibility for flight safety. Vector Check Aerial Group Inc. assumes no liability for operational decisions made using this tool. <br><br>
<em>Usage of this system, including geographic querying and PDF generation, is actively logged to a secure database for audit and security purposes.</em>
</div>
""", unsafe_allow_html=True)


# AUTO-SAVE STATE PERSISTENCE ENGINE
# FIX: Previously fired on every Streamlit rerender (every slider move).
# Now only writes when the preference values actually change.
_current_prefs_key = f"{lat}_{lon}_{t_wind}_{t_ceil}_{t_vis}_{t_turb}_{t_ice}"
if st.session_state.get("_last_saved_prefs_key") != _current_prefs_key:
    try:
        save_prefs(
            st.session_state.get("active_operator", "UNKNOWN"),
            lat, lon, t_wind, t_ceil, t_vis, t_turb, t_ice
        )
        st.session_state["_last_saved_prefs_key"] = _current_prefs_key
    except Exception:
        pass
