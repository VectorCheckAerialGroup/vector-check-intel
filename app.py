import streamlit as st
import pandas as pd
import math
import re
import json
import urllib.request
from datetime import datetime, timezone
from timezonefinder import TimezoneFinder
import pytz

# Import Vector Check Modules
from modules.data_ingest import get_aviation_weather, fetch_mission_data
from modules.hazard_logic import get_weather_element, calculate_icing_profile, get_turb_ice, apply_tactical_highlights
from modules.visualizations import plot_convective_profile
from modules.telemetry import log_action
from modules.astronomy import get_astronomical_data
from modules.space_weather import get_kp_index

# 1. PAGE CONFIG & CSS
st.set_page_config(page_title="Vector Check: Atmospheric Risk Management", layout="wide")
st.markdown("""
    <style>
    [data-testid="stMetricValue"] { font-size: 1.2rem !important; color: #E58E26 !important; }
    [data-testid="stMetricLabel"] { font-size: 0.8rem !important; color: #A0A4AB !important; text-transform: uppercase; }
    .ifr-text { color: #ff4b4b; font-weight: bold; }
    .mvfr-text { color: #f6ec15; font-weight: bold; }
    .fz-warn { background-color: #ff4b4b; color: white; padding: 2px; border-radius: 3px; font-weight: bold; }
    table { margin-left: auto; margin-right: auto; text-align: center !important; width: 90%; border-collapse: collapse; background-color: #1B1E23; }
    th { text-align: center !important; color: #8E949E !important; font-weight: bold !important; padding: 10px !important; border-bottom: 2px solid #3E444E !important; text-transform: uppercase; }
    td { text-align: center !important; padding: 8px !important; color: #D1D5DB !important; border-bottom: 1px solid #2D3139 !important; }
    .obs-text { font-family: "Source Sans Pro", sans-serif; font-size: 0.95rem; line-height: 1.6; color: #D1D5DB; }
    div[data-testid="column"] button { width: 100%; padding: 0px; font-size: 0.8rem; }
    </style>
    """, unsafe_allow_html=True)

# 2. AUTHENTICATION GATEWAY
def check_password():
    def password_entered():
        user = st.session_state["username"]
        pwd = st.session_state["password"]
        
        if user in st.secrets.get("passwords", {}) and pwd == st.secrets["passwords"][user]:
            st.session_state["password_correct"] = True
            st.session_state["active_operator"] = user
            log_action(user, 0.0, 0.0, "SYS", "AUTHENTICATION_SUCCESS")
            del st.session_state["password"]  
            del st.session_state["username"]
        else:
            st.session_state["password_correct"] = False

    if "password_correct" not in st.session_state:
        st.title("Vector Check Aerial Group Inc.")
        st.caption("Atmospheric Risk Management System - Restricted Access")
        st.text_input("Operator ID", key="username")
        st.text_input("Passcode", type="password", key="password")
        st.button("Authenticate", on_click=password_entered)
        return False
        
    elif not st.session_state["password_correct"]:
        st.title("Vector Check Aerial Group Inc.")
        st.caption("Atmospheric Risk Management System - Restricted Access")
        st.text_input("Operator ID", key="username")
        st.text_input("Passcode", type="password", key="password")
        st.button("Authenticate", on_click=password_entered)
        st.error("⚠️ UNAUTHORIZED: Invalid Operator ID or Passcode.")
        return False
    else:
        return True

if not check_password():
    st.stop()

# ---------------------------------------------------------
# SPATIAL ENGINES
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
        elif province != 'Unknown':
            return province
        else:
            return "Unknown Location"
    except Exception:
        return "Location Data Unavailable"

@st.cache_data(ttl=3600)
def get_nearest_icao_station(user_lat, user_lon):
    try:
        min_lat = user_lat - 1.0
        max_lat = user_lat + 1.0
        min_lon = user_lon - 1.0
        max_lon = user_lon + 1.0
        
        url = f"https://aviationweather.gov/api/data/taf?bbox={min_lat},{min_lon},{max_lat},{max_lon}&format=json"
        req = urllib.request.Request(url, headers={'User-Agent': 'VectorCheck-App/2.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
        
        best_station = {"icao": "NONE", "dist": float('inf'), "dir": ""}
        seen_icaos = set()
        
        for taf in data:
            if 'icaoId' not in taf or 'lat' not in taf or 'lon' not in taf:
                continue
                
            icao_code = taf['icaoId']
            if icao_code in seen_icaos:
                continue
            seen_icaos.add(icao_code)
            
            stn_lat = float(taf['lat'])
            stn_lon = float(taf['lon'])
            
            R = 6371.0 
            lat1, lon1 = math.radians(user_lat), math.radians(user_lon)
            lat2, lon2 = math.radians(stn_lat), math.radians(stn_lon)
            
            dlat = lat2 - lat1
            dlon = lon2 - lon1
            
            a = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
            c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
            dist = R * c
            
            if dist <= 50.0 and dist < best_station["dist"]:
                y = math.sin(dlon) * math.cos(lat2)
                x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
                bearing = (math.degrees(math.atan2(y, x)) + 360) % 360
                dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
                best_station = {
                    "icao": icao_code,
                    "dist": dist,
                    "dir": dirs[int(round(bearing / 45)) % 8]
                }
        
        if best_station["icao"] != "NONE":
            return best_station
            
    except Exception as e:
        pass
    
    return {"icao": "NONE", "dist": None, "dir": ""}

# ---------------------------------------------------------
# MAIN DASHBOARD EXECUTION
# ---------------------------------------------------------

LOGO_URL = "https://raw.githubusercontent.com/VectorCheck/vector-check-intel/main/VCAG%20Inc%20-%20Logo%20Final.png"
try:
    st.sidebar.image(LOGO_URL, use_container_width=True)
except Exception:
    st.sidebar.title("Vector Check")
    st.sidebar.caption("Aerial Group Inc.")

st.sidebar.header("Mission Parameters")
lat = st.sidebar.number_input("Latitude", value=44.1628, format="%.4f", key="lat_input")
lon = st.sidebar.number_input("Longitude", value=-77.3832, format="%.4f", key="lon_input")

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
    log_action(
        st.session_state.get("active_operator", "UNKNOWN"), 
        st.session_state.get("lat_input", 44.1628), 
        st.session_state.get("lon_input", -77.3832), 
        st.session_state.get("icao_input", icao), 
        "MANUAL_REFRESH"
    )

st.sidebar.button("Force Manual Data Refresh", on_click=log_refresh_callback)

model_api_map = {
    "HRDPS (Canada 2.5km)": "https://api.open-meteo.com/v1/gem",
    "ECMWF (Global 9km)": "https://api.open-meteo.com/v1/forecast" 
}

data = fetch_mission_data(lat, lon, model_api_map[model_choice])

if icao != "NONE":
    metar_raw, taf_raw = get_aviation_weather(icao)
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
    st.markdown("**Diagnostic URL (For Dev review only):**")
    st.code(data.get('url', 'URL Unavailable'))
    st.stop()
elif "hourly" not in data:
    st.error("⚠️ CRITICAL: Malformed data payload received from server.")
    st.stop()

tf = TimezoneFinder()
tz_str = tf.timezone_at(lng=lon, lat=lat)
local_tz = pytz.timezone(tz_str) if tz_str else timezone.utc
tz_abbr = datetime.now(local_tz).tzname() if tz_str else "UTC"

h = data["hourly"]

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

is_kmh = "km/h" in data.get("hourly_units", {}).get("wind_speed_10m", "km/h").lower()
k_conv = 0.539957 if is_kmh else 1.0
raw_wind_unit = "KT"

def format_dir(d, spd):
    r = int(round(float(d), -1)) % 360
    if r == 0 and spd > 0: return 360
    if spd == 0: return 0
    return r

t_temp_raw = h.get('temperature_2m', [0])[idx]
t_temp = float(t_temp_raw) if t_temp_raw is not None else 0.0

rh_raw = h.get('relative_humidity_2m', [0])[idx]
rh = int(rh_raw) if rh_raw is not None else 0

w_spd_raw = h.get('wind_speed_10m', [0])[idx]
w_spd = (float(w_spd_raw) if w_spd_raw is not None else 0.0) * k_conv

wx_list = h.get('weather_code', [0])
wx = int(wx_list[idx]) if (wx_list and len(wx_list) > idx and wx_list[idx] is not None) else 0

if t_temp_raw is not None and rh_raw is not None and rh > 0:
    a = 17.625
    b = 243.04
    alpha = math.log(rh / 100.0) + ((a * t_temp) / (b + t_temp))
    td = (b * alpha) / (a - alpha)
    raw_base = max(0, (t_temp - td) * 400)
    c_base = int(round(raw_base, -2)) 
else:
    td = t_temp
    c_base = 10000

sfc_dir_raw = h.get('wind_direction_10m', [0])[idx]
sfc_dir = format_dir(float(sfc_dir_raw) if sfc_dir_raw is not None else 0.0, w_spd)

thermal_profile = [
    {'h': data.get('elevation', 0) * 3.28084, 't': t_temp}
]

for p in [1000, 925, 850, 700]:
    gh_list = h.get(f'geopotential_height_{p}hPa')
    t_list = h.get(f'temperature_{p}hPa')
    if gh_list and t_list and len(gh_list) > idx and len(t_list) > idx and gh_list[idx] is not None and t_list[idx] is not None:
        gh_ft = float(gh_list[idx]) * 3.28084
        if gh_ft > thermal_profile[-1]['h']:
            thermal_profile.append({'h': gh_ft, 't': float(t_list[idx])})

frz_raw_list = h.get('freezing_level_height')
if frz_raw_list and len(frz_raw_list) > idx and frz_raw_list[idx] is not None:
    frz_raw = float(frz_raw_list[idx])
    frz_disp = "SFC" if t_temp <= 0 else f"{int(round(frz_raw * 3.28, -2)):,} ft"
else:
    if t_temp <= 0:
        frz_disp = "SFC"
    else:
        frz_disp = ">10,000 ft"
        for i in range(1, len(thermal_profile)):
            lower = thermal_profile[i-1]
            upper = thermal_profile[i]
            
            if upper['t'] <= 0:
                t_diff = lower['t'] - upper['t']
                if t_diff > 0:
                    ratio = lower['t'] / t_diff
                    frz_h = lower['h'] + ratio * (upper['h'] - lower['h'])
                else:
                    frz_h = lower['h']
                frz_disp = f"{int(round(frz_h, -2)):,} ft"
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
        u_v = w_spd
        u_dir = sfc_dir
        u_h = 10.0
    
icing_cond = calculate_icing_profile(h, idx, wx)

t_950_list = h.get('temperature_925hPa')
t_950 = float(t_950_list[idx]) if (t_950_list and len(t_950_list) > idx and t_950_list[idx] is not None) else t_temp
is_stable = t_950 > (t_temp - 2.0)

dt_utc_exact = datetime.fromisoformat(h["time"][idx]).replace(tzinfo=timezone.utc)
astro = get_astronomical_data(lat, lon, dt_utc_exact, local_tz, tz_abbr)
space_data = get_kp_index(dt_utc_exact)

sun_pos_display = f"{astro['sun_dir']} | Elev: {astro['sun_alt']}°" if astro['sun_alt'] > 0 else "NIL (Below Horizon)"
moon_pos_display = f"{astro['moon_dir']} | Elev: {astro['moon_alt']}°" if astro['moon_alt'] > 0 else "NIL (Below Horizon)"

weather_str = get_weather_element(wx, w_spd)

if int(w_spd) == 0:
    sfc_dir_disp = "CALM"
    sfc_spd_disp = "0"
elif int(w_spd) <= 3:
    sfc_dir_disp = "VRB"
    sfc_spd_disp = "3"
else:
    sfc_dir_disp = f"{sfc_dir:03d}°"
    sfc_spd_disp = str(int(w_spd))

# --- UI RENDERING STARTS HERE ---

st.subheader("Forecasted Surface Data")
c = st.columns(8)
c[0].metric("Temp", f"{t_temp}°C")
c[1].metric("RH", f"{rh}%")
c[2].metric("Wind Dir", sfc_dir_disp)
c[3].metric(f"Wind Spd", f"{sfc_spd_disp} {raw_wind_unit}")
c[4].metric("Weather", weather_str)
c[5].metric("Vis (Est)", f"{int((100-rh)/5 * 1.13)} sm")
c[6].metric("Freezing LVL", frz_disp)
c[7].metric("Cloud Base", f"{c_base} ft")

st.divider()

st.subheader(f"Tactical Hazard Stack (0-400ft AGL)")
stack_tactical = []

gust_delta = max(0, gst - w_spd)

for alt in [400, 300, 200, 100]:
    s_c = w_spd + (u_v - w_spd) * (math.log(max(1, alt*0.3048)/10) / math.log(max(1.1, u_h/10)))
    g_c = s_c + gust_delta
    
    d_c_raw = (sfc_dir + ((u_dir - sfc_dir + 180) % 360 - 180) * (min(alt*0.3048, u_h) / max(0.1, u_h))) % 360
    d_c = format_dir(d_c_raw, s_c)
    
    turb, ice = get_turb_ice(alt, s_c, w_spd, g_c, wx, is_stable, icing_cond, t_temp, rh, terrain_env)
    
    if int(s_c) == 0:
        mat_dir, mat_spd = "CALM", "0"
    elif int(s_c) <= 3:
        mat_dir, mat_spd = "VRB", "3"
    else:
        mat_dir, mat_spd = f"{d_c:03d}°", str(int(s_c))

    stack_tactical.append({
        "Alt (AGL)": f"{alt}ft", 
        "Dir": mat_dir, 
        f"Spd ({raw_wind_unit})": mat_spd, 
        f"Gust ({raw_wind_unit})": str(int(g_c)), 
        "Turbulence": turb, 
        "Icing": ice
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
    
    if ws_list and wd_list and gh_list and len(ws_list) > idx:
        ws = ws_list[idx]
        wd = wd_list[idx]
        gh = gh_list[idx]
        
        if ws is not None and wd is not None and gh is not None:
            p_profile.append({
                'h': float(gh) * 3.28,
                's': float(ws) * k_conv,
                'd': int(wd)
            })

p_profile = sorted(p_profile, key=lambda x: x['h'])
stack_ext = []

if not p_profile:
    for alt in [5000, 4000, 3000, 2000, 1000]:
        stack_ext.append({
            "Alt (AGL)": f"{alt}ft", 
            "Dir": "N/A", 
            f"Spd ({raw_wind_unit})": "N/A", 
            "Turbulence": "N/A", 
            "Icing": "N/A"
        })
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
        turb, ice = get_turb_ice(alt, s_e, w_spd, g_e, wx, is_stable, icing_cond, t_temp, rh, terrain_env)
        
        if int(s_e) == 0:
            mat_dir_ext, mat_spd_ext = "CALM", "0"
        elif int(s_e) <= 3:
            mat_dir_ext, mat_spd_ext = "VRB", "3"
        else:
            mat_dir_ext, mat_spd_ext = f"{d_e:03d}°", str(int(s_e))

        stack_ext.append({
            "Alt (AGL)": f"{alt}ft", 
            "Dir": mat_dir_ext, 
            f"Spd ({raw_wind_unit})": mat_spd_ext, 
            "Turbulence": turb, 
            "Icing": ice
        })

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
    taf_lines = [line.strip() for line in raw_taf_no_html.split('\n') if line.strip()]
    
    ui_taf_lines = []
    csv_taf_lines = []
    
    for i, line in enumerate(taf_lines):
        if i == 0 or i == len(taf_lines) - 1 or line.startswith("FM"):
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

df_export = pd.concat([df_tactical, df_ext])
stn_display_str = f"{icao} | {stn_dist:.1f} km {stn_dir} of AO" if icao != "NONE" else "No METAR/TAF information within a 50km radius."

csv_header = (
    "VECTOR CHECK AERIAL GROUP INC. - Atmospheric Risk Assessment\n"
    f"Target Coordinates: {lat}, {lon}\n"
    f"Regional Area: {regional_name}\n"
    f"Automated Weather Station: {stn_display_str}\n"
    f"Forecast Model: {model_choice} | Valid Time: {selected_time_str}\n"
    f"Terrain Environment: {terrain_env}\n"
    f"Wind Unit Standard: {raw_wind_unit}\n\n" 
    "--- FORECASTED SURFACE CONDITIONS ---\n"
    f"Temperature: {t_temp}C | RH: {rh}% | Dewpoint: {td:.1f}C\n"
    f"Wind: {sfc_dir_disp} @ {sfc_spd_disp} {raw_wind_unit} (Gusts: {int(gst)} {raw_wind_unit})\n"
    f"Weather: {weather_str}\n"
    f"Visibility (Est): {int((100-rh)/5 * 1.13)} sm\n"
    f"Cloud Base: {c_base} ft | Freezing Level: {frz_disp}\n\n"
    "--- ASTRONOMICAL & SPACE WEATHER ---\n"
    f"Sun ({astro['tz']}): Rise {astro['sunrise']} | Set {astro['sunset']} | Civil Dawn {astro['dawn']} | Civil Dusk {astro['dusk']}\n"
    f"Moon ({astro['tz']}): Rise {astro['moonrise']} | Set {astro['moonset']} | Illum {astro['moon_ill']}%\n"
    f"Space Weather: Kp Index {space_data['kp']} | GNSS Risk: {space_data['risk']}\n"
    f"Position: Sun {sun_pos_display} | Moon {moon_pos_display}\n\n"
    f"--- METAR/SPECI ---\n{clean_metar}\n\n"
    f"--- TAF ---\n{clean_taf}\n\n"
    "--- HAZARD STACK (AGL) ---\n"
)

csv_data = (csv_header + df_export.to_csv()).encode('utf-8')

def log_download_callback():
    log_action(
        st.session_state.get("active_operator", "UNKNOWN"), 
        st.session_state.get("lat_input", 44.1628), 
        st.session_state.get("lon_input", -77.3832), 
        st.session_state.get("icao_input", icao), 
        "DOWNLOAD_CSV"
    )

st.download_button(
    label="Download Actuals and Forecast data (CSV)",
    data=csv_data,
    file_name=f"VCAG_Hazard_Matrix_{lat}_{lon}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
    mime="text/csv",
    on_click=log_download_callback
)

st.divider()
st.subheader("Vertical Atmospheric Profile (Convective Ops)")
sfc_h = data.get('elevation', 0) * 3.28084
fig = plot_convective_profile(h, idx, t_temp, td, w_spd, sfc_dir, sfc_h)

if fig: st.pyplot(fig)
else: st.warning("Insufficient atmospheric layers available to render vertical profile.")

# 7. LIABILITY & TRACKING ARMOR
st.divider()
st.markdown("""
<div style="text-align: center; color: #8E949E; font-size: 0.85rem; padding: 20px;">
<strong>⚠️ FOR SITUATIONAL AWARENESS ONLY</strong><br>
This system translates raw meteorological model data for uncrewed systems. It does not replace official NAV CANADA or NOAA flight service briefings. The Pilot in Command (PIC) retains ultimate authority and responsibility for flight safety. Vector Check Aerial Group Inc. assumes no liability for operational decisions made using this tool. <br><br>
<em>Usage of this system, including geographic querying and CSV generation, is actively logged to a secure database for audit and security purposes.</em>
</div>
""", unsafe_allow_html=True)
