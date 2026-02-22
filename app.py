import streamlit as st
import requests
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from metpy.plots import SkewT
from metpy.units import units
import io
import re
from datetime import datetime

# 1. PAGE CONFIG & THEME
st.set_page_config(page_title="Vector Check: Mission Intel", layout="wide")

st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Inter:wght@400;700&display=swap');
    .stApp { background-color: #0E1117; font-family: 'Inter', sans-serif; }
    
    /* Terminal Style Weather Boxes */
    .weather-box {
        background-color: #161B22;
        border-radius: 8px;
        padding: 18px;
        border: 1px solid #30363D;
        font-family: 'JetBrains Mono', monospace;
        font-size: 1rem;
        line-height: 1.6;
        margin-bottom: 12px;
        color: #C9D1D9;
    }
    
    .ifr-highlight { color: #FF4B4B; font-weight: bold; background-color: rgba(255, 75, 75, 0.1); padding: 2px 4px; border-radius: 4px; }
    .mvfr-highlight { color: #FFD700; font-weight: bold; background-color: rgba(255, 215, 0, 0.1); padding: 2px 4px; border-radius: 4px; }
    
    [data-testid="stMetricValue"] { font-size: 1.6rem !important; color: #FFFFFF !important; }
    [data-testid="stMetricLabel"] { font-size: 0.9rem !important; color: #8E949E !important; }
    </style>
    """, unsafe_allow_html=True)

st.title("🛡️ Vector Check: High-Res Airspace Intelligence")

# 2. SIDEBAR MISSION PARAMETERS
st.sidebar.header("Mission Parameters")
lat = st.sidebar.number_input("Latitude", value=44.1628, format="%.4f")
lon = st.sidebar.number_input("Longitude", value=-77.3832, format="%.4f")
icao = st.sidebar.text_input("Nearest ICAO", value="CYTR").upper()

# 3. WEATHER TEXT PARSING (IFR/MVFR)
def get_flight_cat_html(text):
    if not text or "No" in text: return text
    
    # Logic: Look for low ceilings (001-009 IFR, 010-030 MVFR) and visibility
    is_ifr = re.search(r'(BKN00[0-9]|OVC00[0-9])|(\s[12]/?[0-9]?SM)', text)
    is_mvfr = re.search(r'(BKN0[1-2][0-9]|OVC0[1-2][0-9])|(\s[3-5]SM)', text)
    
    if is_ifr:
        return f"<div class='weather-box'><span class='ifr-highlight'>[IFR]</span> {text}</div>"
    elif is_mvfr:
        return f"<div class='weather-box'><span class='mvfr-highlight'>[MVFR]</span> {text}</div>"
    else:
        return f"<div class='weather-box'><span style='color:#78E08F'>[VFR]</span> {text}</div>"

metar_url = f"https://aviationweather.gov/api/data/metar?ids={icao}"
taf_url = f"https://aviationweather.gov/api/data/taf?ids={icao}"
try:
    m_raw = requests.get(metar_url, timeout=5).text.strip()
    t_raw = requests.get(taf_url, timeout=5).text.strip()
except:
    m_raw = t_raw = "Data Sync Error"

st.subheader(f"📡 Aviation Feed: {icao}")
st.markdown(get_flight_cat_html(m_raw), unsafe_allow_html=True)
st.markdown(get_flight_cat_html(t_raw), unsafe_allow_html=True)

# 4. DATA FETCHING
@st.cache_data(ttl=600)
def fetch_mission_data(latitude, longitude):
    url = "https://api.open-meteo.com/v1/forecast"
    p_levels = [1000, 950, 925, 900, 850, 800, 700, 600, 500, 400]
    params = {
        "latitude": latitude, "longitude": longitude,
        "hourly": ["temperature_2m", "relative_humidity_2m", "wind_speed_10m", 
                   "wind_direction_10m", "visibility", "weather_code", "wind_speed_80m", 
                   "wind_speed_120m", "freezing_level_height", "cloud_cover"] + 
                   [f"temperature_{p}hPa" for p in p_levels] + 
                   [f"dewpoint_{p}hPa" for p in p_levels],
        "forecast_days": 2, "timezone": "UTC"
    }
    return requests.get(url, params=params).json()

data = fetch_mission_data(lat, lon)

def get_precip_name(code):
    codes = {0: "Clear", 51: "Drizzle", 61: "Rain", 71: "Snow", 95: "TS"}
    return codes.get(code, "Nil")

if data and "hourly" in data:
    h = data["hourly"]
    time_list = h["time"]
    formatted_times = [datetime.fromisoformat(t).strftime("%d %b %H:%M Z") for t in time_list]
    selected_time_str = st.sidebar.select_slider("Forecast Hour:", options=formatted_times)
    idx = formatted_times.index(selected_time_str)

    # METRICS
    m1, m2, m3, m4, m5 = st.columns(5)
    t_s = h['temperature_2m'][idx]
    m1.metric("Temp", f"{int(t_s)}°C")
    m2.metric("Sfc Wind", f"{int(h['wind_direction_10m'][idx])}°@{int(h['wind_speed_10m'][idx])}k/h")
    m3.metric("Precip", get_precip_name(h['weather_code'][idx]))
    m4.metric("Freezing", f"{int(h['freezing_level_height'][idx] * 3.28)}ft")
    m5.metric("Vis", f"{int(h['visibility'][idx]/1000)}km")

    # RESTORED HAZARD STACK
    st.subheader(f"📊 Low-Level Hazard Stack ({selected_time_str})")
    z_ft = [400, 300, 200, 100, 50]
    w10, w80, w120 = h["wind_speed_10m"][idx], h["wind_speed_80m"][idx], h["wind_speed_120m"][idx]
    w_interp = np.interp([z * 0.3048 for z in z_ft], [10, 80, 120], [w10, w80, w120])
    
    hazard_df = pd.DataFrame({
        "Altitude": [f"{z} ft AGL" for z in z_ft],
        "Wind (k/h)": [int(w) for w in w_interp],
        "Turbulence": ["Mod" if w > 20 else "Lgt" if w > 12 else "Nil" for w in w_interp],
        "Precip Type": [get_precip_name(h['weather_code'][idx])] * 5
    })
    st.dataframe(hazard_df, hide_index=True, use_container_width=True)

    # 5. SOUNDING (Stealth Dark & Visible)
    st.divider()
    st.subheader("🌡️ Vertical Synoptic Profile")
    p_levels = [1000, 950, 925, 900, 850, 800, 700, 600, 500, 400]
    t_vals = np.array([h.get(f'temperature_{p}hPa')[idx] for p in p_levels])
    td_vals = np.array([h.get(f'dewpoint_{p}hPa')[idx] for p in p_levels])
    
    fig = plt.figure(figsize=(10, 18)) # Sleeker height for visibility
    fig.patch.set_facecolor('#0E1117') 
    skew = SkewT(fig, rotation=45)
    skew.ax.set_facecolor('#1B1E23')

    skew.ax.tick_params(colors='#D1D5DB', labelsize=10)
    skew.plot_dry_adiabats(color='#E58E26', alpha=0.3, linewidth=1, linestyle='--')
    skew.plot_moist_adiabats(color='#4A69BD', alpha=0.3, linewidth=1, linestyle='--')
    
    # Glow Effect Profiles
    skew.plot(p_levels, t_vals * units.degC, '#FF4B4B', linewidth=6, label='Temp')
    skew.plot(p_levels, td_vals * units.degC, '#00FF41', linewidth=6, label='Dewpt')
    
    # Legend & Style
    leg = plt.legend(loc='upper right', prop={'size': 12})
    leg.get_frame().set_facecolor('#0E1117')
    for text in leg.get_texts(): text.set_color('#FFFFFF')
    
    buf = io.BytesIO(); fig.savefig(buf, format="png", bbox_inches='tight', dpi=140, facecolor='#0E1117')
    st.image(buf, use_container_width=True)
