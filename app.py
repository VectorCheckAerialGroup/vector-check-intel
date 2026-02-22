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
    .weather-box { background-color: #161B22; border-radius: 8px; padding: 18px; border: 1px solid #30363D; font-family: 'JetBrains Mono', monospace; font-size: 1rem; color: #C9D1D9; margin-bottom: 12px; }
    .ifr-highlight { color: #FF4B4B; font-weight: bold; background-color: rgba(255, 75, 75, 0.15); padding: 2px 6px; border-radius: 4px; border: 1px solid #FF4B4B; }
    .mvfr-highlight { color: #FFD700; font-weight: bold; background-color: rgba(255, 215, 0, 0.1); padding: 2px 6px; border-radius: 4px; border: 1px solid #FFD700; }
    [data-testid="stMetricValue"] { font-size: 1.6rem !important; color: #FFFFFF !important; }
    </style>
    """, unsafe_allow_html=True)

st.title("🛡️ Vector Check: High-Res Airspace Intelligence")

# 2. SIDEBAR
st.sidebar.header("Mission Parameters")
lat = st.sidebar.number_input("Latitude", value=44.1628, format="%.4f")
lon = st.sidebar.number_input("Longitude", value=-77.3832, format="%.4f")
icao = st.sidebar.text_input("Nearest ICAO", value="CYTR").upper()

# 3. DATA FETCHING
@st.cache_data(ttl=300)
def fetch_all_data(la, lo, ic):
    # Aviation Text
    try:
        m = requests.get(f"https://aviationweather.gov/api/data/metar?ids={ic}", timeout=10).text.strip()
        t = requests.get(f"https://aviationweather.gov/api/data/taf?ids={ic}", timeout=10).text.strip()
    except: m = t = "Feed Offline"
    
    # Synoptic Data
    p_levels = [1000, 950, 925, 900, 850, 800, 700, 600, 500, 400]
    params = {
        "latitude": la, "longitude": lo,
        "hourly": ["temperature_2m", "relative_humidity_2m", "wind_speed_10m", 
                   "wind_direction_10m", "visibility", "weather_code", "wind_speed_80m", 
                   "wind_speed_120m", "freezing_level_height"] + 
                   [f"temperature_{p}hPa" for p in p_levels] + 
                   [f"dewpoint_{p}hPa" for p in p_levels],
        "forecast_days": 2, "timezone": "UTC"
    }
    try:
        om = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=15).json()
    except: om = None
    return m, t, om

m_raw, t_raw, data = fetch_all_data(lat, lon, icao)

# 4. AVIATION DISPLAY
def get_cat_html(txt):
    if not txt or "Offline" in txt: return f"<div class='weather-box'>{txt}</div>"
    if re.search(r'(BKN00[0-9]|OVC00[0-9]|VV00[0-9])|(\s[0-2]/?[0-9]?SM)', txt):
        return f"<div class='weather-box'><span class='ifr-highlight'>IFR</span> | {txt}</div>"
    if re.search(r'(BKN0[1-2][0-9]|OVC0[1-2][0-9])|(\s[3-5]SM)', txt):
        return f"<div class='weather-box'><span class='mvfr-highlight'>MVFR</span> | {txt}</div>"
    return f"<div class='weather-box'><span style='color:#78E08F'>VFR</span> | {txt}</div>"

st.subheader(f"📡 Aviation Feed: {icao}")
st.markdown(get_cat_html(m_raw), unsafe_allow_html=True)
st.markdown(get_cat_html(t_raw), unsafe_allow_html=True)

# 5. MAIN CONTENT LOOP
if data and "hourly" in data:
    h = data["hourly"]
    time_list = h["time"]
    formatted_times = [datetime.fromisoformat(t).strftime("%d %b %H:%M Z") for t in time_list]
    selected_time = st.sidebar.select_slider("Forecast Time:", options=formatted_times)
    idx = formatted_times.index(selected_time)

    # METRICS
    st.divider()
    m1, m2, m3, m4, m5 = st.columns(5)
    t_s = h['temperature_2m'][idx]
    frz_m = h['freezing_level_height'][idx]
    
    m1.metric("Sfc Temp", f"{int(t_s)}°C")
    m2.metric("Sfc Wind", f"{int(h['wind_direction_10m'][idx])}°@{int(h['wind_speed_10m'][idx])}k/h")
    
    def get_p_name(c, temp):
        names = {0: "Nil", 61: "Rain", 71: "Snow", 66: "Fz Rain"}
        n = names.get(c, "Nil")
        return "Fz Rain" if n == "Rain" and temp <= 0 else n

    m3.metric("Precip", get_p_name(h['weather_code'][idx], t_s))
    m4.metric("Freezing", f"{int(frz_m * 3.28084):,}ft")
    m5.metric("Vis", f"{int(h['visibility'][idx]/1000)}km")

    # HAZARD STACK
    try:
        st.subheader(f"📊 Low-Level Hazard Stack ({selected_time})")
        z_ft = [400, 300, 200, 100, 50]
        w10, w80, w120 = h["wind_speed_10m"][idx], h["wind_speed_80m"][idx], h["wind_speed_120m"][idx]
        w_interp = np.interp([z * 0.3048 for z in z_ft], [10, 80, 120], [w10, w80, w120])
        
        stack = []
        for i, alt in enumerate(z_ft):
            v_t = -1 if (alt * 0.3048) >= frz_m else 5
            stack.append({
                "Altitude": f"{alt} ft AGL",
                "Wind": f"{int(w_interp[i])} k/h",
                "Turb": "Mod" if w_interp[i] > 20 else "Lgt",
                "Icing/Precip": get_p_name(h['weather_code'][idx], v_t) if h['weather_code'][idx] != 0 else "Clear"
            })
        st.dataframe(pd.DataFrame(stack), hide_index=True, use_container_width=True)
    except Exception as e:
        st.error(f"Hazard Stack Error: {e}")

    # SKEW-T SOUNDING
    try:
        st.divider()
        st.subheader("🌡️ Vertical Synoptic Profile (Skew-T)")
        p_levs = [1000, 950, 925, 900, 850, 800, 700, 600, 500, 400]
        t_v = np.array([h.get(f'temperature_{p}hPa')[idx] for p in p_levs])
        td_v = np.array([h.get(f'dewpoint_{p}hPa')[idx] for p in p_levs])
        
        fig = plt.figure(figsize=(10, 18))
        fig.patch.set_facecolor('#0E1117')
        skew = SkewT(fig, rotation=45)
        skew.ax.set_facecolor('#1B1E23')
        skew.ax.tick_params(colors='#D1D5DB')
        
        skew.plot_dry_adiabats(color='#E58E26', alpha=0.3, linestyle='--')
        skew.plot_moist_adiabats(color='#4A69BD', alpha=0.3, linestyle='--')
        
        skew.plot(p_levs, t_v * units.degC, '#FF4B4B', linewidth=8, label='Temp')
        skew.plot(p_levs, td_v * units.degC, '#00FF41', linewidth=8, label='Dewpoint')
        
        for al in [1000, 3000, 5000, 10000]:
            pv = 1013.25 * (1 - (al / 145366.45))**(1 / 0.190284)
            skew.ax.text(-39, pv, f"{al:,}ft", color='#9CA3AF', weight='bold', ha='right')

        plt.legend(loc='upper right').get_frame().set_facecolor('#0E1117')
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches='tight', dpi=140, facecolor='#0E1117')
        st.image(buf, use_container_width=True)
    except Exception as e:
        st.error(f"Sounding Generation Error: {e}")
else:
    st.error("❌ CRITICAL: No Synoptic Data Found. Check API status.")
