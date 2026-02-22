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

st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Inter:wght@400;700&display=swap');
    .stApp { background-color: #0E1117; font-family: 'Inter', sans-serif; }
    
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
    
    .ifr-highlight { color: #FF4B4B; font-weight: bold; background-color: rgba(255, 75, 75, 0.15); padding: 2px 6px; border-radius: 4px; border: 1px solid #FF4B4B; }
    .mvfr-highlight { color: #FFD700; font-weight: bold; background-color: rgba(255, 215, 0, 0.15); padding: 2px 6px; border-radius: 4px; border: 1px solid #FFD700; }
    .vfr-highlight { color: #78E08F; font-weight: bold; }

    [data-testid="stMetricValue"] { font-size: 1.6rem !important; color: #FFFFFF !important; }
    [data-testid="stMetricLabel"] { font-size: 0.9rem !important; color: #8E949E !important; }
    </style>
    """, unsafe_allow_html=True)

st.title("🛡️ Vector Check: High-Res Airspace Intelligence")

# 2. SIDEBAR
st.sidebar.header("Mission Parameters")
lat = st.sidebar.number_input("Latitude", value=44.1628, format="%.4f")
lon = st.sidebar.number_input("Longitude", value=-77.3832, format="%.4f")
icao = st.sidebar.text_input("Nearest ICAO", value="CYTR").upper()

# 3. WEATHER TEXT PARSING
def get_flight_cat_html(text, station_id):
    if not text or "No" in text or "Sync" in text: return f"<div class='weather-box'>{text}</div>"
    is_ifr = re.search(r'(BKN00[0-9]|OVC00[0-9]|VV00[0-9])|(\s[0-2]/?[0-9]?SM)', text)
    is_mvfr = re.search(r'(BKN0[1-2][0-9]|OVC0[1-2][0-9])|(\s[3-5]SM)', text)
    
    if is_ifr:
        return f"<div class='weather-box'><span class='ifr-highlight'>IFR</span> | {text}</div>"
    elif is_mvfr:
        return f"<div class='weather-box'><span class='mvfr-highlight'>MVFR</span> | {text}</div>"
    else:
        return f"<div class='weather-box'><span class='vfr-highlight'>[VFR]</span> | {text}</div>"

# Fetch Aviation Text
try:
    m_raw = requests.get(f"https://aviationweather.gov/api/data/metar?ids={icao}", timeout=5).text.strip()
    t_raw = requests.get(f"https://aviationweather.gov/api/data/taf?ids={icao}", timeout=5).text.strip()
except:
    m_raw = t_raw = "Data Sync Error"

st.subheader(f"📡 Aviation Feed: {icao}")
st.markdown(get_flight_cat_html(m_raw, icao), unsafe_allow_html=True)
st.markdown(get_flight_cat_html(t_raw, icao), unsafe_allow_html=True)

# 4. PRIMARY DATA LOOP
@st.cache_data(ttl=600)
def fetch_mission_data(latitude, longitude):
    url = "https://api.open-meteo.com/v1/forecast"
    p_levels = [1000, 950, 925, 900, 850, 800, 700, 600, 500, 400]
    params = {
        "latitude": latitude, "longitude": longitude,
        "hourly": ["temperature_2m", "relative_humidity_2m", "wind_speed_10m", 
                   "wind_direction_10m", "visibility", "weather_code", "wind_speed_80m", 
                   "wind_speed_120m", "freezing_level_height"] + 
                   [f"temperature_{p}hPa" for p in p_levels] + 
                   [f"dewpoint_{p}hPa" for p in p_levels],
        "forecast_days": 2, "timezone": "UTC"
    }
    return requests.get(url, params=params).json()

data = fetch_mission_data(lat, lon)

def get_precip_name(code, temp):
    codes = {0: "Nil", 51: "Drizzle", 61: "Rain", 71: "Snow", 95: "TS"}
    base_name = codes.get(code, "Nil")
    if base_name == "Rain" and temp <= 0: return "Fz Rain"
    return base_name

if data and "hourly" in data:
    h = data["hourly"]
    time_list = h["time"]
    formatted_times = [datetime.fromisoformat(t).strftime("%d %b %H:%M Z") for t in time_list]
    selected_time_str = st.sidebar.select_slider("Select Forecast Window:", options=formatted_times)
    idx = formatted_times.index(selected_time_str)

    st.divider()
    # METRICS ROW
    m1, m2, m3, m4, m5 = st.columns(5)
    t_s = h['temperature_2m'][idx]
    frz_lvl_m = h['freezing_level_height'][idx]
    
    m1.metric("Sfc Temp", f"{int(t_s)}°C")
    m2.metric("Sfc Wind", f"{int(h['wind_direction_10m'][idx])}°@{int(h['wind_speed_10m'][idx])}k/h")
    m3.metric("Precip Type", get_precip_name(h['weather_code'][idx], t_s))
    m4.metric("Freezing Lvl", f"{int(frz_lvl_m * 3.28084):,}ft")
    m5.metric("Vis Range", f"{int(h['visibility'][idx]/1000)}km")

    # 5. HAZARD STACK WITH VERTICAL PRECIP LOGIC
    st.subheader(f"📊 Low-Level Hazard Stack ({selected_time_str})")
    z_ft = [400, 300, 200, 100, 50]
    w10, w80, w120 = h["wind_speed_10m"][idx], h["wind_speed_80m"][idx], h["wind_speed_120m"][idx]
    w_interp = np.interp([z * 0.3048 for z in z_ft], [10, 80, 120], [w10, w80, w120])
    
    stack_data = []
    for i, alt in enumerate(z_ft):
        # Vertical Precip Logic: If altitude > freezing level, it's likely frozen/freezing
        alt_m = alt * 0.3048
        is_below_freezing = alt_m >= frz_lvl_m
        p_type = get_precip_name(h['weather_code'][idx], -1 if is_below_freezing else 5)
        
        stack_data.append({
            "Altitude (AGL)": f"{alt} ft",
            "Wind Speed": f"{int(w_interp[i])} k/h",
            "Turbulence": "Moderate" if w_interp[i] > 22 else "Light" if w_interp[i] > 12 else "Nil",
            "Icing / Precip": p_type if h['weather_code'][idx] != 0 else "Clear"
        })
    
    st.dataframe(pd.DataFrame(stack_data), hide_index=True, use_container_width=True)

    # 6. VERTICAL PROFILE (SKEW-T)
    st.divider()
    st.subheader("🌡️ Vertical Synoptic Profile (Skew-T)")
    p_levels = [1000, 950, 925, 900, 850, 800, 700, 600, 500, 400]
    t_vals = np.array([h.get(f'temperature_{p}hPa')[idx] for p in p_levels])
    td_vals = np.array([h.get(f'dewpoint_{p}hPa')[idx] for p in p_levels])
    
    fig = plt.figure(figsize=(10, 18))
    fig.patch.set_facecolor('#0E1117') 
    skew = SkewT(fig, rotation=45)
    skew.ax.set_facecolor('#1B1E23')

    skew.ax.tick_params(colors='#D1D5DB', labelsize=10)
    skew.plot_dry_adiabats(color='#E58E26', alpha=0.3, linewidth=1, linestyle='--')
    skew.plot_moist_adiabats(color='#4A69BD', alpha=0.3, linewidth=1, linestyle='--')
    
    # 8px line width for "Vector Check" profile pop
    skew.plot(p_levels, t_vals * units.degC, '#FF4B4B', linewidth=8, label='Temperature')
    skew.plot(p_levels, td_vals * units.degC, '#00FF41', linewidth=8, label='Dewpoint')
    
    # Altitude markings
    for alt_l in [1000, 3000, 5000, 10000, 15000]:
        p_val = 1013.25 * (1 - (alt_l / 145366.45))**(1 / 0.190284)
        skew.ax.text(-39, p_val, f"{alt_l:,}ft", color='#9CA3AF', fontsize=12, ha='right')

    leg = plt.legend(loc='upper right', prop={'size': 12})
    leg.get_frame().set_facecolor('#0E1117')
    for text in leg.get_texts(): text.set_color('#FFFFFF')
    
    buf = io.BytesIO(); fig.savefig(buf, format="png", bbox_inches='tight', dpi=140, facecolor='#0E1117')
    st.image(buf, use_container_width=True)
else:
    st.warning("Awaiting Mission Parameter input or Data Feed offline.")
