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

# CUSTOM CSS: STEALTH THEME + DYNAMIC HIGHLIGHTS
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

model_choice = st.sidebar.selectbox("Select Forecast Model:", options=["HRDPS (Canada 2.5km)", "ECMWF (Global 9km)"])
terrain_type = st.sidebar.selectbox("Terrain Roughness:", options=["Land", "Water", "Mountains"])

# REFRESH TOOL
if st.sidebar.button("Force Manual Data Refresh"):
    st.cache_data.clear()
    st.sidebar.success("Cache Cleared. Fetching Live Data...")

model_api_map = {
    "HRDPS (Canada 2.5km)": "https://api.open-meteo.com/v1/gem",
    "ECMWF (Global 9km)": "https://api.open-meteo.com/v1/ecmwf"
}

# 3. HIGHLIGHTING HELPERS
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
    if code is None: return "None"
    if code in [0, 1, 2, 3, 45, 48]: return "None"
    if code in [51, 53, 55, 61, 63, 65, 80, 81, 82, 95]: return "Rain"
    if code in [56, 57, 66, 67]: return "Freezing Rain"
    if code in [71, 73, 75, 77, 85, 86]: return "Snow"
    return "Mixed"

# 4. TACTICAL DRONE ICING LOGIC (TDL)
def calculate_icing_profile(hourly_data, idx, wx_code):
    p_levels = [1000, 950, 925, 900, 850, 800, 700, 600, 500, 400]
    profile = []
    for p in p_levels:
        t_val = hourly_data.get(f"temperature_{p}hPa")[idx]
        td_val = hourly_data.get(f"dewpoint_{p}hPa")[idx]
        h_m = hourly_data.get(f"geopotential_height_{p}hPa")[idx]
        if t_val is not None and td_val is not None and h_m is not None:
            profile.append({"p": p, "t": t_val, "td": td_val, "h_ft": h_m * 3.28084})
    
    cloud_layers = []
    current_layer = {"base": None, "top": None, "min_t": 100, "max_t": -100, "inversion": False}
    
    for i, lvl in enumerate(profile):
        is_saturated = (lvl["t"] - lvl["td"]) <= 3.0
        if is_saturated:
            if current_layer["base"] is None: 
                current_layer["base"] = lvl["h_ft"]
                current_layer["bottom_t"] = lvl["t"]
            current_layer["top"] = lvl["h_ft"]
            current_layer["min_t"] = min(current_layer["min_t"], lvl["t"])
            current_layer["max_t"] = max(current_layer["max_t"], lvl["t"])
            if i > 0 and lvl["t"] > profile[i-1]["t"]: current_layer["inversion"] = True
        else:
            if current_layer["base"] is not None:
                current_layer["thickness"] = current_layer["top"] - current_layer["base"]
                cloud_layers.append(current_layer)
                current_layer = {"base": None, "top": None, "min_t": 100, "max_t": -100, "inversion": False}
    
    if current_layer["base"] is not None:
        current_layer["thickness"] = current_layer["top"] - current_layer["base"]
        cloud_layers.append(current_layer)

    icing_result = {"type": "NONE", "sev": "NONE", "base": 99999, "top": -99999}
    if wx_code in [66, 67, 56, 57]: 
        return {"type": "CLR (FZRA)", "sev": "SEV", "base": 0, "top": 10000}

    for layer in cloud_layers:
        if layer["max_t"] <= 0.5:
            i_type = "RIME"
            i_sev = "LGT"
            if layer["thickness"] > 1500 or layer["inversion"]:
                i_sev = "MOD"
                i_type = "MXD"
            if layer["thickness"] > 4000:
                i_sev = "SEV"
            icing_result = {"type": i_type, "sev": i_sev, "base": layer["base"], "top": layer["top"]}
            break
            
    return icing_result

# 5. DATA FETCHING
@st.cache_data(ttl=60)
def get_aviation_weather(station):
    API_KEY = "c453505478304bbbae7761f99c8a84ba" 
    headers = {"X-API-Key": API_KEY}
    try:
        m_res = requests.get(f"https://api.checkwx.com/metar/{station}/decoded?count=3", headers=headers, timeout=10)
        t_res = requests.get(f"https://api.checkwx.com/taf/{station}/decoded", headers=headers, timeout=10)
        if m_res.status_code == 200:
            m_data = m_res.json()
            metars = [apply_tactical_highlights(r.get('raw_text', '')) for r in m_data.get('data', [])]
            for i in range(len(metars)):
                if "SPECI" in metars[i]: metars[i] = metars[i].replace("SPECI", '<span style="color: #E58E26; font-weight: bold;">SPECI</span>')
            
            taf_final = "NO ACTIVE TAF"
            if t_res.status_code == 200:
                t_data = t_res.json().get('data', [])
                if t_data:
                    taf_raw = t_data[0].get('raw_text', "NO ACTIVE TAF")
                    taf_final = re.sub(r'\b(FM\d{6}|TEMPO|PROB\d{2}|BECMG)\b', r'<br><b>\1</b>', apply_tactical_highlights(taf_raw))
            return "<br>".join(metars) if metars else "NO DATA", taf_final
    except Exception: pass
    return "LINK FAILURE", "LINK FAILURE"

@st.cache_data(ttl=600)
def fetch_mission_data(lat, lon, model_url):
    p_levels = [1000, 950, 925, 900, 850, 800, 700, 600, 500, 400]
    hourly = ["temperature_2m", "relative_humidity_2m", "wind_speed_10m", "wind_direction_10m", "weather_code", "freezing_level_height"]
    
    if "gem" in model_url: 
        hourly += ["wind_gusts_10m", "wind_speed_80m", "wind_speed_120m", "wind_direction_80m", "wind_direction_120m"]
    else: 
        hourly += ["wind_speed_100m", "wind_direction_100m"]
        
    hourly += [f"temperature_{p}hPa" for p in p_levels] + [f"dewpoint_{p}hPa" for p in p_levels] + \
              [f"geopotential_height_{p}hPa" for p in p_levels] + \
              [f"wind_speed_{p}hPa" for p in p_levels] + [f"wind_direction_{p}hPa" for p in p_levels]
              
    params = {"latitude": lat, "longitude": lon, "hourly": hourly, "wind_speed_unit": "kn", "forecast_hours": 48, "timezone": "UTC"}
    res = requests.get(model_url, params=params)
    if res.status_code == 200: return res.json(), None
    else: return None, res.text

# 6. RENDER
st.title("Atmospheric Risk Management")
st.caption("Vector Check Aerial Group Inc.")

data, error_msg = fetch_mission_data(lat, lon, model_api_map[model_choice])
metar_raw, taf_raw = get_aviation_weather(icao)

st.markdown(f'<div style="background-color: #1B1E23; padding: 15px; border-radius: 5px;"><div class="obs-text"><strong style="color: #8E949E; font-family: sans-serif;">METAR/SPECI</strong><br>{metar_raw}<br><br><strong style="color: #8E949E; font-family: sans-serif;">TAF</strong><br>{taf_raw}</div></div>', unsafe_allow_html=True)
st.divider()

if error_msg:
    st.error(f"⚠️ TARGET DATA STREAM FAILED: {error_msg}")
elif data and "hourly" in data:
    h = data["hourly"]
    model_elev = data.get("elevation", 0)
    times = [datetime.fromisoformat(t).strftime("%d %b %H:%M Z") for t in h["time"]]
    selected_time = st.sidebar.select_slider("Forecast Hour:", options=times, value=times[0])
    idx = times.index(selected_time)
    
    t, rh, w_spd, wx = h['temperature_2m'][idx], h['relative_humidity_2m'][idx], h['wind_speed_10m'][idx], h['weather_code'][idx]
    td = t - ((100 - rh) / 5) if (t is not None and rh is not None) else t
    sfc_dir = int(h['wind_direction_10m'][idx])
    frz_raw = h.get('freezing_level_height', [None]*len(h['time']))[idx]
    frz_display = "SFC" if t <= 0 else (f"{int(round(frz_raw * 3.28, -2)):,} ft" if frz_raw else "N/A")
    c_base_ft = int((t - td)*400) if (t is not None and td is not None) else 10000

    # METRICS ROW
    c = st.columns(8)
    c[0].metric("Temp", f"{t}°C"); c[1].metric("RH", f"{rh}%"); c[2].metric("Wind Dir", f"{sfc_dir:03d}°")
    c[3].metric("Wind Spd", f"{int(w_spd)} kt"); c[4].metric("Precip Type", get_precip_type(wx))
    c[5].metric("Vis (Est)", f"{int((100-rh)/5 * 1.13)} sm"); c[6].metric("Freezing LVL", frz_display)
    c[7].metric("Cloud Base", f"{c_base_ft} ft")

    icing_cond = calculate_icing_profile(h, idx, wx)
    
    # WIND PROFILE SETUP (MODEL AGNOSTIC)
    raw_gust = h.get('wind_gusts_10m', [w_spd]*len(h['time']))[idx]
    gst = (w_spd * 1.25) if raw_gust <= w_spd else raw_gust
    
    if "gem" in model_api_map[model_choice]:
        upper_v, upper_dir, upper_h = h['wind_speed_120m'][idx], h['wind_direction_120m'][idx], 120
    else:
        upper_v, upper_dir, upper_h = h['wind_speed_100m'][idx], h['wind_direction_100m'][idx], 100

    # --- TABLES ---
    st.subheader("Tactical Hazard Stack (0-400ft AGL)")
    stack_tactical = []
    for alt in [400, 300, 200, 100]:
        spd_calc = w_spd + (upper_v - w_spd) * (math.log(alt*0.3048/10) / math.log(upper_h/10))
        gst_calc = spd_calc * (gst / max(w_spd, 1))
        diff = (upper_dir - sfc_dir + 180) % 360 - 180
        dir_calc = (sfc_dir + diff * (min(alt*0.3048, upper_h) / upper_h)) % 360
        
        ice_final = "NONE"
        if icing_cond["base"] <= alt <= icing_cond["top"]: ice_final = f"{icing_cond['sev']} {icing_cond['type']}"
        elif icing_cond["base"] == 0 and alt < icing_cond["top"]: ice_final = f"{icing_cond['sev']} {icing_cond['type']}"
        
        stack_tactical.append({"Alt (AGL)": f"{alt}ft", "Dir": f"{int(dir_calc):03d}°", "Spd (kt)": int(spd_calc), "Gust (kt)": int(gst_calc), "Icing": ice_final})
    st.table(pd.DataFrame(stack_tactical).set_index("Alt (AGL)"))

    st.subheader("Extended Trajectory (1,000-5,000ft AGL)")
    p_levels_traj = [1000, 950, 925, 900, 850, 800, 700, 600]
    p_profile = []
    for p in p_levels_traj:
        ws, wd, gh = h.get(f'wind_speed_{p}hPa'), h.get(f'wind_direction_{p}hPa'), h.get(f'geopotential_height_{p}hPa')
        if ws and ws[idx] is not None:
            p_profile.append({'h_ft': gh[idx] * 3.28084, 'spd': ws[idx], 'dir': wd[idx]})
    p_profile = sorted(p_profile, key=lambda x: x['h_ft'])

    stack_ext = []
    for alt in [5000, 4000, 3000, 2000, 1000]:
        pts = [{'h_ft': upper_h * 3.28084, 'spd': upper_v, 'dir': upper_dir}] + p_profile
        below, above = pts[0], pts[-1]
        for i in range(len(pts)-1):
            if pts[i]['h_ft'] <= alt <= pts[i+1]['h_ft']:
                below, above = pts[i], pts[i+1]
                break
        
        frac = (alt - below['h_ft']) / (above['h_ft'] - below['h_ft']) if above['h_ft'] != below['h_ft'] else 0
        spd_ext = below['spd'] + frac * (above['spd'] - below['spd'])
        diff_ext = (above['dir'] - below['dir'] + 180) % 360 - 180
        dir_ext = (below['dir'] + diff_ext * frac) % 360

        ice_ext = "NONE"
        if icing_cond["base"] <= alt <= icing_cond["top"]: ice_ext = f"{icing_cond['sev']} {icing_cond['type']}"
        elif icing_cond["base"] == 0 and alt < icing_cond["top"]: ice_ext = f"{icing_cond['sev']} {icing_cond['type']}"

        stack_ext.append({"Alt (AGL)": f"{alt}ft", "Dir": f"{int(dir_ext):03d}°", "Spd (kt)": int(spd_ext), "Icing": ice_ext})
    st.table(pd.DataFrame(stack_ext).set_index("Alt (AGL)"))

    st.divider()
    
    # SKEW-T
    p_levs_plot = [1000, 950, 925, 900, 850, 800, 700, 600, 500, 400]
    t_plot = [h.get(f'temperature_{p}hPa')[idx] for p in p_levs_plot]
    td_plot = [h.get(f'dewpoint_{p}hPa')[idx] for p in p_levs_plot]
    
    if all(v is not None for v in t_plot):
        fig = plt.figure(figsize=(9, 9)); fig.patch.set_facecolor('#222222')
        skew = SkewT(fig, rotation=45); skew.ax.set_facecolor('#222222')
        skew.plot(p_levs_plot, np.array(t_plot) * units.degC, '#e74c3c', linewidth=2.5)
        skew.plot(p_levs_plot, np.array(td_plot) * units.degC, '#3498db', linewidth=2.5)
        skew.ax.set_ylim(1000, 400); skew.ax.set_xlim(-40, 40); skew.ax.axvline(0, color='#B976AC', linestyle='--')
        skew.plot_dry_adiabats(alpha=0.2, color='#e67e22'); skew.plot_moist_adiabats(alpha=0.2, color='#27ae60')
        skew.ax.tick_params(colors='white'); plt.figtext(0.12, 0.05, f"elev: {int(model_elev*3.28)}ft", color='#A0A4AB')
        st.pyplot(fig)
