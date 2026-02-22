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

# CUSTOM CSS: STEALTH THEME + DYNAMIC HIGHLIGHTS
st.markdown("""
    <style>
    [data-testid="stMetricValue"] { font-size: 1.2rem !important; color: #E58E26 !important; }
    [data-testid="stMetricLabel"] { font-size: 0.8rem !important; color: #A0A4AB !important; text-transform: uppercase; }
    .ifr-text { color: #ff4b4b; font-weight: bold; } /* Red for IFR */
    .mvfr-text { color: #f6ec15; font-weight: bold; } /* Yellow for MVFR */
    .fz-warn { background-color: #ff4b4b; color: white; padding: 2px; border-radius: 3px; font-weight: bold; }
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

# 3. HIGHLIGHTING LOGIC
def apply_tactical_highlights(text):
    if not text: return ""
    
    # 1. Freezing Precipitation (Red Background)
    text = re.sub(r'\b(FZRA|FZDZ|PL|FZFG)\b', r'<span class="fz-warn">\1</span>', text)
    
    # 2. Visibility Highlights (Handles "1 1/2SM", "3SM", "1/4SM")
    def vis_match(m):
        try:
            s = m.group(0).replace('SM', '').strip()
            if ' ' in s:
                parts = s.split()
                val = float(parts[0]) + float(eval(parts[1]))
            elif '/' in s:
                val = float(eval(s))
            else:
                val = float(s)
            
            if val < 3: return f'<span class="ifr-text">{m.group(0)}</span>'
            if 3 <= val <= 5: return f'<span class="mvfr-text">{m.group(0)}</span>'
        except: pass
        return m.group(0)
    
    text = re.sub(r'\b(?:\d+\s+)?(?:\d+/\d+|\d+)SM\b', vis_match, text)
    
    # 3. Ceiling Highlights (BKN/OVC/VV)
    def sky_match(m):
        try:
            h = int(m.group(2)) * 100
            if h < 1000: return f'<span class="ifr-text">{m.group(0)}</span>'
            if 1000 <= h <= 3000: return f'<span class="mvfr-text">{m.group(0)}</span>'
        except: pass
        return m.group(0)
    text = re.sub(r'\b(BKN|OVC|VV)(\d{3})\b', sky_match, text)
    
    return text

# 4. DATA FETCHING
@st.cache_data(ttl=300)
def get_aviation_weather(station):
    station = station.strip().upper()
    API_KEY = "c453505478304bbbae7761f99c8a84ba" 
    headers = {"X-API-Key": API_KEY}
    try:
        m_url = f"https://api.checkwx.com/metar/{station}/decoded?count=3"
        t_url = f"https://api.checkwx.com/taf/{station}/decoded"
        
        m_res = requests.get(m_url, headers=headers, timeout=10).json()
        t_res = requests.get(t_url, headers=headers, timeout=10).json()
        
        metars = []
        if m_res.get('data'):
            for report in m_res['data']:
                raw = report.get('raw_text', '')
                raw = apply_tactical_highlights(raw)
                if "SPECI" in raw:
                    raw = raw.replace("SPECI", '<span style="color: #E58E26; font-weight: bold;">SPECI</span>')
                metars.append(raw)
        
        final_metar = "<br>".join(metars) if metars else "STATION INACTIVE / NO DATA"
        
        taf_raw = t_res['data'][0].get('raw_text', "NO ACTIVE TAF") if t_res.get('data') else "NO ACTIVE TAF"
        taf_highlighted = apply_tactical_highlights(taf_raw)
        
        # Correctly capture FM followed by 6 digits, plus standard groups
        taf_final = re.sub(r'\b(FM\d{6}|TEMPO|PROB\d{2}|BECMG)\b', r'<br><b>\1</b>', taf_highlighted)
        
        return final_metar, taf_final
    except Exception as e:
        return f"API ERROR: {str(e)[:15]}", "CONNECTION ERROR"

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

def safe_val(val, multiplier=1, default="N/A", precision=0):
    if val is None: return default
    res = val * multiplier
    return f"{res:,.{precision}f}" if precision > 0 else f"{int(round(res)):,}"

# 5. MAIN RENDER
st.title("Atmospheric Risk Management")
st.caption("Vector Check Aerial Group Inc. | Specialized Drone Operations & Weather Consulting")

current_hour_key = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H")
data = fetch_mission_data(lat, lon, model_api_map[model_choice], current_hour_key)
metar_raw, taf_raw = get_aviation_weather(icao)

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
    
    temp = h['temperature_2m'][idx]
    hum  = h['relative_humidity_2m'][idx]
    w_dir_raw = h['wind_direction_10m'][idx]
    w_spd = h['wind_speed_10m'][idx]
    wx_code = h['weather_code'][idx]
    w_dir_display = str(int(w_dir_raw)).zfill(3) if w_dir_raw is not None else "N/A"
    
    frz_raw = h.get('freezing_level_height', [None]*len(h['time']))[idx]
    frz_display = "SFC" if temp is not None and temp <= 0 else (f"{int(round(frz_raw * 3.28084, -2)):,} ft" if frz_raw else "N/A")
    
    cols = st.columns(8)
    cols[0].metric("Temp", f"{temp}°C")
    cols[1].metric("RH", f"{hum}%")
    cols[2].metric("Wind Dir", f"{w_dir_display}°")
    cols[3].metric("Wind Spd", f"{int(w_spd)} kt" if w_spd is not None else "N/A")
    cols[4].metric("Wx Code", f"{wx_code}")
    cols[5].metric("Vis (Est)", f"{int((100 - hum) / 5 * 1.13)} sm" if hum is not None else "N/A")
    cols[6].metric("Freezing", frz_display)
    
    if temp is not None and hum is not None:
        c_base = f"{int((temp - (temp - ((100-hum)/5)))*122*3.28)} ft"
    else:
        c_base = "N/A"
    cols[7].metric("Cloud Base", c_base)

    st.subheader("Tactical Hazard Stack")
    gst = h['wind_gusts_10m'][idx]
    
    upper_v, upper_h = None, None
    for height_key, height_val in [('wind_speed_120m', 120), ('wind_speed_100m', 100), ('wind_speed_80m', 80)]:
        if h.get(height_key) and h[height_key][idx] is not None:
            upper_v, upper_h = h[height_key][idx], height_val
            break
    
    if w_spd is not None and upper_v is not None and gst is not None:
        stack = []
        gst_factor = gst / max(w_spd, 1)
        for alt in [400, 300, 200, 100]:
            alt_m = alt * 0.3048
            spd = w_spd + (upper_v - w_spd) * (math.log(alt_m/10) / math.log(upper_h/10))
            cur_gst = spd * gst_factor
            status = "NOMINAL"
            if cur_gst > 25: status = "NO-GO (GUST)"
            elif spd > 20: status = "CAUTION (WIND)"
            stack.append({"Alt (AGL)": f"{alt}ft", "Wind (kt)": int(spd), "Gust (kt)": int(cur_gst), "Status": status})
        st.table(pd.DataFrame(stack))

    st.divider()
    
    p_levs = [1000, 950, 925, 900, 850, 800, 700, 600, 500, 400]
    t_plot = [h.get(f'temperature_{p}hPa')[idx] for p in p_levs]
    td_plot = [h.get(f'dewpoint_{p}hPa')[idx] for p in p_levs]
    
    if None not in t_plot:
        fig = plt.figure(figsize=(6, 8))
        fig.patch.set_facecolor('#0E1117')
        skew = SkewT(fig, rotation=45)
        skew.ax.set_facecolor('#1B1E23')
        skew.plot(p_levs, np.array(t_plot) * units.degC, 'r', linewidth=2)
        skew.plot(p_levs, np.array(td_plot) * units.degC, 'g', linewidth=2)
        plt.title(f"Vertical Profile (UTC: {selected_time})", color='white')
        st.pyplot(fig)
