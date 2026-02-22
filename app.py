import streamlit as st
import requests
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from metpy.plots import SkewT
from metpy.units import units
import io
import math
from datetime import datetime

# 1. PAGE CONFIG
st.set_page_config(page_title="Vector Check: Atmospheric Risk Management", layout="wide")

# CUSTOM CSS: STEALTH THEME
st.markdown("""
    <style>
    [data-testid="stMetricValue"] { font-size: 1.4rem !important; color: #E58E26 !important; }
    [data-testid="stMetricLabel"] { font-size: 0.8rem !important; color: #8E949E !important; }
    table { margin-left: auto; margin-right: auto; text-align: center !important; width: 90%; border-collapse: collapse; background-color: #1B1E23; }
    th { text-align: center !important; color: #8E949E !important; font-weight: bold !important; padding: 10px !important; border-bottom: 2px solid #3E444E !important; text-transform: uppercase; }
    td { text-align: center !important; padding: 8px !important; color: #D1D5DB !important; border-bottom: 1px solid #2D3139 !important; }
    </style>
    """, unsafe_allow_html=True)

st.title("Atmospheric Risk Management")
st.caption("")

# 2. SIDEBAR
st.sidebar.header("Mission Parameters")
lat = st.sidebar.number_input("Latitude", value=44.1628, format="%.4f")
lon = st.sidebar.number_input("Longitude", value=-77.3832, format="%.4f")
icao = st.sidebar.text_input("Nearest ICAO", value="CYTR").upper()

model_choice = st.sidebar.selectbox("Select Forecast Model:", 
    options=["HRDPS (Canada 2.5km)", "ECMWF (Global 9km)"])

model_api_map = {
    "HRDPS (Canada 2.5km)": "https://api.open-meteo.com/v1/gem",
    "ECMWF (Global 9km)": "https://api.open-meteo.com/v1/ecmwf"
}

# 3. ROBUST HELPERS
def safe_val(val, multiplier=1, default="N/A", precision=0):
    if val is None: return default
    res = val * multiplier
    return f"{res:,.{precision}f}" if precision > 0 else f"{int(round(res)):,}"

def get_best_upper_wind(h_data, idx):
    for key, height in [('wind_speed_120m', 120), ('wind_speed_100m', 100), ('wind_speed_80m', 80)]:
        val_list = h_data.get(key)
        if val_list and val_list[idx] is not None:
            return val_list[idx], height
    return None, None

# 4. DATA FETCHING
@st.cache_data(ttl=600)
def fetch_mission_data(latitude, longitude, model_url):
    hourly_params = [
        "temperature_2m", "relative_humidity_2m", "wind_speed_10m", "wind_gusts_10m",
        "wind_direction_10m", "visibility", "weather_code", "pressure_msl",
        "wind_speed_80m", "wind_speed_120m", "wind_speed_100m", "freezing_level_height", "cloud_cover"
    ]
    p_levels = [1000, 950, 925, 900, 850, 800, 700, 600, 500, 400]
    hourly_params += [f"temperature_{p}hPa" for p in p_levels] + [f"dewpoint_{p}hPa" for p in p_levels]

    params = {
        "latitude": latitude, "longitude": longitude,
        "hourly": hourly_params,
        "wind_speed_unit": "kn", "forecast_days": 2, "timezone": "UTC"
    }
    try:
        res = requests.get(model_url, params=params, timeout=15)
        res.raise_for_status()
        return res.json()
    except: return None

@st.cache_data(ttl=300)
def get_aviation_weather(station):
    try:
        m = requests.get(f"https://aviationweather.gov/api/data/metar?ids={station}", timeout=5).text.strip()
        t = requests.get(f"https://aviationweather.gov/api/data/taf?ids={station}", timeout=5).text.strip()
        return m or "No METAR", t or "No TAF"
    except: return "Link Error", "Link Error"

# 5. MAIN RENDER
data = fetch_mission_data(lat, lon, model_api_map[model_choice])
metar, taf = get_aviation_weather(icao)

st.subheader(f"{model_choice} Analysis + {icao} Text")

# CUSTOM CSS FOR AVIATION TEXT (Matches Hazard Stack Table Font)
st.markdown(f"""
    <div style="
        background-color: #1B1E23; 
        padding: 15px; 
        border: 1px solid #2D3139; 
        border-radius: 5px; 
        font-family: sans-serif; 
        color: #D1D5DB; 
        font-size: 0.9rem;
        line-height: 1.6;
    ">
        <strong style="color: #8E949E; text-transform: uppercase;">METAR</strong><br>
        {metar}<br><br>
        <strong style="color: #8E949E; text-transform: uppercase;">TAF</strong><br>
        {taf.replace('TAF', '<br>TAF').replace('FM', '<br>FM').replace('TEMPO', '<br>TEMPO').replace('PROB', '<br>PROB')}
    </div>
    """, unsafe_allow_html=True)

st.divider()

if data and "hourly" in data:
    h = data["hourly"]
    times = [datetime.fromisoformat(t).strftime("%d %b %H:%M Z") for t in h["time"]]
    selected_time = st.sidebar.select_slider("Forecast Hour:", options=times)
    idx = times.index(selected_time)
    
    # Core Metrics
    w10 = h['wind_speed_10m'][idx]
    gst = h['wind_gusts_10m'][idx]
    
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("WIND (10m)", f"{safe_val(w10)} kt")
    m2.metric("GUSTS", f"{safe_val(gst)} kt")
    m3.metric("FREEZING LVL", f"{safe_val(h['freezing_level_height'][idx], 3.28084)} ft")
    # VISIBILITY IN KM: Multiply meters by 0.001
    m4.metric("VISIBILITY", f"{safe_val(h['visibility'][idx], 0.001, precision=1)} km")

    # --- HAZARD STACK ---
    st.subheader("Tactical Hazard Stack (Estimated AGL Winds)")
    upper_v, upper_h = get_best_upper_wind(h, idx)
    
    if w10 is not None and upper_v is not None:
        stack = []
        gst_factor = gst / max(w10, 1)
        for alt in [400, 300, 200, 100]:
            alt_m = alt * 0.3048
            spd = w10 + (upper_v - w10) * (math.log(alt_m/10) / math.log(upper_h/10))
            cur_gst = spd * gst_factor
            
            status = "NOMINAL"
            if cur_gst > 25: status = "NO-GO (GUST)"
            elif spd > 20: status = "CAUTION (WIND)"
            
            stack.append({"Alt (AGL)": f"{alt}ft", "Wind (kt)": int(spd), "Gust (kt)": int(cur_gst), "Status": status})
        st.table(pd.DataFrame(stack))
    else:
        st.warning(f"Note: {model_choice} upper-air data is unavailable at this specific coordinate.")

    # --- SKEW-T ---
    st.divider()
    p_levs = [1000, 950, 925, 900, 850, 800, 700, 600, 500, 400]
    t_plot = [h.get(f'temperature_{p}hPa')[idx] for p in p_levs]
    td_plot = [h.get(f'dewpoint_{p}hPa')[idx] for p in p_levs]

    if None not in t_plot:
        fig = plt.figure(figsize=(6, 8))
        fig.patch.set_facecolor('#0E1117')
        skew = SkewT(fig, rotation=45)
        skew.ax.set_facecolor('#1B1E23')
        skew.plot(p_levs, np.array(t_plot) * units.degC, 'r', linewidth=2, label="Temp")
        skew.plot(p_levs, np.array(td_plot) * units.degC, 'g', linewidth=2, label="Dewpoint")
        plt.title(f"Vertical Profile (UTC: {selected_time})", color='white')
        st.pyplot(fig)
