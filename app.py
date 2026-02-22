import streamlit as st
import requests
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from metpy.plots import SkewT
from metpy.units import units
import math
import re
from datetime import datetime, timezone

# 1. PAGE CONFIG
st.set_page_config(page_title="Vector Check: Atmospheric Risk Management", layout="wide")

# CUSTOM CSS: STEALTH THEME
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
    </style>
    """, unsafe_allow_html=True)

# 2. SIDEBAR
st.sidebar.header("Mission Parameters")
lat = st.sidebar.number_input("Latitude", value=44.1628, format="%.4f")
lon = st.sidebar.number_input("Longitude", value=-77.3832, format="%.4f")
icao = st.sidebar.text_input("Nearest ICAO", value="CYTR").upper().strip()

model_choice = st.sidebar.selectbox("Select Forecast Model:", options=["HRDPS (Canada 2.5km)", "ECMWF (Global 9km)"])

model_api_map = {
    "HRDPS (Canada 2.5km)": "https://api.open-meteo.com/v1/gem",
    "ECMWF (Global 9km)": "https://api.open-meteo.com/v1/ecmwf"
}

# 3. HELPERS
def apply_tactical_highlights(text):
    if not text: return ""
    text = re.sub(r'\b(FZRA|FZDZ|PL|FZFG)\b', r'<span class="fz-warn">\1</span>', text)
    def vis_match(m):
        try:
            s = m.group(0).replace('SM', '').strip()
            val = float(eval(s)) if '/' in s else float(s)
            if val < 3: return f'<span class="ifr-text">{m.group(0)}</span>'
            if 3 <= val <= 5: return f'<span class="mvfr-text">{m.group(0)}</span>'
        except: pass
        return m.group(0)
    text = re.sub(r'\b(?:\d+\s+)?(?:\d+/\d+|\d+)SM\b', vis_match, text)
    def sky_match(m):
        try:
            h = int(m.group(2)) * 100
            if h < 1000: return f'<span class="ifr-text">{m.group(0)}</span>'
            if 1000 <= h <= 3000: return f'<span class="mvfr-text">{m.group(0)}</span>'
        except: pass
        return m.group(0)
    text = re.sub(r'\b(BKN|OVC|VV)(\d{3})\b', sky_match, text)
    return text

def get_precip_type(code):
    if code is None: return "N/A"
    if code in [0, 1, 2, 3, 45, 48]: return "None"
    if code in [51, 53, 55, 61, 63, 65, 80, 81, 82, 95]: return "Rain"
    if code in [56, 57, 66, 67]: return "Freezing Rain"
    if code in [71, 73, 75, 77, 85, 86]: return "Snow"
    return "Mixed"

# 4. DATA FETCHING
@st.cache_data(ttl=300)
def get_aviation_weather(station):
    API_KEY = "c453505478304bbbae7761f99c8a84ba" 
    headers = {"X-API-Key": API_KEY}
    try:
        m_res = requests.get(f"https://api.checkwx.com/metar/{station}/decoded?count=3", headers=headers, timeout=10).json()
        t_res = requests.get(f"https://api.checkwx.com/taf/{station}/decoded", headers=headers, timeout=10).json()
        metars = [apply_tactical_highlights(r.get('raw_text', '')) for r in m_res.get('data', [])]
        taf_raw = t_res['data'][0].get('raw_text', "NO ACTIVE TAF") if t_res.get('data') else "NO ACTIVE TAF"
        taf_final = re.sub(r'\b(FM\d{6}|TEMPO|PROB\d{2}|BECMG)\b', r'<br><b>\1</b>', apply_tactical_highlights(taf_raw))
        return "<br>".join(metars) if metars else "NO DATA", taf_final
    except: return "CONN ERROR", "CONN ERROR"

@st.cache_data(ttl=600)
def fetch_mission_data(lat, lon, model_url):
    p_levels = [1000, 950, 925, 900, 850, 800, 700, 600, 500, 400]
    # Standard variables
    hourly = ["temperature_2m", "relative_humidity_2m", "wind_speed_10m", "wind_direction_10m", "weather_code", "freezing_level_height"]
    # Model-specific logic for Hazard Stack
    if "gem" in model_url:
        hourly += ["wind_gusts_10m", "wind_speed_80m", "wind_speed_120m"]
    else: # ECMWF
        hourly += ["wind_speed_100m"] # Gusts not supported in open ECMWF hourly
    
    hourly += [f"temperature_{p}hPa" for p in p_levels] + [f"dewpoint_{p}hPa" for p in p_levels]
    
    params = {"latitude": lat, "longitude": lon, "hourly": hourly, "wind_speed_unit": "kn", "forecast_hours": 48, "timezone": "UTC"}
    res = requests.get(model_url, params=params)
    return res.json() if res.status_code == 200 else None

# 5. RENDER
st.title("Atmospheric Risk Management")
st.caption("Vector Check Aerial Group Inc.")

data = fetch_mission_data(lat, lon, model_api_map[model_choice])
metar_raw, taf_raw = get_aviation_weather(icao)

st.markdown(f'<div style="background-color: #1B1E23; padding: 15px; border-radius: 5px;"><div class="obs-text"><strong style="color: #8E949E;">METAR/SPECI</strong><br>{metar_raw}<br><br><strong style="color: #8E949E;">TAF</strong><br>{taf_raw}</div></div>', unsafe_allow_html=True)
st.divider()

if data and "hourly" in data:
    h = data["hourly"]
    times = [datetime.fromisoformat(t).strftime("%d %b %H:%M Z") for t in h["time"]]
    selected_time = st.sidebar.select_slider("Forecast Hour:", options=times, value=times[0])
    idx = times.index(selected_time)
    
    t, rh, w_spd, wx = h['temperature_2m'][idx], h['relative_humidity_2m'][idx], h['wind_speed_10m'][idx], h['weather_code'][idx]
    frz_raw = h.get('freezing_level_height', [None]*len(h['time']))[idx]
    frz_display = "SFC" if t <= 0 else (f"{int(round(frz_raw * 3.28, -2)):,} ft" if frz_raw else "N/A")

    c = st.columns(8)
    c[0].metric("Temp", f"{t}°C"); c[1].metric("RH", f"{rh}%"); c[2].metric("Wind Dir", f"{int(h['wind_direction_10m'][idx]):03d}°")
    c[3].metric("Wind Spd", f"{int(w_spd)} kt"); c[4].metric("Precip Type", get_precip_type(wx))
    c[5].metric("Vis (Est)", f"{int((100-rh)/5 * 1.13)} sm"); c[6].metric("Freezing LVL", frz_display)
    c[7].metric("Cloud Base", f"{int((t - (t - ((100-rh)/5)))*400)} ft")

    st.subheader("Tactical Hazard Stack")
    # Handling ECMWF missing Gust/Upper Wind
    gst = h.get('wind_gusts_10m', [w_spd * 1.3]*len(h['time']))[idx] # Fallback to 1.3x wind if gust missing
    upper_v = h.get('wind_speed_120m', h.get('wind_speed_100m', [w_spd*1.5]*len(h['time'])))[idx]
    upper_h = 120 if h.get('wind_speed_120m') else 100

    stack = []
    for alt in [400, 300, 200, 100]:
        spd = w_spd + (upper_v - w_spd) * (math.log(alt*0.3/10) / math.log(upper_h/10))
        cur_gst = spd * (gst / max(w_spd, 1))
        status = "NO-GO (GUST)" if cur_gst > 25 else ("CAUTION (WIND)" if spd > 20 else "NOMINAL")
        stack.append({"Alt (AGL)": f"{alt}ft", "Wind (kt)": int(spd), "Gust (kt)": int(cur_gst), "Status": status})
    st.table(pd.DataFrame(stack))
    
    if "ECMWF" in model_choice:
        st.caption("⚠️ NOTE: ECMWF does not provide gust data. Gusts are estimated based on a standard 1.3x factor.")
