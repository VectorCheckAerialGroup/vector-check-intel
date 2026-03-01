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

# Import Vector Check Modules
from modules.data_ingest import get_aviation_weather, fetch_mission_data
from modules.hazard_logic import get_weather_element, calculate_icing_profile, get_turb_ice, apply_tactical_highlights
from modules.visualizations import plot_convective_profile
from modules.telemetry import log_action
from modules.astronomy import get_astronomical_data
from modules.space_weather import get_kp_index

# --- CONSTANTS & PREFS ---
CONVECTIVE_CCL_MULTIPLIER = 400
METERS_TO_SM = 1609.34
ALL_P_LEVELS = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300, 250, 200, 150]
PREFS_FILE = "user_prefs.json"

# DETACHMENT FALLBACK COORDINATES (Used only if memory is wiped)
USER_DEFAULTS = {
    "VCAG": {"lat": 44.1628, "lon": -77.3832},     # Belleville, ON
    "Vector1": {"lat": 54.4642, "lon": -110.1825}, # Cold Lake, AB
    "Vector2": {"lat": 45.9003, "lon": -77.2818},  # Petawawa, ON
    "Vector3": {"lat": 48.3303, "lon": -70.9961},  # Bagotville, QC
    "Vector4": {"lat": 43.6532, "lon": -79.3832}   # Toronto, ON
}

def load_prefs(user):
    if os.path.exists(PREFS_FILE):
        try:
            with open(PREFS_FILE, "r") as f:
                return json.load(f).get(user, {})
        except Exception: pass
    return {}

def sanitize_prefs(prefs, user):
    """Anti-Corruption Gate: Scrubs poisoned memory states and enforces user-specific baselines."""
    
    # Grab the user's specific detachment base, default to Belleville if unknown ID
    base_loc = USER_DEFAULTS.get(user, {"lat": 44.1628, "lon": -77.3832})
    def_lat, def_lon = base_loc["lat"], base_loc["lon"]
    
    lat = float(prefs.get('lat', def_lat))
    lon = float(prefs.get('lon', def_lon))
    wind = int(prefs.get('wind', 30))
    ceil = int(prefs.get('ceil', 500))
    vis = float(prefs.get('vis', 3.0))
    turb = str(prefs.get('turb', "MOD"))
    ice = str(prefs.get('ice', "NIL"))
    
    # Check for crash-induced zeroes
    if lat == 0.0 and lon == 0.0:
        lat, lon = def_lat, def_lon
    if wind == 0 and ceil == 0:
        wind, ceil, vis = 30, 500, 3.0
        
    return lat, lon, wind, ceil, vis, turb, ice

def save_prefs(user, lat, lon, wind, ceil, vis, turb, ice):
    prefs = {}
    if os.path.exists(PREFS_FILE):
        try:
            with open(PREFS_FILE, "r") as f:
                prefs = json.load(f)
        except Exception: pass
    
    prefs[user] = {
        "lat": float(lat), "lon": float(lon),
        "wind": int(wind), "ceil": int(ceil), "vis": float(vis),
        "turb": turb, "ice": ice
    }
    
    try:
        with open(PREFS_FILE, "w") as f:
            json.dump(prefs, f)
    except Exception: pass

# 1. PAGE CONFIG & CSS
st.set_page_config(page_title="Vector Check: Atmospheric Risk Management", layout="wide")
st.markdown("""
    <style>
    [data-testid="stMetricValue"] { font-size: 1.2rem !important; color: #E58E26 !important; }
    [data-testid="stMetricLabel"] { font-size: 0.8rem !important; color: #A0A4AB !important; text-transform: uppercase; }
    .ifr-text { color: #ff4b4b; font-weight: bold; }
    .mvfr-text { color: inherit !important; font-weight: inherit !important; }
    .fz-warn { background-color: #ff4b4b; color: white; padding: 2px; border-radius: 3px; font-weight: bold; }
    table { margin-left: auto; margin-right: auto; text-align: center !important; width: 90%; border-collapse: collapse; background-color: #1B1E23; }
    th { text-align: center !important; color: #8E949E !important; font-weight: bold !important; padding: 10px !important; border-bottom: 2px solid #3E444E !important; text-transform: uppercase; }
    td { text-align: center !important; padding: 8px !important; color: #D1D5DB !important; border-bottom: 1px solid #2D3139 !important; }
    .obs-text { font-family: "Source Sans Pro", sans-serif; font-size: 0.95rem; line-height: 1.6; color: #D1D5DB; }
    div[data-testid="column"] button { width: 100%; padding: 0px; font-size: 0.8rem; }
    </style>
    """, unsafe_allow_html=True)

# 2. ZERO-COST AUTHENTICATION & LEGAL GATEWAY
def check_password():
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
This Atmospheric Risk Management System is an uncertified, supplemental situational awareness tool. It aggregates and visualizes raw numerical weather prediction (NWP) models. It is STRICTLY PROHIBITED to use this software as a primary or sole source of aeronautical weather information. It DOES NOT replace, nor is it an alternative to, official flight weather briefings provided by NAV CANADA, Environment and Climate Change Canada (ECCC), NOAA, or other designated civil aviation authorities.
<br><br>
<strong>2. ABSOLUTE PILOT IN COMMAND (PIC) RESPONSIBILITY</strong><br>
In accordance with Transport Canada Civil Aviation (TCCA) regulations, the Pilot in Command (PIC) retains absolute, non-transferable authority and responsibility for the safe operation of the aircraft. Atmospheric models are inherently flawed, subject to latency, and cannot accurately predict micro-climates, sudden localized shear, or boundary layer anomalies. Vector Check Aerial Group Inc. does not clear, authorize, or endorse any flight operations.
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
    
    with st.form("login_form"):
        eula_check = st.checkbox("I confirm I am the Pilot in Command (PIC) and I accept the terms of this End User License Agreement.")
        st.markdown("<br>", unsafe_allow_html=True)
        
        user = st.text_input("Operator ID")
        pwd = st.text_input("Passcode", type="password")
        submitted = st.form_submit_button("Acknowledge & Authenticate")
        
        if submitted:
            if user in st.secrets.get("passwords", {}) and pwd == st.secrets["passwords"][user]:
                if not eula_check:
                    st.error("⚠️ REGULATORY HALT: You must accept the End User License Agreement to authenticate.")
                else:
                    st.session_state["password_correct"] = True
                    st.session_state["eula_accepted"] = True
                    st.session_state["active_operator"] = user
                    
                    # LOAD & SANITIZE PREFERENCES ON SUCCESSFUL LOGIN
                    raw_prefs = load_prefs(user)
                    lat, lon, wind, ceil, vis, turb, ice = sanitize_prefs(raw_prefs, user)
                    
                    st.session_state['input_lat'] = lat
                    st.session_state['input_lon'] = lon
                    st.session_state['input_wind'] = wind
                    st.session_state['input_ceil'] = ceil
                    st.session_state['input_vis'] = vis
                    st.session_state['input_turb'] = turb
                    st.session_state['input_ice'] = ice
                    
                    try: log_action(user, 0.0, 0.0, "SYS", "AUTH_AND_EULA_SUCCESS")
                    except: pass
                    st.rerun()
            else:
                st.error("⚠️ UNAUTHORIZED: Invalid Operator ID or Passcode.")
                
    return False

if not check_password():
    st.stop()

# Ensure keys exist if session was restored without hitting the login block
if "input_lat" not in st.session_state:
    current_op = st.session_state.get("active_operator", "UNKNOWN")
    raw_prefs = load_prefs(current_op)
    lat, lon, wind, ceil, vis, turb, ice = sanitize_prefs(raw_prefs, current_op)
    st.session_state['input_lat'] = lat
    st.session_state['input_lon'] = lon
    st.session_state['input_wind'] = wind
    st.session_state['input_ceil'] = ceil
    st.session_state['input_vis'] = vis
    st.session_state['input_turb'] = turb
    st.session_state['input_ice'] = ice

# ---------------------------------------------------------
# HELPER FUNCTIONS 
# ---------------------------------------------------------
def calc_td(t, rh):
    if rh <= 0: return t
    a = 17.625
    b = 243.04
    alpha = math.log(rh / 100.0) + ((a * t) / (b + t))
    return (b * alpha) / (a - alpha)

def get_interp_thermals(alt_msl, profile):
    if not profile: return 0.0, 0
    if alt_msl <= profile[0]['h']: return profile[0]['t'], profile[0]['rh']
    if alt_msl >= profile[-1]['h']: return profile[-1]['t'], profile[-1]['rh']
    for i in range(len(profile)-1):
        if profile[i]['h'] <= alt_msl <= profile[i+1]['h']:
            lower = profile[i]
            upper = profile[i+1]
            frac = (alt_msl - lower['h']) / (upper['h'] - lower['h']) if upper['h'] != lower['h'] else 0
            i_t = lower['t'] + frac * (upper['t'] - lower['t'])
            i_rh = lower['rh'] + frac * (upper['rh'] - lower['rh'])
            return i_t, int(i_rh)
    return profile[0]['t'], profile[0]['rh']

def format_dir(d, spd):
    r = int(round(float(d), -1)) % 360
    if r == 0 and spd > 0: return 360
    if spd == 0: return 0
    return r

def hazard_lvl(h_str):
    h_str = h_str.upper()
    if "SEV" in h_str: return 3
    if "MOD-SEV" in h_str: return 2.5
    if "MOD" in h_str: return 2
    if "LGT" in h_str: return 1
    return 0

def calc_tactical_visibility(vis_raw_m, rh, w_spd, wx):
    if vis_raw_m is not None:
        vis_sm = float(vis_raw_m) / 1609.34
    else:
        if rh >= 95: vis_sm = 1.5
        elif rh >= 90: vis_sm = 3.0
        elif rh >= 80: vis_sm = 5.0
        else: vis_sm = 10.0
        
    if wx >= 50: return vis_sm
    if vis_sm < 3.0 and w_spd >= 10.0 and wx not in [45, 48]: return max(vis_sm, 6.0)
    if vis_sm < 4.0 and rh < 85: return max(vis_sm, 7.0)
    if vis_sm < 3.0 and wx <= 3 and rh < 95: return max(vis_sm, 4.0)
    return vis_sm

# ---------------------------------------------------------
# SPATIAL ENGINES & CACHED DATA FETCH
# ---------------------------------------------------------
@st.cache_data(ttl=86400)
def get_location_name(user_lat, user_lon):
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?lat={user_lat}&lon={user_lon}&format=json"
        req = urllib.request.Request(url, headers={'User-Agent': 'VectorCheck-App/2.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            
        address = data.get('address', {})
        region = address.get('city', address.get('town', address.get('county', address.get('village', address.get('region', 'Unknown Region')))))
        province = address.get('state', address.get('country', 'Unknown'))
        
        if region != 'Unknown Region' and province != 'Unknown':
            return f"{region}, {province}"
        elif province != 'Unknown': return province
        else: return f"Coord: {user_lat:.2f}, {user_lon:.2f}"
    except Exception: 
        return f"Coord: {user_lat:.2f}, {user_lon:.2f}" 

@st.cache_data(ttl=3600)
def get_nearest_icao_station(user_lat, user_lon):
    try:
        min_lat, max_lat = user_lat - 1.0, user_lat + 1.0
        min_lon, max_lon = user_lon - 1.0, user_lon + 1.0
        url = f"https://aviationweather.gov/api/data/taf?bbox={min_lat},{min_lon},{max_lat},{max_lon}&format=json"
        req = urllib.request.Request(url, headers={'User-Agent': 'VectorCheck-App/2.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
        
        best_station = {"icao": "NONE", "dist": float('inf'), "dir": ""}
        seen_icaos = set()
        
        for taf in data:
            if 'icaoId' not in taf or 'lat' not in taf or 'lon' not in taf: continue
            icao_code = taf['icaoId']
            if icao_code in seen_icaos: continue
            seen_icaos.add(icao_code)
            
            stn_lat, stn_lon = float(taf['lat']), float(taf['lon'])
            R = 6371.0 
            lat1, lon1 = math.radians(user_lat), math.radians(user_lon)
            lat2, lon2 = math.radians(stn_lat), math.radians(stn_lon)
            
            dlat, dlon = lat2 - lat1, lon2 - lon1
            a = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
            c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
            dist = R * c
            
            if dist <= 50.0 and dist < best_station["dist"]:
                y = math.sin(dlon) * math.cos(lat2)
                x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
                bearing = (math.degrees(math.atan2(y, x)) + 360) % 360
                dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
                best_station = {"icao": icao_code, "dist": dist, "dir": dirs[int(round(bearing / 45)) % 8]}
        
        if best_station["icao"] != "NONE": return best_station
    except Exception: pass
    return {"icao": "NONE", "dist": None, "dir": ""}

@st.cache_data(ttl=900)
def fetch_weather_payload(fetch_lat, fetch_lon, fetch_model):
    return fetch_mission_data(fetch_lat, fetch_lon, fetch_model)

@st.cache_data(ttl=900)
def fetch_metar_taf(fetch_icao):
    return get_aviation_weather(fetch_icao)

@st.cache_data(ttl=10800)
def fetch_space_weather_cached(dt_iso_str):
    dt_utc = datetime.fromisoformat(dt_iso_str).replace(tzinfo=timezone.utc)
    return get_kp_index(dt_utc)

@st.cache_data(ttl=86400)
def fetch_astronomy_cached(lat_val, lon_val, dt_iso_str, tz_name, tz_abbr_str):
    dt_utc = datetime.fromisoformat(dt_iso_str).replace(tzinfo=timezone.utc)
    local_tz = pytz.timezone(tz_name) if tz_name else timezone.utc
    return get_astronomical_data(lat_val, lon_val, dt_utc, local_tz, tz_abbr_str)

# --- SIDEBAR CONFIGURATION ---
LOGO_URL = "https://raw.githubusercontent.com/VectorCheck/vector-check-intel/main/VCAG%20Inc%20-%20Logo%20Final.png"
try:
    st.sidebar.image(LOGO_URL, use_container_width=True)
except Exception:
    st.sidebar.title("Vector Check")
    st.sidebar.caption("Aerial Group Inc.")

st.sidebar.header("Mission Parameters")
# Input widgets natively bound to Session State keys
lat = st.sidebar.number_input("Latitude", format="%.4f", key="input_lat")
lon = st.sidebar.number_input("Longitude", format="%.4f", key="input_lon")

regional_name = get_location_name(lat, lon)
st.sidebar.markdown(f"<div style='color: #8E949E; font-size: 0.9rem; margin-top: -10px; margin-bottom: 20px;'>{regional_name}</div>", unsafe_allow_html=True)

station_data = get_nearest_icao_station(lat, lon)
icao = station_data["icao"]
stn_dist = station_data["dist"]
stn_dir = station_data["dir"]

st.sidebar.text_input("Nearest Valid ICAO (Auto-Locked)", value=(icao if icao != "NONE" else "N/A"), disabled=True)
if icao == "NONE":
    st.sidebar.markdown("<div style='font-size: 0.85rem; color: #8E949E; margin-bottom: 15px;'>No TAF-issuing station within 50km.</div>", unsafe_allow_html=True)

terrain_env = st.sidebar.selectbox("Terrain Environment:", options=["Land", "Water", "Mountains", "Urban"])
model_choice = st.sidebar.selectbox("Select Forecast Model:", options=["HRDPS (Canada 2.5km)", "ECMWF (Global 9km)"])

def log_refresh_callback():
    st.cache_data.clear()
    try:
        log_action(st.session_state.get("active_operator", "UNKNOWN"), lat, lon, icao, "MANUAL_REFRESH")
    except Exception: pass 

st.sidebar.button("Force Manual Data Refresh", on_click=log_refresh_callback)

model_api_map = {
    "HRDPS (Canada 2.5km)": "https://api.open-meteo.com/v1/gem",
    "ECMWF (Global 9km)": "https://api.open-meteo.com/v1/forecast" 
}

data = fetch_weather_payload(lat, lon, model_api_map[model_choice])

if icao != "NONE":
    metar_raw, taf_raw = fetch_metar_taf(icao)
else:
    metar_raw, taf_raw = "NIL", "NIL"

st.title("Atmospheric Risk Management")
st.caption(f"Vector Check Aerial Group Inc. - SYSTEM ACTIVE | OPERATOR: {st.session_state.get('active_operator', 'UNKNOWN')}")
st.divider()

if data is None:
    st.error("⚠️ CRITICAL: Atmospheric Data API Offline.")
    st.stop()
elif "error" in data:
    st.error(f"⚠️ CRITICAL API REJECTION: {data.get('message', 'Unknown Error')}")
    st.stop()
elif "hourly" not in data:
    st.error("⚠️ CRITICAL: Malformed data payload received from server.")
    st.stop()

# --- TIME PARSING ---
tf = TimezoneFinder()
tz_str = tf.timezone_at(lng=lon, lat=lat)
local_tz = pytz.timezone(tz_str) if tz_str else timezone.utc
tz_abbr = datetime.now(local_tz).tzname() if tz_str else "UTC"

h = data["hourly"]
is_kmh = "km/h" in data.get("hourly_units", {}).get("wind_speed_10m", "km/h").lower()
k_conv = 0.539957 if is_kmh else 1.0
raw_wind_unit = "KT"
sfc_elevation = data.get('elevation', 0) * 3.28084

times_display = []
for t in h["time"]:
    dt_u = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
    dt_l = dt_u.astimezone(local_tz)
    times_display.append(f"{dt_u.strftime('%d %b %H:%M')} Z | {dt_l.strftime('%H:%M')} {tz_abbr}")
    
now_utc = datetime.now(timezone.utc)
time_diffs = [abs((datetime.fromisoformat(t).replace(tzinfo=timezone.utc) - now_utc).total_seconds()) for t in h["time"]]
nearest_idx = time_diffs.index(min(time_diffs))
max_idx = min(len(h["time"]) - 1, nearest_idx + 48)
valid_times_display = times_display[nearest_idx : max_idx + 1]

if "forecast_slider" not in st.session_state or st.session_state.forecast_slider not in valid_times_display:
    st.session_state.forecast_slider = valid_times_display[0]

# ---------------------------------------------------------
# INTERACTIVE IMPACT MATRIX UI & LOGIC
# ---------------------------------------------------------
st.subheader("Impact Matrix")

with st.expander("Configure Operational Constraints"):
    tc1, tc2, tc3, tc4, tc5 = st.columns(5)
    # Constraints bound directly to Session State for persistence
    t_wind = tc1.number_input("Max Wind/Gust (KT)", key="input_wind")
    t_ceil = tc2.number_input("Min Ceiling (ft AGL)", step=100, key="input_ceil")
    t_vis = tc3.number_input("Min Vis (SM)", step=0.5, key="input_vis")
    
    # Secure string fallbacks for Selectboxes
    turb_idx = ["NIL", "LGT", "MOD", "SEV"].index(st.session_state['input_turb']) if st.session_state['input_turb'] in ["NIL", "LGT", "MOD", "SEV"] else 2
    ice_idx = ["NIL", "LGT", "MOD", "SEV"].index(st.session_state['input_ice']) if st.session_state['input_ice'] in ["NIL", "LGT", "MOD", "SEV"] else 0
    
    t_turb = tc4.selectbox("Max Turb", ["NIL", "LGT", "MOD", "SEV"], index=turb_idx, key="input_turb_widget")
    t_ice = tc5.selectbox("Max Icing", ["NIL", "LGT", "MOD", "SEV"], index=ice_idx, key="input_ice_widget")
    
    # Sync back the widgets to the master session state to maintain harmony
    st.session_state['input_turb'] = t_turb
    st.session_state['input_ice'] = t_ice

x_labels = []      
hover_texts = []   
color_vals = []    

for i in range(nearest_idx, max_idx + 1):
    failures = []
    
    w_raw = h.get('wind_speed_10m', [0])[i]
    w_spd = (float(w_raw) if w_raw is not None else 0.0) * k_conv
    g_raw_list = h.get('wind_gusts_10m')
    g_raw = (float(g_raw_list[i]) * k_conv) if (g_raw_list and len(g_raw_list) > i and g_raw_list[i] is not None) else w_spd
    gst = (w_spd * 1.25) if g_raw <= w_spd else g_raw
    wx_raw = h.get('weather_code', [0])
    wx = int(wx_raw[i]) if (wx_raw and len(wx_raw) > i and wx_raw[i] is not None) else 0
    
    t_temp_raw = h.get('temperature_2m', [0])[i]
    t_temp = float(t_temp_raw) if t_temp_raw is not None else 0.0
    rh_v = int(h.get('relative_humidity_2m', [0])[i]) if h.get('relative_humidity_2m', [0])[i] is not None else 0
    td = calc_td(t_temp, rh_v)
    sfc_spread = t_temp - td
    
    vis_raw_list = h.get('visibility')
    vis_raw_val = vis_raw_list[i] if vis_raw_list and len(vis_raw_list) > i else None
    vis_sm = calc_tactical_visibility(vis_raw_val, rh_v, w_spd, wx)
    
    profile = [{'h': sfc_elevation, 't': t_temp, 'td': td, 'spread': sfc_spread, 'rh': rh_v}]
    for p in ALL_P_LEVELS:
        gh_list = h.get(f'geopotential_height_{p}hPa')
        t_list = h.get(f'temperature_{p}hPa')
        rh_list = h.get(f'relative_humidity_{p}hPa')
        if gh_list and t_list and rh_list and len(gh_list) > i:
            if gh_list[i] is not None and t_list[i] is not None and rh_list[i] is not None:
                p_gh = float(gh_list[i]) * 3.28084
                if p_gh > profile[-1]['h']:
                    profile.append({'h': p_gh, 't': float(t_list[i]), 'td': calc_td(float(t_list[i]), int(rh_list[i])), 'spread': float(t_list[i]) - calc_td(float(t_list[i]), int(rh_list[i])), 'rh': int(rh_list[i])})

    t_950_list = h.get('temperature_925hPa')
    t_950 = float(t_950_list[i]) if (t_950_list and len(t_950_list) > i and t_950_list[i] is not None) else t_temp
    is_convective = (wx >= 80) or ((t_temp - t_950) >= 7.5)
    
    c_base_agl = 99999 
    c_amt = "CLR"
    
    if is_convective:
        c_base_agl = int(round(max(0, sfc_spread * CONVECTIVE_CCL_MULTIPLIER), -2))
        c_amt = "CONV"
    else:
        search_profile = profile[1:] if len(profile) > 1 else profile
        for layer in search_profile:
            h_agl = max(0, layer['h'] - sfc_elevation)
            if layer['spread'] <= 3.0: 
                if h_agl < 1000 and sfc_spread <= 3.0 and vis_sm >= 1.5 and wx < 50: continue
                c_base_agl = int(round(h_agl, -2))
                c_amt = "OVC" if layer['spread'] <= 1.0 else "BKN"
                break
        
        if c_amt == "CLR":
            for layer in search_profile:
                h_agl = max(0, layer['h'] - sfc_elevation)
                if layer['spread'] <= 5.0:
                    if h_agl < 1000 and sfc_spread <= 3.0 and vis_sm >= 1.5 and wx < 50: continue
                    c_base_agl = int(round(h_agl, -2))
                    c_amt = "SCT"
                    break
                    
        if c_amt == "CLR":
            for layer in search_profile:
                h_agl = max(0, layer['h'] - sfc_elevation)
                if layer['spread'] <= 7.0:
                    if h_agl < 1000 and sfc_spread <= 3.0 and vis_sm >= 1.5 and wx < 50: continue
                    c_base_agl = int(round(h_agl, -2))
                    c_amt = "FEW"
                    break

    alt_msl = sfc_elevation + 400
    alt_t, alt_rh = get_interp_thermals(alt_msl, profile)
    icing_cond = calculate_icing_profile(h, i, wx)
    
    u_v_list = h.get('wind_speed_1000hPa')
    if u_v_list and len(u_v_list) > i and u_v_list[i] is not None:
        u_v = float(u_v_list[i]) * k_conv
        u_h_list = h.get('geopotential_height_1000hPa')
        u_h = float(u_h_list[i]) if (u_h_list and len(u_h_list) > i and u_h_list[i] is not None) else 110.0
    else:
        u_v, u_h = w_spd, 10.0
        
    s_c = w_spd + (u_v - w_spd) * (math.log(max(1, 400*0.3048)/10) / math.log(max(1.1, u_h/10)))
    g_c = s_c + max(0, gst - w_spd)
    
    turb, ice = get_turb_ice(400, s_c, w_spd, g_c, wx, is_convective, icing_cond, alt_t, alt_rh, terrain_env, c_base_agl)

    max_wind_val = max(w_spd, gst)
    if max_wind_val > t_wind: failures.append(f"Wind ({int(max_wind_val)}KT)")
    if vis_sm < t_vis: failures.append(f"Vis ({vis_sm:.1f}SM)")
    if c_base_agl < t_ceil: failures.append(f"Ceil ({c_base_agl}ft)")
    if hazard_lvl(turb) > hazard_lvl(t_turb): failures.append(f"Turb ({turb})")
    if hazard_lvl(ice) > hazard_lvl(t_ice): failures.append(f"Ice ({ice})")
    
    dt_local = datetime.fromisoformat(h["time"][i]).replace(tzinfo=timezone.utc).astimezone(local_tz)
    time_str = dt_local.strftime('%H:%M')
    
    x_labels.append(f"T{i}") 
    
    if len(failures) == 0:
        color_vals.append("#1E8449") 
        hover_texts.append(f"{time_str} | FLIGHT AUTHORIZED")
    else:
        color_vals.append("#B82E2E") 
        hover_texts.append(f"{time_str} | " + ", ".join(failures))

tick_vals = x_labels[::4]
tick_texts = []

for val in tick_vals:
    idx_for_val = nearest_idx + x_labels.index(val)
    dt_local = datetime.fromisoformat(h["time"][idx_for_val]).replace(tzinfo=timezone.utc).astimezone(local_tz)
    t_str = dt_local.strftime('%H:%M')
    tick_texts.append(t_str)

fig = go.Figure(data=go.Bar(
    x=x_labels,
    y=[1] * len(x_labels),
    marker_color=color_vals,
    customdata=hover_texts, 
    hovertemplate="%{customdata}<extra></extra>", 
    width=1 
))

current_selected = st.session_state.forecast_slider
try:
    selected_idx = valid_times_display.index(current_selected)
    selected_x_label = x_labels[selected_idx]
except ValueError:
    selected_x_label = x_labels[0]

fig.add_trace(go.Scatter(
    x=[selected_x_label],
    y=[-0.15],
    mode="markers",
    marker=dict(symbol="line-ew", color="#E58E26", size=14, line=dict(width=4, color="#E58E26")),
    hoverinfo="skip"
))

fig.update_layout(
    height=65, 
    margin=dict(l=0, r=0, t=0, b=25),
    plot_bgcolor="#1B1E23",
    paper_bgcolor="#1B1E23",
    xaxis=dict(
        tickmode='array',
        tickvals=tick_vals,
        ticktext=tick_texts,
        tickangle=0,
        tickfont=dict(color="#A0A4AB", size=11, family="Source Sans Pro, sans-serif"),
        showgrid=False,
        zeroline=False,
        fixedrange=True
    ),
    yaxis=dict(showticklabels=False, showgrid=False, zeroline=False, range=[-0.25, 1], fixedrange=True),
    dragmode=False,
    showlegend=False
)

try:
    event = st.plotly_chart(fig, use_container_width=True, on_select="rerun", selection_mode="points", key="impact_matrix_chart", config={'displayModeBar': False})
    if event and "selection" in event and "points" in event["selection"] and len(event["selection"]["points"]) > 0:
        point_data = event["selection"]["points"][0]
        clicked_idx = point_data.get("point_index", point_data.get("pointIndex", None))
        
        if clicked_idx is not None:
            target_time = valid_times_display[clicked_idx]
            if st.session_state.get("forecast_slider") != target_time:
                st.session_state.forecast_slider = target_time
                st.rerun()
except Exception as e:
    pass

st.markdown("<div style='margin-bottom: 20px;'></div>", unsafe_allow_html=True)

# ---------------------------------------------------------
# FORECAST DASHBOARD EXECUTION (SINGLE HOUR)
# ---------------------------------------------------------

def update_time(offset):
    current_val = st.session_state.forecast_slider
    try:
        current_idx_in_valid = valid_times_display.index(current_val)
        new_idx_in_valid = max(0, min(len(valid_times_display) - 1, current_idx_in_valid + offset))
        st.session_state.forecast_slider = valid_times_display[new_idx_in_valid]
    except ValueError:
        st.session_state.forecast_slider = valid_times_display[0]

selected_time_str = st.sidebar.select_slider("Forecast Time:", options=valid_times_display, key="forecast_slider")
idx = times_display.index(selected_time_str)
relative_hr = valid_times_display.index(selected_time_str)

nav_col1, nav_col2, nav_col3 = st.sidebar.columns([1, 2, 1])
nav_col1.button("◄", on_click=update_time, args=(-1,), use_container_width=True)
nav_col2.markdown(f"<div style='text-align: center; font-size: 1.1rem; font-weight: bold; color: #E58E26; margin-top: 5px;'>+ {relative_hr} HR</div>", unsafe_allow_html=True)
nav_col3.button("►", on_click=update_time, args=(1,), use_container_width=True)

st.sidebar.divider()

t_temp_raw = h.get('temperature_2m', [0])[idx]
t_temp = float(t_temp_raw) if t_temp_raw is not None else 0.0

rh_raw = h.get('relative_humidity_2m', [0])[idx]
rh = int(rh_raw) if rh_raw is not None else 0

w_spd_raw = h.get('wind_speed_10m', [0])[idx]
w_spd = (float(w_spd_raw) if w_spd_raw is not None else 0.0) * k_conv

wx_list = h.get('weather_code', [0])
wx = int(wx_list[idx]) if (wx_list and len(wx_list) > idx and wx_list[idx] is not None) else 0

td = calc_td(t_temp, rh)
sfc_spread = t_temp - td

sfc_dir_raw = h.get('wind_direction_10m', [0])[idx]
sfc_dir = format_dir(float(sfc_dir_raw) if sfc_dir_raw is not None else 0.0, w_spd)

vis_raw_list = h.get('visibility')
vis_raw_val = vis_raw_list[idx] if vis_raw_list and len(vis_raw_list) > idx else None
vis_sm = calc_tactical_visibility(vis_raw_val, rh, w_spd, wx)

if vis_sm > 7: vis_disp = "> 7 SM"
else: vis_disp = f"{vis_sm:.1f} SM"

thermal_profile = [{'h': sfc_elevation, 't': t_temp, 'td': td, 'spread': sfc_spread, 'rh': rh}]
for p in ALL_P_LEVELS:
    gh_list = h.get(f'geopotential_height_{p}hPa')
    t_list = h.get(f'temperature_{p}hPa')
    rh_list = h.get(f'relative_humidity_{p}hPa')
    if gh_list and t_list and rh_list and len(gh_list) > idx:
        if gh_list[idx] is not None and t_list[idx] is not None and rh_list[idx] is not None:
            p_gh = float(gh_list[idx]) * 3.28084
            p_t = float(t_list[idx])
            p_rh = int(rh_list[idx])
            p_td = calc_td(p_t, p_rh)
            if p_gh > thermal_profile[-1]['h']:
                thermal_profile.append({'h': p_gh, 't': p_t, 'td': p_td, 'spread': p_t - p_td, 'rh': p_rh})

frz_raw_list = h.get('freezing_level_height')
if frz_raw_list and len(frz_raw_list) > idx and frz_raw_list[idx] is not None:
    frz_raw = float(frz_raw_list[idx])
    frz_disp = "SFC" if t_temp <= 0 else f"{int(round(frz_raw * 3.28, -2)):,} ft"
else:
    if t_temp <= 0: frz_disp = "SFC"
    else:
        frz_disp = "> 10,000 ft"
        for i in range(1, len(thermal_profile)):
            lower, upper = thermal_profile[i-1], thermal_profile[i]
            if upper['t'] <= 0:
                t_diff = lower['t'] - upper['t']
                frz_h = lower['h'] + (lower['t'] / t_diff) * (upper['h'] - lower['h']) if t_diff > 0 else lower['h']
                frz_disp = f"{int(round(frz_h, -2)):,} ft"
                break

t_950_list = h.get('temperature_925hPa')
t_950 = float(t_950_list[idx]) if (t_950_list and len(t_950_list) > idx and t_950_list[idx] is not None) else t_temp

lapse_rate_temp_drop = t_temp - t_950
is_convective = (wx >= 80) or lapse_rate_temp_drop >= 7.5

c_base_agl = 99999
c_amt = "CLR"
c_base_disp = "CLR"

if is_convective:
    c_base_agl = int(round(max(0, sfc_spread * CONVECTIVE_CCL_MULTIPLIER), -2))
    c_base_disp = f"{c_base_agl:,} ft CONV"
else:
    search_profile = thermal_profile[1:] if len(thermal_profile) > 1 else thermal_profile
    for layer in search_profile:
        h_agl = max(0, layer['h'] - sfc_elevation)
        if layer['spread'] <= 3.0: 
            if h_agl < 1000 and sfc_spread <= 3.0 and vis_sm >= 1.5 and wx < 50: continue
            c_amt = "OVC" if layer['spread'] <= 1.0 else "BKN"
            c_base_agl = int(round(h_agl, -2))
            c_base_disp = f"{c_base_agl:,} ft {c_amt}"
            break
            
    if c_amt == "CLR":
        for layer in search_profile:
            h_agl = max(0, layer['h'] - sfc_elevation)
            if layer['spread'] <= 5.0:
                if h_agl < 1000 and sfc_spread <= 3.0 and vis_sm >= 1.5 and wx < 50: continue
                c_base_agl = int(round(h_agl, -2))
                c_amt = "SCT"
                c_base_disp = f"{c_base_agl:,} ft SCT"
                break
                
    if c_amt == "CLR":
        for layer in search_profile:
            h_agl = max(0, layer['h'] - sfc_elevation)
            if layer['spread'] <= 7.0:
                if h_agl < 1000 and sfc_spread <= 3.0 and vis_sm >= 1.5 and wx < 50: continue
                c_base_agl = int(round(h_agl, -2))
                c_amt = "FEW"
                c_base_disp = f"{c_base_agl:,} ft FEW"
                break

raw_gst_list = h.get('wind_gusts_10m')
raw_gst = (float(raw_gst_list[idx]) * k_conv) if (raw_gst_list and len(raw_gst_list) > idx and raw_gst_list[idx] is not None) else w_spd
gst = (w_spd * 1.25) if raw_gst <= w_spd else raw_gst

u_v_list = h.get('wind_speed_1000hPa')
if u_v_list and len(u_v_list) > idx and u_v_list[idx] is not None:
    u_v = float(u_v_list[idx]) * k_conv
    u_dir = int(h.get('wind_direction_1000hPa', [0])[idx])
    u_h_list = h.get('geopotential_height_1000hPa')
    u_h = float(u_h_list[idx]) if (u_h_list and len(u_h_list) > idx and u_h_list[idx] is not None) else 110.0
else:
    u_v_list = h.get('wind_speed_925hPa')
    if u_v_list and len(u_v_list) > idx and u_v_list[idx] is not None:
        u_v = float(u_v_list[idx]) * k_conv
        u_dir = int(h.get('wind_direction_925hPa', [0])[idx])
        u_h_list = h.get('geopotential_height_925hPa')
        u_h = float(u_h_list[idx]) if (u_h_list and len(u_h_list) > idx and u_h_list[idx] is not None) else 760.0
    else:
        u_v, u_dir, u_h = w_spd, sfc_dir, 10.0
    
icing_cond = calculate_icing_profile(h, idx, wx)
dt_utc_exact_iso = h["time"][idx]
astro = fetch_astronomy_cached(lat, lon, dt_utc_exact_iso, tz_str, tz_abbr)
space_data = fetch_space_weather_cached(dt_utc_exact_iso)

sun_pos_display = f"{astro['sun_dir']} | Elev: {astro['sun_alt']}°" if astro['sun_alt'] > 0 else "NIL"
moon_pos_display = f"{astro['moon_dir']} | Elev: {astro['moon_alt']}°" if astro['moon_alt'] > 0 else "NIL"

weather_str = get_weather_element(wx, w_spd)

if int(w_spd) == 0: sfc_dir_disp, sfc_spd_disp = "CALM", "0"
elif int(w_spd) <= 3: sfc_dir_disp, sfc_spd_disp = "VRB", "3"
else: sfc_dir_disp, sfc_spd_disp = f"{sfc_dir:03d}°", str(int(w_spd))

# --- UI RENDERING STARTS HERE ---

st.subheader("Forecasted Surface Data")
c = st.columns(8)
c[0].metric("Temp", f"{t_temp}°C")
c[1].metric("RH", f"{rh}%")
c[2].metric("Wind Dir", sfc_dir_disp)
c[3].metric(f"Wind Spd", f"{sfc_spd_disp} {raw_wind_unit}")
c[4].metric("Weather", weather_str)
c[5].metric("Visibility", vis_disp)
c[6].metric("Freezing LVL", frz_disp)
c[7].metric("Cloud Base", c_base_disp)

st.divider()

st.subheader(f"Tactical Hazard Stack (0-400ft AGL)")
stack_tactical = []

gust_delta = max(0, gst - w_spd)

for alt in [400, 300, 200, 100]:
    s_c = w_spd + (u_v - w_spd) * (math.log(max(1, alt*0.3048)/10) / math.log(max(1.1, u_h/10)))
    g_c = s_c + gust_delta
    
    d_c_raw = (sfc_dir + ((u_dir - sfc_dir + 180) % 360 - 180) * (min(alt*0.3048, u_h) / max(0.1, u_h))) % 360
    d_c = format_dir(d_c_raw, s_c)
    
    alt_msl = sfc_elevation + alt
    alt_t, alt_rh = get_interp_thermals(alt_msl, thermal_profile)
    
    turb, ice = get_turb_ice(alt, s_c, w_spd, g_c, wx, is_convective, icing_cond, alt_t, alt_rh, terrain_env, c_base_agl)
    
    if int(s_c) == 0: mat_dir, mat_spd = "CALM", "0"
    elif int(s_c) <= 3: mat_dir, mat_spd = "VRB", "3"
    else: mat_dir, mat_spd = f"{d_c:03d}°", str(int(s_c))

    stack_tactical.append({"Alt (AGL)": f"{alt}ft", "Dir": mat_dir, f"Spd ({raw_wind_unit})": mat_spd, f"Gust ({raw_wind_unit})": str(int(g_c)), "Turbulence": turb, "Icing": ice})

df_tactical = pd.DataFrame(stack_tactical).set_index("Alt (AGL)")
st.table(df_tactical)

st.subheader("Extended Trajectory (1,000-5,000ft AGL)")
p_levels_traj = [1000, 925, 850, 700]

p_profile = []
for p in p_levels_traj:
    ws_list, wd_list, gh_list = h.get(f'wind_speed_{p}hPa'), h.get(f'wind_direction_{p}hPa'), h.get(f'geopotential_height_{p}hPa')
    if ws_list and wd_list and gh_list and len(ws_list) > idx:
        ws, wd, gh = ws_list[idx], wd_list[idx], gh_list[idx]
        if ws is not None and wd is not None and gh is not None:
            p_profile.append({'h': float(gh) * 3.28, 's': float(ws) * k_conv, 'd': int(wd)})

p_profile = sorted(p_profile, key=lambda x: x['h'])
stack_ext = []

if not p_profile:
    for alt in [5000, 4000, 3000, 2000, 1000]:
        stack_ext.append({"Alt (AGL)": f"{alt}ft", "Dir": "N/A", f"Spd ({raw_wind_unit})": "N/A", f"Gust ({raw_wind_unit})": "N/A", "Turbulence": "N/A", "Icing": "N/A"})
else:
    for alt in [5000, 4000, 3000, 2000, 1000]:
        pts = [{'h': u_h*3.28, 's': u_v, 'd': u_dir}] + p_profile
        blw, abv = pts[0], pts[-1]
        for i in range(len(pts)-1):
            if pts[i]['h'] <= alt <= pts[i+1]['h']:
                blw, abv = pts[i], pts[i+1]; break
        
        frac = (alt - blw['h']) / (abv['h'] - blw['h']) if abv['h'] != blw['h'] else 0
        s_e = blw['s'] + frac * (abv['s'] - blw['s'])
        d_e_raw = (blw['d'] + ((abv['d'] - blw['d'] + 180) % 360 - 180) * frac) % 360
        d_e = format_dir(d_e_raw, s_e)
        g_e = s_e + gust_delta
        
        alt_msl = sfc_elevation + alt
        alt_t, alt_rh = get_interp_thermals(alt_msl, thermal_profile)
        turb, ice = get_turb_ice(alt, s_e, w_spd, g_e, wx, is_convective, icing_cond, alt_t, alt_rh, terrain_env, c_base_agl)
        
        if int(s_e) == 0: mat_dir_ext, mat_spd_ext = "CALM", "0"
        elif int(s_e) <= 3: mat_dir_ext, mat_spd_ext = "VRB", "3"
        else: mat_dir_ext, mat_spd_ext = f"{d_e:03d}°", str(int(s_e))

        stack_ext.append({"Alt (AGL)": f"{alt}ft", "Dir": mat_dir_ext, f"Spd ({raw_wind_unit})": mat_spd_ext, f"Gust ({raw_wind_unit})": str(int(g_e)), "Turbulence": turb, "Icing": ice})

df_ext = pd.DataFrame(stack_ext).set_index("Alt (AGL)")
st.table(df_ext)

st.divider()

st.subheader(f"Light Profile ({astro['tz']})")
ac1, ac2, ac3, ac4, ac5 = st.columns(5)
ac1.metric("Dawn (Civil)", astro['dawn'])
ac2.metric("Sunrise", astro['sunrise'])
ac3.metric("Sunset", astro['sunset'])
ac4.metric("Dusk (Civil)", astro['dusk'])
ac5.metric("Sun Pos", sun_pos_display)

mc1, mc2, mc3, mc4, mc5 = st.columns(5)
mc1.metric("Moonrise", astro['moonrise'])
mc2.metric("Moonset", astro['moonset'])
mc3.metric("Illumination", f"{astro['moon_ill']}%")
mc4.metric("Moon Pos", moon_pos_display)
mc5.empty()

st.divider()
st.subheader("Space Weather (GNSS & C2 Link)")

risk_color = "#ff4b4b" if space_data['risk'] in ["HIGH (G1)", "SEVERE (G2+)"] else "#D1D5DB"
space_wx_html = f"""
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
"""
st.markdown(space_wx_html, unsafe_allow_html=True)

st.divider()

if icao == "NONE":
    st.subheader("Station Actuals")
    st.markdown('<div class="obs-text">No METAR/TAF information within a 50km radius.</div>', unsafe_allow_html=True)
    clean_metar = "NIL"
    clean_taf = "NIL"
else:
    clean_metar = re.sub('<[^<]+>', '', metar_raw)
    
    raw_taf_no_html = re.sub('<[^<]+>', '', taf_raw)
    raw_taf_no_html = raw_taf_no_html.replace(" RMK ", "\nRMK ")
    taf_lines = [line.strip() for line in raw_taf_no_html.split('\n') if line.strip()]
    
    ui_taf_lines = []
    csv_taf_lines = []
    
    for i, line in enumerate(taf_lines):
        if i == 0 or i == len(taf_lines) - 1 or line.startswith("FM") or line.startswith("RMK"):
            ui_taf_lines.append(line)
            csv_taf_lines.append(line)
        else:
            ui_taf_lines.append("&nbsp;&nbsp;&nbsp;&nbsp;" + line)
            csv_taf_lines.append("    " + line)
            
    ui_rebuilt_taf = '\n'.join(ui_taf_lines)
    clean_taf = '\n'.join(csv_taf_lines)
    
    metar_disp = apply_tactical_highlights(clean_metar)
    taf_disp = apply_tactical_highlights(ui_rebuilt_taf)
    taf_disp = taf_disp.replace('\n', '<br>')
    
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

# --- ENTERPRISE PDF EXPORT ENGINE ---
def generate_pdf_report():
    stn_display_str = f"{icao} | {stn_dist:.1f} km {stn_dir} of AO" if icao != "NONE" else "No valid ICAO within 50km."
    
    pdf = FPDF()
    pdf.add_page()
    
    def safe_txt(txt):
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
    pdf.cell(0, 6, safe_txt(f"{model_choice} | {selected_time_str}"), border=0, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    pdf.set_font("helvetica", "B", 12)
    pdf.cell(0, 8, "FORECASTED SURFACE CONDITIONS", border=0, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 10)
    pdf.multi_cell(0, 6, safe_txt(f"Temperature: {t_temp}C | RH: {rh}% | Dewpoint: {td:.1f}C\nWind: {sfc_dir_disp} @ {sfc_spd_disp} {raw_wind_unit} (Gusts: {int(gst)} {raw_wind_unit})\nWeather: {weather_str} | Visibility: {vis_disp}\nCloud Base: {c_base_disp} | Freezing Level: {frz_disp}"))
    pdf.ln(5)

    pdf.set_font("helvetica", "B", 12)
    pdf.cell(0, 8, "ASTRONOMICAL & SPACE WEATHER", border=0, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 10)
    pdf.multi_cell(0, 6, safe_txt(f"Sun ({astro['tz']}): Rise {astro['sunrise']} | Set {astro['sunset']} | Civil Dawn {astro['dawn']} | Civil Dusk {astro['dusk']}\nMoon ({astro['tz']}): Rise {astro['moonrise']} | Set {astro['moonset']} | Illum {astro['moon_ill']}%\nSpace Weather: Kp Index {space_data['kp']} | GNSS Risk: {space_data['risk']}"))
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

    def draw_table(title, df):
        pdf.set_font("helvetica", "B", 12)
        pdf.cell(0, 8, title, border=0, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("helvetica", "B", 9)
        
        col_names = ["Alt (AGL)"] + list(df.columns)
        col_widths = [25, 20, 25, 25, 30, 25] 
        
        for i, col in enumerate(col_names):
            pdf.cell(col_widths[i], 8, safe_txt(str(col)), border=1, align='C')
        pdf.ln(8)
        
        pdf.set_font("helvetica", "", 9)
        for idx_val, row in df.iterrows():
            pdf.cell(col_widths[0], 8, safe_txt(str(idx_val)), border=1, align='C')
            for i, val in enumerate(row):
                pdf.cell(col_widths[i+1], 8, safe_txt(str(val)), border=1, align='C')
            pdf.ln(8)
        pdf.ln(5)

    draw_table("TACTICAL HAZARD STACK (0-400ft AGL)", df_tactical)
    draw_table("EXTENDED TRAJECTORY (1,000-5,000ft AGL)", df_ext)
    
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp_name = tmp.name
    
    try:
        pdf.output(tmp_name)
        with open(tmp_name, "rb") as f:
            pdf_bytes = f.read()
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
            
    return pdf_bytes

def log_download_callback():
    try:
        log_action(st.session_state.get("active_operator", "UNKNOWN"), lat, lon, icao, "DOWNLOAD_PDF")
    except Exception:
        pass 

st.download_button(
    label="Download Flight Briefing (PDF)",
    data=generate_pdf_report(),
    file_name=f"VCAG_Briefing_{lat}_{lon}_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
    mime="application/pdf",
    on_click=log_download_callback
)

st.divider()
st.subheader("Vertical Atmospheric Profile (Convective Ops)")
fig = plot_convective_profile(h, idx, t_temp, td, w_spd, sfc_dir, sfc_elevation)

if fig: st.pyplot(fig)
else: st.warning("Insufficient atmospheric layers available to render vertical profile.")

st.divider()
st.markdown("""
<div style="text-align: center; color: #8E949E; font-size: 0.85rem; padding: 20px;">
<strong>⚠️ FOR SITUATIONAL AWARENESS ONLY</strong><br>
This system translates raw meteorological model data for uncrewed systems. It does not replace official NAV CANADA or NOAA flight service briefings. The Pilot in Command (PIC) retains ultimate authority and responsibility for flight safety. Vector Check Aerial Group Inc. assumes no liability for operational decisions made using this tool. <br><br>
<em>Usage of this system, including geographic querying and PDF generation, is actively logged to a secure database for audit and security purposes.</em>
</div>
""", unsafe_allow_html=True)

# AUTO-SAVE STATE PERSISTENCE ENGINE
try:
    save_prefs(
        st.session_state.get("active_operator", "UNKNOWN"),
        lat, lon, t_wind, t_ceil, t_vis, t_turb, t_ice
    )
except Exception:
    pass
