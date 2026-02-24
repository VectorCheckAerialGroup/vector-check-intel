import streamlit as st
import pandas as pd
import math
import re
from datetime import datetime, timezone
from timezonefinder import TimezoneFinder
import pytz

# Import Vector Check Modules
from modules.data_ingest import get_aviation_weather, fetch_mission_data
from modules.hazard_logic import get_precip_type, calculate_icing_profile, get_turb_ice
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
icao = st.sidebar.text_input("Nearest ICAO", value="CYTR", key="icao_input").upper().strip()

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
        st.session_state.get("icao_input", "CYTR"), 
        "MANUAL_REFRESH"
    )

st.sidebar.button("Force Manual Data Refresh", on_click=log_refresh_callback)

model_api_map = {
    "HRDPS (Canada 2.5km)": "https://api.open-meteo.com/v1/gem",
    "ECMWF (Global 9km)": "https://api.open-meteo.com/v1/ecmwf"
}

data = fetch_mission_data(lat, lon, model_api_map[model_choice])
metar_raw, taf_raw = get_aviation_weather(icao)

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
    
    # Generate Dual-Time Display for the Slider
    times_display = []
    for t in h["time"]:
        dt_u = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
        dt_l = dt_u.astimezone(local_tz)
        times_display.append(f"{dt_u.strftime('%d %b %H:%M')} Z | {dt_l.strftime('%H:%M')} {tz_abbr}")
        
    # SLIDER STATE MANAGEMENT & QUICK NAVIGATION BUTTONS
    if "forecast_time_val" not in st.session_state or st.session_state.forecast_time_val not in times_display:
        st.session_state.forecast_time_val = times_display[0]

    def update_time(offset):
        try:
            curr = times_display.index(st.session_state.forecast_time_val)
            st.session_state.forecast_time_val = times_display[min(len(times_display)-1, curr + offset)]
        except ValueError:
            st.session_state.forecast_time_val = times_display[0]

    def reset_time():
        st.session_state.forecast_time_val = times_display[0]

    selected_time_str = st.sidebar.select_slider(
        "Forecast Hour:", 
        options=times_display, 
        key="forecast_time_val"
    )
    idx = times_display.index(selected_time_str)

    # Inject Quick Jump Buttons
    btn_col1, btn_col2, btn_col3, btn_col4 = st.sidebar.columns(4)
    btn_col1.button("Now", on_click=reset_time)
    btn_col2.button("+3h", on_click=update_time, args=(3,))
    btn_col3.button("+6h", on_click=update_time, args=(6,))
    btn_col4.button("+12h", on_click=update_time, args=(12,))

    st.sidebar.divider()
    
    # --- NATIVE UNIT PASSTHROUGH ---
    raw_wind_unit = data.get("hourly_units", {}).get("wind_speed_10m", "km/h")
    
    # Extract Surface Data Without Conversion
    t_temp = h['temperature_2m'][idx]
    rh = h['relative_humidity_2m'][idx]
    w_spd = h['wind_speed_10m'][idx] 
    wx = h['weather_code'][idx]
    td = t_temp - ((100 - rh) / 5) if (t_temp is not None and rh is not None) else t_temp
    sfc_dir = int(h['wind_direction_10m'][idx])
    frz_raw = h.get('freezing_level_height', [None]*len(h['time']))[idx]
    frz_disp = "SFC" if t_temp <= 0 else (f"{int(round(frz_raw * 3.28, -2)):,} ft" if frz_raw else "N/A")
    c_base = int((t_temp - td)*400) if (t_temp is not None and td is not None) else 10000

    raw_gst = h.get('wind_gusts_10m', [w_spd])[idx]
    gst = (w_spd * 1.25) if raw_gst <= w_spd else raw_gst
    
    if "gem" in model_api_map[model_choice]:
        u_v = h['wind_speed_120m'][idx]
        u_dir = h['wind_direction_120m'][idx]
        u_h = 120
    else:
        u_v = h['wind_speed_100m'][idx]
        u_dir = h['wind_direction_100m'][idx]
        u_h = 100
        
    icing_cond = calculate_icing_profile(h, idx, wx)
    t_950 = h.get('temperature_950hPa', [t_temp])[idx]
    is_stable = t_950 is not None and t_950 > (t_temp - 2.0)

    # Pre-calculate astronomical and space weather
    dt_utc_exact = datetime.fromisoformat(h["time"][idx]).replace(tzinfo=timezone.utc)
    astro = get_astronomical_data(lat, lon, dt_utc_exact, local_tz, tz_abbr)
    space_data = get_kp_index(dt_utc_exact)
    
    sun_pos_display = f"{astro['sun_dir']} | Elev: {astro['sun_alt']}°" if astro['sun_alt'] > 0 else "NIL (Below Horizon)"
    moon_pos_display = f"{astro['moon_dir']} | Elev: {astro['moon_alt']}°" if astro['moon_alt'] > 0 else "NIL (Below Horizon)"

    # --- UI RENDERING STARTS HERE ---
    
    st.subheader("Forecasted Surface Data")
    c = st.columns(8)
    c[0].metric("Temp", f"{t_temp}°C")
    c[1].metric("RH", f"{rh}%")
    c[2].metric("Wind Dir", f"{sfc_dir:03d}°")
    c[3].metric(f"Wind Spd", f"{int(w_spd)} {raw_wind_unit}")
    c[4].metric("Precip Type", get_precip_type(wx))
    c[5].metric("Vis (Est)", f"{int((100-rh)/5 * 1.13)} sm")
    c[6].metric("Freezing LVL", frz_disp)
    c[7].metric("Cloud Base", f"{c_base} ft")

    st.divider()

    st.subheader(f"Tactical Hazard Stack (0-400ft AGL)")
    stack_tactical = []
    
    # PRE-CALCULATE GUST DELTA TO PREVENT MATH EXPLOSION
    gust_delta = max(0, gst - w_spd)
    
    for alt in [400, 300, 200, 100]:
        s_c = w_spd + (u_v - w_spd) * (math.log(alt*0.3048/10) / math.log(u_h/10))
        g_c = s_c + gust_delta
        d_c = (sfc_dir + ((u_dir - sfc_dir + 180) % 360 - 180) * (min(alt*0.3048, u_h) / u_h)) % 360
        
        turb, ice = get_turb_ice(alt, s_c, w_spd, g_c, wx, is_stable, icing_cond, airframe_class)
        
        stack_tactical.append({
            "Alt (AGL)": f"{alt}ft", 
            "Dir": f"{int(d_c):03d}°", 
            f"Spd ({raw_wind_unit})": int(s_c), 
            f"Gust ({raw_wind_unit})": int(g_c), 
            "Turbulence": turb, 
            "Icing": ice
        })
    
    df_tactical = pd.DataFrame(stack_tactical).set_index("Alt (AGL)")
    st.table(df_tactical)

    st.subheader("Extended Trajectory (1,000-5,000ft AGL)")
    p_levels_traj = [1000, 950, 925, 900, 850, 800, 700, 600]
    p_profile = sorted([{'h': h.get(f'geopotential_height_{p}hPa')[idx]*3.28, 
                         's': h.get(f'wind_speed_{p}hPa')[idx], 
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
        d_e = (blw['d'] + ((abv['d'] - blw['d'] + 180) % 360 - 180) * frac) % 360
        
        g_e = s_e + gust_delta
        turb, ice = get_turb_ice(alt, s_e, w_spd, g_e, wx, is_stable, icing_cond, airframe_class)
        
        stack_ext.append({
            "Alt (AGL)": f"{alt}ft", 
            "Dir": f"{int(d_e):03d}°", 
            f"Spd ({raw_wind_unit})": int(s_e), 
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

    # --- METAR / TAF MOVED HERE ---
    st.subheader(f"Official Aviation Briefing ({icao})")
    st.markdown(f'<div style="background-color: #1B1E23; padding: 15px; border-radius: 5px;"><div class="obs-text"><strong style="color: #8E949E;">METAR/SPECI</strong><br>{metar_raw}<br><br><strong style="color: #8E949E;">TAF</strong><br>{taf_raw}</div></div>', unsafe_allow_html=True)
    
    st.divider()

    # --- ADVANCED CSV EXPORT ENGINE ---
    df_export = pd.concat([df_tactical, df_ext])
    
    clean_metar = re.sub('<[^<]+>', '', metar_raw.replace('<br>', ' '))
    clean_taf = re.sub('<[^<]+>', '', taf_raw.replace('<br>', '\n'))
    
    csv_header = (
        "VECTOR CHECK AERIAL GROUP INC. - MISSION HAZARD MATRIX\n"
        f"Target ICAO: {icao} | Coordinates: {lat}, {lon}\n"
        f"Forecast Model: {model_choice} | Valid Time: {selected_time_str}\n"
        f"Airframe Class: {airframe_class}\n"
        f"Wind Unit Standard: {raw_wind_unit}\n" 
        f"Sun ({astro['tz']}): Rise {astro['sunrise']} | Set {astro['sunset']} | Civil Dawn {astro['dawn']} | Civil Dusk {astro['dusk']}\n"
        f"Moon ({astro['tz']}): Rise {astro['moonrise']} | Set {astro['moonset']} | Illum {astro['moon_ill']}%\n"
        f"Space Weather: Kp Index {space_data['kp']} | GNSS Risk: {space_data['risk']}\n"
        f"Position: Sun {sun_pos_display} | Moon {moon_pos_display}\n\n"
        f"METAR/SPECI:\n{clean_metar}\n\n"
        f"TAF:\n{clean_taf}\n\n"
        "--- HAZARD STACK (AGL) ---\n"
    )
    
    csv_data = (csv_header + df_export.to_csv()).encode('utf-8')
    
    def log_download_callback():
        log_action(
            st.session_state.get("active_operator", "UNKNOWN"), 
            st.session_state.get("lat_input", 44.1628), 
            st.session_state.get("lon_input", -77.3832), 
            st.session_state.get("icao_input", "CYTR"), 
            f"DOWNLOAD_CSV_{airframe_class[:5]}"
        )
    
    st.download_button(
        label="📥 Download Pre-Flight Hazard Matrix (CSV)",
        data=csv_data,
        file_name=f"VCAG_Hazard_Matrix_{icao}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
        on_click=log_download_callback
    )

    st.divider()
    st.subheader("Vertical Atmospheric Profile (Convective Ops)")
    sfc_h = data.get('elevation', 0) * 3.28084
    fig = plot_convective_profile(h, idx, t_temp, td, w_spd, sfc_dir, sfc_h)
    
    if fig:
        st.pyplot(fig)
    else:
        st.warning("Insufficient atmospheric layers available to render vertical profile.")

# 7. LIABILITY & TRACKING ARMOR
st.divider()
st.markdown("""
<div style="text-align: center; color: #8E949E; font-size: 0.85rem; padding: 20px;">
<strong>⚠️ FOR SITUATIONAL AWARENESS ONLY</strong><br>
This system translates raw meteorological model data for uncrewed systems. It does not replace official NAV CANADA or NOAA flight service briefings. The Pilot in Command (PIC) retains ultimate authority and responsibility for flight safety. Vector Check Aerial Group Inc. assumes no liability for operational decisions made using this tool. <br><br>
<em>Usage of this system, including geographic querying and CSV generation, is actively logged to a secure database for audit and security purposes.</em>
</div>
""", unsafe_allow_html=True)
