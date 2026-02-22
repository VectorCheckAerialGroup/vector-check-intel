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

# 4. ICING LOGIC
def calculate_icing_profile(hourly_data, idx, wx_code):
    p_levels = [1000, 950, 925, 900, 850, 800, 700, 600, 500, 400]
    profile = []
    for p in p_levels:
        t = hourly_data.get(f"temperature_{p}hPa")[idx]
        td = hourly_data.get(f"dewpoint_{p}hPa")[idx]
        h_m = hourly_data.get(f"geopotential_height_{p}hPa")[idx]
        if t is not None and td is not None and h_m is not None:
            profile.append({"p": p, "t": t, "td": td, "h_ft": h_m * 3.28084})
    
    cloud_layers = []
    ice_cloud_aloft = False
    current_layer = {"base": None, "top": None, "min_t": 100, "max_t": -100, "inversion": False}
    for i, lvl in enumerate(profile):
        is_cloud = (lvl["t"] - lvl["td"]) <= 2.0
        if is_cloud and lvl["t"] < -20: ice_cloud_aloft = True
        if is_cloud:
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
    if wx_code in [66, 67]: return {"type": "CLR", "sev": "SEV", "base": 0, "top": 10000}
    if wx_code in [56, 57, 77]: return {"type": "MX", "sev": "MOD", "base": 0, "top": 10000}

    for layer in cloud_layers:
        if layer["max_t"] <= 0 and layer["min_t"] >= -15:
            if layer["thickness"] >= 2000 and not ice_cloud_aloft:
                i_type, i_sev = "MX", "LGT"
                if wx_code in [71, 73, 75, 85, 86]: i_sev = "NONE"
                elif layer["inversion"]: i_sev = "MOD"
                if i_sev != "NONE":
                    icing_result = {"type": i_type, "sev": i_sev, "base": layer["base"], "top": layer["top"]}
                    break
            else:
                i_type, i_sev = "RIME", "NONE"
                if layer["thickness"] > 5000: i_sev = "MOD"
                elif layer["thickness"] >= 2000: i_sev = "LGT"
                if wx_code in [71, 73, 75, 85, 86]: i_sev = "LGT" if i_sev == "MOD" else "NONE"
                if i_sev != "NONE":
                    icing_result = {"type": i_type, "sev": i_sev, "base": layer["base"], "top": layer["top"]}
                    break
    return icing_result

# 5. DATA FETCHING (PACE LINKS)
@st.cache_data(ttl=300)
def get_aviation_weather(station):
    API_KEY = "c453505478304bbbae7761f99c8a84ba" 
    headers = {"X-API-Key": API_KEY}
    try:
        m_res = requests.get(f"https://api.checkwx.com/metar/{station}/decoded?count=3", headers=headers, timeout=10)
        t_res = requests.get(f"https://api.checkwx.com/taf/{station}/decoded", headers=headers, timeout=10)
        if m_res.status_code == 200 and t_res.status_code == 200:
            m_data = m_res.json()
            metars = [apply_tactical_highlights(r.get('raw_text', '')) for r in m_data.get('data', [])]
            for i in range(len(metars)):
                if "SPECI" in metars[i]: metars[i] = metars[i].replace("SPECI", '<span style="color: #E58E26; font-weight: bold;">SPECI</span>')
            taf_raw = t_res.json()['data'][0].get('raw_text', "NO ACTIVE TAF")
            taf_final = re.sub(r'\b(FM\d{6}|TEMPO|PROB\d{2}|BECMG)\b', r'<br><b>\1</b>', apply_tactical_highlights(taf_raw))
            return "<br>".join(metars) if metars else "NO DATA", taf_final
    except Exception: pass
    try:
        hdr = {'User-Agent': 'Mozilla/5.0'}
        m_res_alt = requests.get(f"https://aviationweather.gov/api/data/metar?ids={station}&hours=3", headers=hdr, timeout=10)
        t_res_alt = requests.get(f"https://aviationweather.gov/api/data/taf?ids={station}", headers=hdr, timeout=10)
        m_lines = [apply_tactical_highlights(m) for m in m_res_alt.text.strip().split('\n') if m][:3]
        for i in range(len(m_lines)):
            if "SPECI" in m_lines[i]: m_lines[i] = m_lines[i].replace("SPECI", '<span style="color: #E58E26; font-weight: bold;">SPECI</span>')
        taf_raw = t_res_alt.text.strip() or "NO ACTIVE TAF"
        taf_final = re.sub(r'\b(FM\d{6}|TEMPO|PROB\d{2}|BECMG)\b', r'<br><b>\1</b>', apply_tactical_highlights(taf_raw))
        return f"<span style='color:#E58E26;'>[FAILOVER DATA ACTIVE]</span><br>{'<br>'.join(m_lines)}", taf_final
    except Exception as e: return f"LINK FAILURE: {str(e)[:20]}", "LINK FAILURE"

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
    return res.json() if res.status_code == 200 else None

# 6. RENDER
st.title("Atmospheric Risk Management")
st.caption("Vector Check Aerial Group Inc.")

data = fetch_mission_data(lat, lon, model_api_map[model_choice])
metar_raw, taf_raw = get_aviation_weather(icao)

st.markdown(f'<div style="background-color: #1B1E23; padding: 15px; border-radius: 5px;"><div class="obs-text"><strong style="color: #8E949E; font-family: sans-serif;">METAR/SPECI</strong><br>{metar_raw}<br><br><strong style="color: #8E949E; font-family: sans-serif;">TAF</strong><br>{taf_raw}</div></div>', unsafe_allow_html=True)
st.divider()

if data and "hourly" in data:
    h = data["hourly"]
    times = [datetime.fromisoformat(t).strftime("%d %b %H:%M Z") for t in h["time"]]
    selected_time = st.sidebar.select_slider("Forecast Hour:", options=times, value=times[0])
    idx = times.index(selected_time)
    
    t, rh, w_spd, wx = h['temperature_2m'][idx], h['relative_humidity_2m'][idx], h['wind_speed_10m'][idx], h['weather_code'][idx]
    sfc_dir = int(h['wind_direction_10m'][idx])
    frz_raw = h.get('freezing_level_height', [None]*len(h['time']))[idx]
    frz_display = "SFC" if t <= 0 else (f"{int(round(frz_raw * 3.28, -2)):,} ft" if frz_raw else "N/A")
    c_base_ft = int((t - (t - ((100-rh)/5)))*400) if rh else 10000

    c = st.columns(8)
    c[0].metric("Temp", f"{t}°C"); c[1].metric("RH", f"{rh}%"); c[2].metric("Wind Dir", f"{sfc_dir:03d}°")
    c[3].metric("Wind Spd", f"{int(w_spd)} kt"); c[4].metric("Precip Type", get_precip_type(wx))
    c[5].metric("Vis (Est)", f"{int((100-rh)/5 * 1.13)} sm"); c[6].metric("Freezing LVL", frz_display)
    c[7].metric("Cloud Base", f"{c_base_ft} ft")

    # PREPARE CORE VARIABLES
    raw_gust = h.get('wind_gusts_10m', [w_spd]*len(h['time']))[idx]
    gst = (w_spd * 1.25) if raw_gust <= w_spd else raw_gust
    
    if h.get('wind_speed_120m') and h['wind_speed_120m'][idx] is not None:
        upper_v, upper_dir, upper_h = h['wind_speed_120m'][idx], h['wind_direction_120m'][idx], 120
    else:
        upper_v, upper_dir, upper_h = h.get('wind_speed_100m', [w_spd*1.5])[idx], h.get('wind_direction_100m', [sfc_dir])[idx], 100
    
    icing_cond = calculate_icing_profile(h, idx, wx)
    t_950 = h.get('temperature_950hPa', [t])[idx]
    is_stable = t_950 is not None and t_950 > (t - 2.0)

    # --- TABLE 1: TACTICAL BOUNDARY LAYER (0-400ft) ---
    st.subheader("Tactical Hazard Stack (0-400ft AGL)")
    stack_tactical = []
    for alt in [400, 300, 200, 100]:
        spd = w_spd + (upper_v - w_spd) * (math.log(alt*0.3048/10) / math.log(upper_h/10))
        cur_gst = spd * (gst / max(w_spd, 1))
        
        diff = (upper_dir - sfc_dir + 180) % 360 - 180
        dir_val = (sfc_dir + diff * (min(alt*0.3048, upper_h) / upper_h)) % 360

        max_w = max(spd, cur_gst)
        shear_kt = spd - w_spd
        shear_per_1000 = (shear_kt / alt) * 1000 if alt > 0 else 0
        
        if wx in [95, 96, 99]: turb_type, turb_sev = "CVCTV", ("SEV" if cur_gst > 25 else "MDT")
        elif is_stable and shear_per_1000 >= 20: turb_type, turb_sev = "LLWS", ("SEV" if shear_per_1000 >= 40 else "MDT")
        else:
            turb_type = "MECH"
            if max_w < 15: turb_sev = "NONE"
            elif max_w < 20: turb_sev = "LGT"
            elif max_w < 25: turb_sev = "MOD" if terrain_type == "Mountains" else "LGT"
            elif max_w < 35: turb_sev = "LGT" if terrain_type == "Water" else "MOD"
            elif max_w < 40: turb_sev = "MOD" if terrain_type == "Water" else ("MOD-SEV" if terrain_type == "Land" else "SEV")
            else: turb_sev = "MOD-SEV" if terrain_type == "Water" else "SEV"
        turb_final = "NONE" if turb_sev == "NONE" else f"{turb_sev} {turb_type}"

        ice_final = "NONE"
        if icing_cond["base"] <= alt <= icing_cond["top"]: ice_final = f"{icing_cond['sev']} {icing_cond['type']}"
        elif icing_cond["base"] == 0 and alt < icing_cond["top"]: ice_final = f"{icing_cond['sev']} {icing_cond['type']}"

        stack_tactical.append({
            "Alt (AGL)": f"{alt}ft", 
            "Dir": f"{int(dir_val):03d}°",
            "Spd (kt)": int(spd), 
            "Gust (kt)": int(cur_gst), 
            "Turbulence": turb_final,
            "Icing": ice_final
        })
    st.table(pd.DataFrame(stack_tactical).set_index("Alt (AGL)"))

    # --- TABLE 2: EXTENDED TRAJECTORY (1000-5000ft) ---
    st.subheader("Extended Trajectory (1,000-5,000ft AGL)")
    
    # Build Profile for Interpolation (pulling from deep pressure layers to cover 5000ft)
    p_levels = [1000, 950, 925, 900, 850, 800, 700, 600]
    p_profile = []
    for p in p_levels:
        ws, wd, gh = h.get(f'wind_speed_{p}hPa'), h.get(f'wind_direction_{p}hPa'), h.get(f'geopotential_height_{p}hPa')
        if ws and wd and gh and ws[idx] is not None and wd[idx] is not None and gh[idx] is not None:
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
        
        if below['h_ft'] == above['h_ft']:
            spd, dir_val = below['spd'], below['dir']
        else:
            fraction = max(0, min(1, (alt - below['h_ft']) / (above['h_ft'] - below['h_ft'])))
            spd = below['spd'] + fraction * (above['spd'] - below['spd'])
            diff = (above['dir'] - below['dir'] + 180) % 360 - 180
            dir_val = (below['dir'] + diff * fraction) % 360
        
        cur_gst = spd + (max(0, gst - w_spd) * math.exp(-alt / 1500))

        max_w = max(spd, cur_gst)
        shear_per_1000 = ((spd - upper_v) / alt) * 1000 if alt > 0 else 0
        if wx in [95, 96, 99]: turb_type, turb_sev = "CVCTV", ("SEV" if cur_gst > 25 else "MDT")
        elif is_stable and shear_per_1000 >= 15: turb_type, turb_sev = "LLWS", ("SEV" if shear_per_1000 >= 30 else "MDT")
        else:
            turb_type = "MECH"
            if max_w < 20: turb_sev = "NONE"
            elif max_w < 30: turb_sev = "LGT"
            elif max_w < 45: turb_sev = "MOD"
            else: turb_sev = "SEV"
        turb_final = "NONE" if turb_sev == "NONE" else f"{turb_sev} {turb_type}"

        ice_final = "NONE"
        if icing_cond["base"] <= alt <= icing_cond["top"]: ice_final = f"{icing_cond['sev']} {icing_cond['type']}"
        elif icing_cond["base"] == 0 and alt < icing_cond["top"]: ice_final = f"{icing_cond['sev']} {icing_cond['type']}"

        stack_ext.append({
            "Alt (AGL)": f"{alt}ft", 
            "Dir": f"{int(dir_val):03d}°",
            "Spd (kt)": int(spd), 
            "Gust (kt)": int(cur_gst), 
            "Turbulence": turb_final,
            "Icing": ice_final
        })
    st.table(pd.DataFrame(stack_ext).set_index("Alt (AGL)"))

    st.divider()
    p_levs = [1000, 950, 925, 900, 850, 800, 700, 600, 500, 400]
    t_plot = [h.get(f'temperature_{p}hPa', [None]*len(h['time']))[idx] for p in p_levs]
    td_plot = [h.get(f'dewpoint_{p}hPa', [None]*len(h['time']))[idx] for p in p_levs]
    if all(v is not None for v in t_plot):
        fig = plt.figure(figsize=(6, 8)); fig.patch.set_facecolor('#0E1117')
        skew = SkewT(fig, rotation=45); skew.ax.set_facecolor('#1B1E23')
        skew.plot(p_levs, np.array(t_plot) * units.degC, 'r', linewidth=2)
        skew.plot(p_levs, np.array(td_plot) * units.degC, 'g', linewidth=2)
        st.pyplot(fig)
