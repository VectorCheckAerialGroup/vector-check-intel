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

# CUSTOM CSS: TACTICAL TRIAGE COLORS
st.markdown("""
    <style>
    [data-testid="stMetricValue"] { font-size: 1.2rem !important; color: #E58E26 !important; }
    [data-testid="stMetricLabel"] { font-size: 0.8rem !important; color: #A0A4AB !important; text-transform: uppercase; }
    .vfr { color: #00e676; font-weight: bold; } /* Green */
    .ifr { color: #ff4b4b; font-weight: bold; } /* Red */
    .default-cat { color: #f6ec15; font-weight: bold; } /* Yellow for MVFR/Others */
    table { width: 100%; border-collapse: collapse; background-color: #1B1E23; }
    th { color: #8E949E !important; padding: 10px; border-bottom: 2px solid #3E444E; text-transform: uppercase; }
    td { padding: 8px; color: #D1D5DB; border-bottom: 1px solid #2D3139; text-align: center; }
    .obs-box { background-color: #1B1E23; padding: 15px; border: 1px solid #2D3139; border-radius: 5px; font-family: monospace; color: #D1D5DB; font-size: 0.85rem; line-height: 1.6; }
    </style>
    """, unsafe_allow_html=True)

# 2. SIDEBAR & LOGO
LOGO_URL = "https://raw.githubusercontent.com/VectorCheck/vector-check-intel/main/VCAG%20Inc%20-%20Logo%20Final.png"
try:
    st.sidebar.image(LOGO_URL, use_container_width=True)
except Exception:
    st.sidebar.title("Vector Check")

st.sidebar.header("Mission Parameters")
lat = st.sidebar.number_input("Latitude", value=44.1628, format="%.4f")
lon = st.sidebar.number_input("Longitude", value=-77.3832, format="%.4f")
icao = st.sidebar.text_input("Nearest ICAO", value="CYTR").upper().strip()

# 3. FLIGHT CATEGORY LOGIC (STRICT RED/GREEN/YELLOW)
def get_flight_cat(vis, ceiling):
    try:
        vis = float(vis)
        ceiling = float(ceiling)
        if vis > 5 and ceiling > 3000: return "VFR", "vfr"
        if vis < 3 or ceiling < 1000: return "IFR", "ifr"
        return "MVFR", "default-cat"
    except: return "UNK", "default-cat"

# 4. DATA FETCHING
@st.cache_data(ttl=300)
def get_aviation_weather(station):
    API_KEY = "c453505478304bbbae7761f99c8a84ba" 
    headers = {"X-API-Key": API_KEY}
    try:
        # Requesting last 3 reports
        m_url = f"https://api.checkwx.com/metar/{station}/decoded?count=3"
        t_url = f"https://api.checkwx.com/taf/{station}/decoded"
        
        m_res = requests.get(m_url, headers=headers, timeout=10).json()
        t_res = requests.get(t_url, headers=headers, timeout=10).json()
        
        # 4a. Process METARs
        metar_lines = []
        if m_res.get('data'):
            for report in m_res['data']:
                raw = report.get('raw_text', '')
                # Extracting Ceiling (handling multiple layers)
                ceiling = 10000
                if report.get('ceiling'):
                    ceiling = report['ceiling'].get('feet', 10000)
                elif report.get('clouds'):
                    # Fallback to lowest BKN or OVC layer
                    layers = [c.get('base', 10000) for c in report['clouds'] if c.get('code') in ['BKN', 'OVC', 'VV']]
                    if layers: ceiling = min(layers)
                
                vis = report.get('visibility', {}).get('miles_float', 10)
                cat_label, cat_class = get_flight_cat(vis, ceiling)
                
                metar_lines.append(f"<span class='{cat_class}'>[{cat_label}]</span> {raw}")
        
        metar_html = "<br>".join(metar_lines) if metar_lines else "STATION INACTIVE"

        # 4b. Process TAF with change group line breaks
        taf_raw = t_res['data'][0].get('raw_text', "NO ACTIVE TAF") if t_res.get('data') else "NO ACTIVE TAF"
        # Using Regex to find FM, TEMPO, PROB, BECMG and insert a line break and bolding
        taf_formatted = re.sub(r'\b(FM|TEMPO|PROB\d{2}|BECMG)\b', r'<br><b>\1</b>', taf_raw)
        
        return metar_html, taf_formatted
    except:
        return "DATA LINK ERROR", "DATA LINK ERROR"

@st.cache_data(ttl=600)
def fetch_mission_data(latitude, longitude, time_key):
    params = {
        "latitude": latitude, "longitude": longitude,
        "hourly": ["temperature_2m", "relative_humidity_2m", "wind_speed_10m", "wind_gusts_10m",
                   "wind_direction_10m", "visibility", "weather_code", "wind_speed_80m", 
                   "wind_speed_120m", "freezing_level_height", "pressure_msl"],
        "wind_speed_unit": "kn", "forecast_hours": 48, "past_hours": 0, "timezone": "UTC"
    }
    res = requests.get("https://api.open-meteo.com/v1/gem", params=params, timeout=15)
    return res.json() if res.status_code == 200 else None

# 5. UI RENDER
st.title("Atmospheric Risk Management")
st.caption("Vector Check Aerial Group Inc. | Specialized Drone Operations & Weather Consulting")

current_hour_key = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H")
data = fetch_mission_data(lat, lon, current_hour_key)
metar_html, taf_html = get_aviation_weather(icao)

st.markdown(f"""
    <div class="obs-box">
        <strong style="color: #8E949E; text-transform: uppercase; font-family: sans-serif;">Observations (Last 3)</strong><br>
        {metar_html}<br><br>
        <strong style="color: #8E949E; text-transform: uppercase; font-family: sans-serif;">TAF</strong><br>
        {taf_html}
    </div>
    """, unsafe_allow_html=True)

st.divider()

if data and "hourly" in data:
    h = data["hourly"]
    times = [datetime.fromisoformat(t).strftime("%d %b %H:%M Z") for t in h["time"]]
    selected_time = st.sidebar.select_slider("Forecast Hour (48h Window):", options=times, value=times[0])
    idx = times.index(selected_time)
    
    # METRICS
    cols = st.columns(4)
    cols[0].metric("Surface Temp", f"{h['temperature_2m'][idx]}°C")
    cols[1].metric("Rel. Humidity", f"{h['relative_humidity_2m'][idx]}%")
    cols[2].metric("Wind", f"{int(h['wind_direction_10m'][idx]):03d} / {int(h['wind_speed_10m'][idx])} kt")
    
    frz = h['freezing_level_height'][idx]
    frz_txt = f"{int(frz * 3.28)} ft" if frz else "SFC"
    cols[3].metric("Freezing Level", frz_txt)

    # HAZARD STACK
    st.subheader("Tactical Hazard Stack (AGL Estimations)")
    w_spd = h['wind_speed_10m'][idx]
    upper_v = h['wind_speed_120m'][idx] or h['wind_speed_80m'][idx]
    gst = h['wind_gusts_10m'][idx]
    
    if w_spd is not None and upper_v is not None:
        stack = []
        gst_factor = gst / max(w_spd, 1)
        for alt in [400, 300, 200, 100]:
            alt_m = alt * 0.3048
            # Logarithmic wind profile
            spd = w_spd + (upper_v - w_spd) * (math.log(alt_m/10) / math.log(120/10))
            cur_gst = spd * gst_factor
            status = "NOMINAL"
            if cur_gst > 25: status = "NO-GO (GUST)"
            elif spd > 20: status = "CAUTION (WIND)"
            stack.append({"Alt (AGL)": f"{alt}ft", "Wind (kt)": int(spd), "Gust (kt)": int(cur_gst), "Status": status})
        st.table(pd.DataFrame(stack))
