import streamlit as st
import pandas as pd
import math
import re
import urllib.request
import xml.etree.ElementTree as ET
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

# 2. AUTHENTICATION GATEWAY WITH TELEMETRY
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
# SPATIAL ENGINE: AUTO-LOCATE NEAREST TAF STATION
# ---------------------------------------------------------
@st.cache_data(ttl=3600)
def get_nearest_icao_station(user_lat, user_lon):
    """Spatially queries the AWC API for active TAFs within 50km to bypass Canadian metadata bugs."""
    try:
        # Directly querying the 'tafs' endpoint ensures the station physically issues TAFs
        url = f"https://aviationweather.gov/api/data/dataserver?requestType=retrieve&dataSource=tafs&radialDistance=40;{user_lat},{user_lon}&hoursBeforeNow=4&format=xml"
        req = urllib.request.Request(url, headers={'User-Agent': 'VectorCheck-App/1.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            xml_data = response.read()
        
        root = ET.fromstring(xml_data)
        tafs = root.findall('.//TAF')
        
        best_station = {"icao": "NONE", "dist": float('inf')}
        seen_icaos = set()
        
        for taf in tafs:
            icao_code = taf.find('station_id').text
            if icao_code in seen_icaos:
                continue
            seen_icaos.add(icao_code)
            
            stn_lat = float(taf.find('latitude').text)
            stn_lon = float(taf.find('longitude').text)
            
            # Spherical Haversine calculation for exact kilometers
            R = 6371.0 
            lat1, lon1 = math.radians(user_lat), math.radians(user_lon)
            lat2, lon2 = math.radians(stn_lat), math.radians(stn_lon)
            
            dlat = lat2 - lat1
            dlon = lon2 - lon1
            
            a = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
            c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
            dist = R * c
            
            # STRICT DOCTRINE LIMIT: Must be <= 50.0 km
            if dist <= 50.0 and dist < best_station["dist"]:
                best_station = {
                    "icao": icao_code,
                    "dist": dist
                }
        
        if best_station["icao"] != "NONE":
            return best_station
    except Exception as e:
        pass
    
    # Graceful fallback if nothing is within 50km or API fails
    return {"icao": "NONE", "dist": None}

# ---------------------------------------------------------
# MAIN DASHBOARD EXECUTION
# ---------------------------------------------------------

LOGO_URL = "https://raw.githubusercontent.com/VectorCheck/vector-check-intel/main/VCAG%20Inc%20-%20Logo%20Final.png"
try:
    st.sidebar.image(LOGO_URL, use_container_width=True)
except Exception:
    st.sidebar.title("Vector Check")
    st.sidebar.caption("Aerial Group Inc.")

# 3. SIDEBAR PARAMETERS 
st.sidebar.header("Mission Parameters")
lat = st.sidebar.number_input("Latitude", value=44.1628, format="%.4f", key="lat_input")
lon = st.sidebar.number_input("Longitude", value=-77.3832, format="%.4f", key="lon_input")

# Automated Spatial Query Execution
station_data = get_nearest_icao_station(lat, lon)
icao = station_data["icao"]
stn_dist = station_data["dist"]

# Locked UI Element
display_icao = icao if icao != "NONE" else "N/A"
st.sidebar.text_input("Nearest Valid ICAO (Auto-Locked)", value=display_icao, disabled=True)

# Uncolored, consistent sidebar text
if icao == "NONE":
    st.sidebar.markdown("<div style='font-size: 0.85rem; color: #8E949E; margin-bottom: 15px;'>No TAF-issuing station within 50km.</div>", unsafe_allow_html=True)

# Transport Canada Airframe Classification
airframe_class = st.sidebar.selectbox(
    "Airframe Class (Transport Canada):", 
    options=[
        "Small (250g - 25kg)", 
        "Micro (< 250g)", 
        "Heavy (> 25kg)", 
        "Rotary (Helicopter)"
    ]
)

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
    "ECMWF (Global 9km)": "https://api.open-meteo.com/v1/ecmwf"
}

data = fetch_mission_data(lat, lon, model_api_map[model_choice])

# Controlled Fetch to avoid bad API calls
if icao != "NONE":
    metar_raw, taf_raw = get_aviation_weather(icao)
else:
    metar_raw, taf_raw = "NIL", "NIL"

st.title("Atmospheric Risk Management")
st.caption(f"Vector Check Aerial Group Inc. - SYSTEM ACTIVE | OPERATOR: {st.session_state.get('active_operator', 'UNKNOWN')}")
st.divider()

# Resolve Target Timezone
tf = TimezoneFinder()
tz_str = tf.timezone_at(lng=lon, lat=lat)
local_tz = pytz.timezone(tz_str) if tz_str else timezone.utc
tz_abbr = datetime.now(local_tz).tzname() if tz_str else "UTC"

if data and "hourly" in data:
    h = data["hourly"]
    
    # Generate Dual-Time Display
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

    selected_time_str = st.sidebar.select_slider(
        "Forecast Time:", 
        options=valid_times_display, 
        key="forecast_slider"
    )
    
    idx = times_display.index(selected_time_str)
    relative_hr = valid_times_display.index(selected_time_str)

    # Dynamic Forecast Navigator
    nav_col1, nav_col2, nav_col3 = st.sidebar.columns([1, 2, 1])
    nav_col1.button("◄", on_click=update_time, args=(-1,), use_container_width=True)
    nav_col2.markdown(
        f"<div style='text-align: center; font-size: 1.1rem; font-weight: bold; color: #E58E26; margin-top: 5px;'>+ {relative_hr} HR</div>", 
        unsafe_allow_html=True
    )
    nav_col3.button("►", on_click=update_time, args=(1,), use_container_width=True)

    st.sidebar.divider()
    
    # --- HARD CONVERSION TO KNOTS ---
    is_kmh = "km/h" in data.get("hourly_units", {}).get("wind_speed_10m", "km/h").lower()
    k_conv = 0.539957 if is_kmh else 1.0
    raw_wind_unit = "KT"
    
    def format_dir(d, spd):
        r = int(round(float(d), -1)) % 360
        if r == 0 and spd > 0: return 360
        if spd == 0: return 0
        return r

    # Extract Surface Data
    t_temp = h['temperature_2m'][idx]
    rh = h['relative_humidity_2m'][idx]
    w_spd = h['wind_speed_10m'][idx] * k_conv
    wx = h['weather_code'][idx]
    
    if t_temp is not None and rh is not None and rh > 0:
        a = 17.625
        b = 243.04
        alpha = math.log(rh / 100.0) + ((a * t_temp) / (b + t_temp))
        td = (b * alpha) / (a - alpha)
        raw_base = max(0, (t_temp - td) * 400)
        c_base = int(round(raw_base, -2)) 
    else:
        td = t_temp
        c_base = 10000

    sfc_dir = format_dir(h['wind_direction_10m'][idx], w_spd)

    frz_raw = h.get('freezing_level_height', [None]*len(h['time']))[idx]
    frz_disp = "SFC" if t_temp <= 0 else (f"{int(round(frz_raw * 3.28, -2)):,} ft" if frz_raw else "N/A")

    raw_gst = h.get('wind_gusts_10m', [w_spd / k_conv])[idx] * k_conv
    gst = (w_spd * 1.25) if raw_gst <= w_spd else raw_gst
    
    if "gem" in model_api_map[model_choice]:
        u_v = h['wind_speed_120m'][idx] * k_conv
        u_dir = h['wind_direction_120m'][idx]
        u_h = 120
    else:
        u_v = h['wind_speed_100m'][idx] * k_conv
        u_dir = h['wind_direction_100m'][idx]
        u_h = 100
        
    icing_cond = calculate_icing_profile(h, idx, wx)
    t_950 = h.get('temperature_950hPa', [t_temp])[idx]
    is_stable = t_950 is not None and t_950 > (t_temp - 2.0)

    dt_utc_exact = datetime.fromisoformat(h["time"][idx]).replace(tzinfo=timezone.utc)
    astro = get_astronomical_data(lat, lon, dt_utc_exact, local_tz, tz_abbr)
    space_data = get_kp_index(dt_utc_exact)
    
    sun_pos_display = f"{astro['sun_dir']} | Elev: {astro['sun_alt']}°" if astro['sun_alt'] > 0 else "NIL (Below Horizon)"
    moon_pos_display = f"{astro['moon_dir']} | Elev: {astro['moon_alt']}°" if astro['moon_alt'] > 0 else "NIL (Below Horizon)"

    # Variable capture for shared UI and CSV alignment
    weather_str = get_weather_element(wx, w_spd)

    # UI Display override for Calm / VRB logic
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
        s_c = w_spd + (u_v - w_spd) * (math.log(alt*0.3048/10) / math.log(u_h/10))
        g_c = s_c + gust_delta
        
        d_c_raw = (sfc_dir + ((u_dir - sfc_dir + 180) % 360 - 180) * (min(alt*0.3048, u_h) / u_h)) % 360
        d_c = format_dir(d_c_raw, s_c)
        
        turb, ice = get_turb_ice(alt, s_c, w_spd, g_c, wx, is_stable, icing_cond, airframe_class, t_temp)
        
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
    p_levels_traj = [1000, 950, 925, 900, 850, 800, 700, 600]
    p_profile = sorted([{'h': h.get(f'geopotential_height_{p}hPa')[idx]*3.28, 
                         's': h.get(f'wind_speed_{p}hPa')[idx] * k_conv, 
                         'd': h.get(f'wind_direction_{p}hPa')[idx]} 
                        for p in p_levels_traj if h.get(f'wind_speed_{p}hPa')[idx] is not None], 
                       key=lambda x: x['h'])
                       
    stack_ext = []
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
        turb, ice = get_turb_ice(alt, s_e, w_spd, g_e, wx, is_stable, icing_cond, airframe_class, t_temp)
        
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

    # Controlled Rendering of Station Actuals based on 50km Rule
    if icao == "NONE":
        st.subheader("Station Actuals")
        # Uncolored, consistent markdown text
        st.markdown('<div class="obs-text">No METAR/TAF information within a 50km radius.</div>', unsafe_allow_html=True)
        clean_metar = "NIL"
        clean_taf = "NIL"
    else:
        clean_metar = re.sub('<[^<]+>', '', metar_raw)
        clean_taf = re.sub('<[^<]+>', '', taf_raw)
        clean_taf = re.sub(r'\n\s*\n', '\n', clean_taf).strip()
        metar_disp = apply_tactical_highlights(clean_metar)
        taf_disp = apply_tactical_highlights(clean_taf)
        taf_disp = taf_disp.replace('\n', '<br>')
        
        st.subheader(f"Station Actuals: {icao} | {stn_dist:.1f} km from AO")
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
    
    stn_display_str = f"{icao} | Distance: {stn_dist:.1f} km" if icao != "NONE" else "No METAR/TAF information within a 50km radius."
    
    csv_header = (
        "VECTOR CHECK AERIAL GROUP INC. - Atmospheric Risk Assessment\n"
        f"Target Coordinates: {lat}, {lon}\n"
        f"Automated Weather Station: {stn_display_str}\n"
        f"Forecast Model: {model_choice} | Valid Time: {selected_time_str}\n"
        f"Airframe Class: {airframe_class}\n"
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
            f"DOWNLOAD_CSV_{airframe_class[:5]}"
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
