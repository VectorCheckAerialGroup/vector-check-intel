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

# 1. PAGE CONFIG
st.set_page_config(page_title="Vector Check: Mission Intel", layout="wide")

# GLOBAL SLEEK DARK THEME & MODERN FONTS
st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Inter:wght@400;700&display=swap');
    
    .stApp { background-color: #0E1117; font-family: 'Inter', sans-serif; }
    
    /* Modern Weather Text Boxes */
    .weather-box {
        background-color: #161B22;
        border-radius: 8px;
        padding: 15px;
        border: 1px solid #30363D;
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.95rem;
        line-height: 1.6;
        margin-bottom: 10px;
        color: #C9D1D9;
    }
    
    .ifr-text { color: #FF4B4B; font-weight: bold; border-bottom: 1px solid #FF4B4B; }
    .mvfr-text { color: #FFD700; font-weight: bold; border-bottom: 1px solid #FFD700; }
    .reason-alert { font-weight: bold; text-decoration: underline; }

    [data-testid="stMetricValue"] { font-size: 1.6rem !important; color: #FFFFFF !important; }
    [data-testid="stMetricLabel"] { font-size: 0.9rem !important; color: #8E949E !important; }
    </style>
    """, unsafe_allow_html=True)

st.title("🛡️ Vector Check: High-Res Airspace Intelligence")

# 2. DATA FETCHING
st.sidebar.header("Mission Parameters")
lat = st.sidebar.number_input("Latitude", value=44.1628, format="%.4f")
lon = st.sidebar.number_input("Longitude", value=-77.3832, format="%.4f")
icao = st.sidebar.text_input("Nearest ICAO", value="CYTR").upper()

# Logic to highlight Flight Categories
def format_aviation_text(text):
    if not text or "No" in text: return text
    
    # Simple RegEx to find Ceiling and Visibility (The "Reasons")
    # Searches for 1/4SM, 2SM etc or BKN004, OVC008
    low_vis = re.findall(r'(\s[12]?/?\d?SM)', text)
    low_ceil = re.findall(r'(BKN00[1-9]|OVC00[1-9]|BKN01[0-9]|OVC01[0-9])', text)
    
    # Determine Category
    category = "VFR"
    reason = ""
    
    # Check IFR (Vis < 3SM or Ceiling < 1000ft)
    if any(float(v.replace('SM','').strip().split('/')[-1]) < 3 for v in low_vis if '/' in v) or low_ceil:
        category = "IFR"
        reason = "LOW CEILING/VIS"
    # Check MVFR (Vis 3-5SM or Ceiling 1000-3000ft)
    elif "3SM" in text or "4SM" in text or "5SM" in text:
        category = "MVFR"
        reason = "MARGINAL VIS"

    if category == "IFR":
        text = text.replace(icao, f"<span class='ifr-text'>[{category}] {icao}</span>")
        for r in low_vis + low_ceil:
            text = text.replace(r, f"<span class='ifr-text reason-alert'>{r}</span>")
    elif category == "MVFR":
        text = text.replace(icao, f"<span class='mvfr-text'>[{category}] {icao}</span>")
        for r in low_vis + low_ceil:
            text = text.replace(r, f"<span class='mvfr-text reason-alert'>{r}</span>")
            
    return text

# 3. DISPLAY
metar_url = f"https://aviationweather.gov/api/data/metar?ids={icao}"
taf_url = f"https://aviationweather.gov/api/data/taf?ids={icao}"
try:
    metar_raw = requests.get(metar_url, timeout=5).text.strip()
    taf_raw = requests.get(taf_url, timeout=5).text.strip()
except:
    metar_raw, taf_raw = "Sync Error", "Sync Error"

st.subheader(f"📡 Tactical Weather Feed: {icao}")
st.markdown(f"<div class='weather-box'>{format_aviation_text(metar_raw)}</div>", unsafe_allow_html=True)
st.markdown(f"<div class='weather-box'>{format_aviation_text(taf_raw)}</div>", unsafe_allow_html=True)

# ... [Rest of your Dashboard logic: Metrics, Hazard Stack, and Sounding remain below] ...

# 4. SOUNDING (Ensuring Dark Sleek Profile remains visible)
@st.cache_data(ttl=600)
def fetch_mission_data(latitude, longitude):
    url = "https://api.open-meteo.com/v1/forecast"
    p_levels = [1000, 950, 925, 900, 850, 800, 700, 600, 500, 400]
    params = {
        "latitude": latitude, "longitude": longitude,
        "hourly": ["temperature_2m", "relative_humidity_2m", "wind_speed_10m", 
                   "wind_direction_10m", "visibility", "weather_code", "wind_speed_80m", 
                   "wind_speed_120m", "freezing_level_height", "cloud_cover", "is_day"] + 
                   [f"temperature_{p}hPa" for p in p_levels] + 
                   [f"dewpoint_{p}hPa" for p in p_levels],
        "forecast_days": 2, "timezone": "UTC"
    }
    res = requests.get(url, params=params)
    return res.json()

data = fetch_mission_data(lat, lon)
if data and "hourly" in data:
    h = data["hourly"]
    idx = 0 # Defaulting to current for now
    p_levels = [1000, 950, 925, 900, 850, 800, 700, 600, 500, 400]
    t_vals = np.array([h.get(f'temperature_{p}hPa')[idx] for p in p_levels])
    td_vals = np.array([h.get(f'dewpoint_{p}hPa')[idx] for p in p_levels])
    
    fig = plt.figure(figsize=(10, 35)) 
    fig.patch.set_facecolor('#0E1117') 
    skew = SkewT(fig, rotation=45)
    skew.ax.set_facecolor('#1B1E23') # Sleek Charcoal

    # Visibility enhancements for Sounding
    skew.ax.tick_params(colors='#D1D5DB', labelsize=12)
    skew.plot(p_levels, t_vals * units.degC, '#FF4B4B', linewidth=8, label='Temp')
    skew.plot(p_levels, td_vals * units.degC, '#00FF41', linewidth=8, label='Dewpt')
    
    plt.legend(loc='upper right', prop={'size': 14}).get_frame().set_facecolor('#0E1117')
    
    buf = io.BytesIO(); fig.savefig(buf, format="png", bbox_inches='tight', dpi=140, facecolor='#0E1117')
    st.image(buf, use_container_width=True)
