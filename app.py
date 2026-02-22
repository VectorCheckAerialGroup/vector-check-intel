import streamlit as st
import requests
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from metpy.plots import SkewT
from metpy.units import units
import io
import math
import re
from datetime import datetime, timezone, timedelta

# 1. PAGE CONFIG
st.set_page_config(page_title="Vector Check: Atmospheric Risk Management", layout="wide")

# CUSTOM CSS: STEALTH THEME
st.markdown("""
    <style>
    [data-testid="stMetricValue"] { font-size: 1.2rem !important; color: #E58E26 !important; }
    [data-testid="stMetricLabel"] { font-size: 0.8rem !important; color: #A0A4AB !important; text-transform: uppercase; }
    table { margin-left: auto; margin-right: auto; text-align: center !important; width: 90%; border-collapse: collapse; background-color: #1B1E23; }
    th { text-align: center !important; color: #8E949E !important; font-weight: bold !important; padding: 10px !important; border-bottom: 2px solid #3E444E !important; text-transform: uppercase; }
    td { text-align: center !important; padding: 8px !important; color: #D1D5DB !important; border-bottom: 1px solid #2D3139 !important; }
    </style>
    """, unsafe_allow_html=True)

# 2. SIDEBAR & LOGO
LOGO_URL = "https://raw.githubusercontent.com/VectorCheck/vector-check-intel/main/VCAG%20Inc%20-%20Logo%20Final.png"
try:
    st.sidebar.image(LOGO_URL, use_container_width=True)
except Exception:
    st.sidebar.title("Vector Check")
    st.sidebar.caption("Aerial Group Inc.")

st.sidebar.header("Mission Parameters")
lat = st.sidebar.number_input("Latitude", value=44.1628, format="%.4f")
lon = st.sidebar.number_input("Longitude", value=-77.3832, format="%.4f")
icao = st.sidebar.text_input("Nearest ICAO", value="CYTR").upper().strip()

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
    # Search for the highest resolution boundary layer wind available
    for key, height in [('wind_speed_120m', 120), ('wind_speed_100m', 100), ('wind_speed_80m', 80)]:
        val_list = h_data.get(key)
        if val_list and val_list[idx] is not None:
            return val_list[idx], height
    return None, None

def estimate_tactical_visibility(temp, rh, weather_code):
    if temp is None or rh is None: return 10.0
    dp_dep = (100 - rh) / 5
    vis_est = max(0.1, dp_dep * 1.13)
    if weather_code is not None:
        if weather_code in [65, 66, 67, 95]: vis_est = min(vis_est, 1.5)
        elif weather_code in [73, 75, 85, 86]: vis_est = min(vis_est, 0.75)
        elif weather_code in [51, 61, 80, 71]: vis_est = min(vis_est, 4.0)
        elif weather_code in [45, 48]: vis_est = min(vis_est, 0.25)
    return min(10.0, vis_est)

# 4. DATA FETCHING (48H SLIDING WINDOW & CHECK-WX)
@st.cache_data(ttl=600)
def fetch_mission_data(latitude, longitude, model_url, time_key):
    hourly_params = [
        "temperature_2m", "relative_humidity_2m", "wind_speed_10m", "wind_gusts_10m",
        "wind_direction_10m", "visibility", "weather_code", "pressure_msl",
        "wind_speed_80m", "wind_speed_120m", "wind_speed_100m", "freezing_level_height", "cloud_cover"
    ]
    p_levels = [1000, 950, 925, 900, 850, 800, 700, 600, 500, 400]
    hourly_params += [f"temperature_{p}hPa" for p in p_levels] + [f"dewpoint_{p}hPa" for p in p_levels]
    
    params = {
        "latitude": latitude, "longitude": longitude, "hourly": hourly_params, 
        "wind_speed_unit": "kn", "forecast_hours": 48, "past_hours": 0, "timezone": "UTC"
    }
    try:
        res = requests.get(model_url, params=params, timeout=15)
        res.raise_for_status()
        return res.json()
    except: return None

@st.cache_data(ttl=300)
def get_aviation_weather(station):
    station = station.strip().upper()
    API_KEY = "c453505478304bbbae7761f99c8a84ba" 
    headers = {"X-API-Key": API_KEY}
    try:
        # Pulling the last 3 reports to ensure SPECIs are captured
        m_url = f"https://api.checkwx.com/metar/{station}/decoded?count=3"
        t_url = f"https://api.checkwx.com/taf/{station}/decoded"
        
        m_res = requests.get(m_url, headers=headers, timeout=10)
        t_res = requests.get(t_url, headers=headers, timeout=10)
        
        m_data = m_res.json()
        metars = []
        if m_data.get('data'):
            for report in m_data['data']:
                raw = report.get('raw_text', '')
                # Met Tech: Highlight SPECI for tactical awareness
                if "SPECI" in raw:
                    raw = raw.replace("SPECI", '<span style="color: #E58E26; font-weight: bold;">SPECI</span>')
                metars.append(raw)
        
        final_metar = "<br>".join(metars) if metars else "STATION INACTIVE / NO DATA"
        
        t_data = t_res.json()
        final_taf = t_data['data'][0].get('raw_text', "NO ACTIVE TAF") if t_data.get('data') else "NO ACTIVE TAF"
        
        return final_metar, final_taf
    except Exception as e:
        return f"API ERROR: {str(e)[:15]}", "CONNECTION ERROR"

# 5. MAIN RENDER
st.title("Atmospheric Risk Management")
st.caption("Vector Check Aerial Group Inc. | Specialized Drone Operations & Weather Consulting")

# Cache-busting key based on current hour
current_hour_key = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H")
data = fetch_mission_data(lat, lon, model_api_map[model_choice], current_hour_key)

metar_raw, taf_raw = get_aviation_weather(icao)

# Observation Display
st.markdown(f"""
    <div style="background-color: #1B1E23; padding: 15px; border: 1px solid #2D3139; border-radius: 5px; font-family: monospace; color: #D1D5DB; font-size: 0.85rem; line-height: 1.5;">
        <strong style="color: #8E949E; text-transform: uppercase; font-family: sans-serif;">Observations (Last 3)</strong><br>
        {metar_raw}<br><br>
        <strong style="color: #8E949E; text-transform: uppercase; font-family: sans-serif;">TAF</strong><br>
        {taf_raw}
    </div>
    """, unsafe_allow_html=True)

st.divider()

if data and "hourly" in data:
    h = data["hourly"]
    times = [datetime.fromisoformat(t).strftime("%d %b %H:%M Z") for t in h["time"]]
    selected_time = st.sidebar.select_slider("Forecast Hour (48h Window):", options=times, value=times[0])
    idx = times.index(selected_time)
    
    # Process Metrics
    temp = h['temperature_2m'][idx]
    hum  = h['relative_humidity_2m'][idx]
    w_dir_raw = h['wind_direction_10m'][idx]
    w_spd = h['wind_speed_10m'][idx]
    wx_code = h['weather_code'][idx]
    
    w_dir_display = str(int(w_dir_raw)).zfill(3) if w_dir_raw is not None else "N/A"
    vis_sm = estimate_tactical_visibility(temp, hum, wx_code)
    
    # Freezing Level Logic
    frz_raw = h.get('freezing_level_height', [None]*len(h['time']))[idx]
    if temp is not None and temp <= 0: frz_display = "SFC"
    elif frz_raw is not None:
        frz_ft = frz_raw * 3.28084
        frz_display = "SFC" if frz_ft < 50 else f"{int(round(frz_ft, -2)):,} ft"
    else: frz_display = "N/A"
    
    # Estimated LCL (Cloud Base)
    c_base = "Clear" if hum < 55 else f"{int(round((temp - (temp - ((100 - hum)/5))) * 122 * 3.28084, -2))} ft"

    cols = st.columns(8)
    cols[0].metric("Temp", f"{safe_val(temp, precision=1)}°C")
    cols[1].metric("RH", f"{safe_val(hum)}%")
    cols[2].metric("Wind Dir", f"{w_dir_display}°")
    cols[3].metric("Wind Spd", f"{safe_val(w_spd)} kt")
    cols[4].metric("Wx Code", f"{wx_code}")
    cols[5].metric("Vis (Est)", f"{safe_val(vis_sm, precision=1)} sm")
    cols[6].metric("Freezing", frz_display)
    cols[7].metric("Cloud Base", c_base)

    st.subheader("Tactical Hazard Stack")
    gst = h['wind_gusts_10m'][idx]
    upper_v, upper_h = get_best_upper_wind(h, idx)
    
    if w_spd is not None and upper_v is not None and gst is not None:
        stack = []
        gst_factor = gst / max(w_spd, 1)
        for alt in [400, 300, 200, 100]:
            alt_m = alt * 0.3048
            # Logarithmic wind profile for AGL estimation
            spd = w_spd + (upper_v - w_spd) * (math.log(alt_m/10) / math.log(upper_h/10))
            cur_gst = spd * gst_factor
            status = "NOMINAL"
            if cur_gst > 25: status = "NO-GO (GUST)"
            elif spd > 20: status = "CAUTION (WIND)"
            stack.append({"Alt (AGL)": f"{alt}ft", "Wind (kt)": int(spd), "Gust (kt)": int(cur_gst), "Status": status})
        st.table(pd.DataFrame(stack))
    else:
        st.warning("Upper-air wind data unavailable for this timestamp.")

    st.divider()
    
    # Skew-T Vertical Profile
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
else:
    st.error("Unable to retrieve forecast model data. Please check coordinates.")
