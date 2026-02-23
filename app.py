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

if st.sidebar.button("Force Manual Data Refresh"):
    st.cache_data.clear()
    st.sidebar.success("Cache Cleared.")

model_api_map = {
    "HRDPS (Canada 2.5km)": "https://api.open-meteo.com/v1/gem",
    "ECMWF (Global 9km)": "https://api.open-meteo.com/v1/ecmwf"
}

# 3. HIGHLIGHTING HELPERS (METAR/TAF)
def apply_tactical_highlights(text):
    if not text: return ""
    
    # 1. Severe Weather / Freezing Precip
    def precip_match(m):
        return f'<span class="fz-warn">{m.group(0)}</span>'
    text = re.sub(r'(?<!\S)[-+]?[A-Z]*(?:FZ|PL|TS|GR|SQ)[A-Z]*(?!\S)', precip_match, text)
    
    # 2. Visibility (Statute Miles - handles P6SM, 1 1/2SM, M1/4SM)
    def vis_match_sm(m):
        raw = m.group(0)
        try:
            clean = raw.upper().replace('SM', '').replace('P', '').replace('M', '').strip()
            parts = clean.split()
            val = 0.0
            for p in parts:
                if '/' in p:
                    num, den = p.split('/')
                    val += float(num) / float(den)
                else:
                    val += float(p)
            if val < 3: return f'<span class="ifr-text">{raw}</span>'
            if 3 <= val <= 5: return f'<span class="mvfr-text">{raw}</span>'
        except: pass
        return raw
    text = re.sub(r'(?<!\S)[PM]?(?:\d+\s+)?(?:\d+/\d+|\d+)SM(?!\S)', vis_match_sm, text)

    # 3. Visibility (ICAO Meters - handles 0800, 1200, 9999)
    def vis_match_m(m):
        raw = m.group(1)
        try:
            val_m = int(raw)
            if val_m == 9999: return raw # P6SM equivalent
            val_sm = val_m / 1609.34     # Convert to SM
            if val_sm < 3: return f'<span class="ifr-text">{raw}</span>'
            if 3 <= val_sm <= 5: return f'<span class="mvfr-text">{raw}</span>'
        except: pass
        return raw
    # Only match exactly 4 digits standing alone
    text = re.sub(r'(?<!\S)(\d{4})(?!\S)', vis_match_m, text)

    # 4. Ceiling/Sky (BKN, OVC, VV with optional CB/TCU)
    def sky_match(m):
        try:
            h = int(m.group(2)) * 100
            if h < 1000: return f'<span class="ifr-text">{m.group(0)}</span>'
            if 1000 <= h <= 3000: return f'<span class="mvfr-text">{m.group(0)}</span>'
        except: pass
        return m.group(0)
    text = re.sub(r'(?<!\S)(BKN|OVC|VV)(\d{3})(?:CB|TCU)?(?!\S)', sky_match, text)
    
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
        if t_val is not None and td_val is not None:
            profile.append({"p": p, "t": t_val, "td": td_val, "h_ft": h_m * 3.28084})
    
    cloud_layers = []
    current_layer = {"base": None, "top": None, "min_t": 100, "max_t": -100, "inversion": False}
    for i, lvl in enumerate(profile):
        if (lvl["t"] - lvl["td"]) <= 3.0:
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
    
    icing_result = {"type": "NONE", "sev": "NONE", "base": 99999, "top": -99999}
    if wx_code in [66, 67, 56, 57]: return {"type": "CLR (FZRA)", "sev": "SEV", "base": 0, "top": 10000}

    for layer in cloud_layers:
        if layer["max_t"] <= 0.5:
            i_type, i_sev = "RIME", "LGT"
            if layer["thickness"] > 1500 or layer["inversion"]: i_type, i_sev = "MXD", "MOD"
            if layer["thickness"] > 4000: i_sev = "SEV"
            return {"type": i_type, "sev": i_sev, "base": layer["base"], "top": layer["top"]}
    return icing_result

# 5. DATA FETCHING
@st.cache_data(ttl=60)
def get_aviation_weather(station):
    API_KEY = "c453505478304bbbae7761f99c8a84ba" 
    headers = {"X-API-Key": API_KEY}
    try:
        m_res = requests.get(f"https://api.checkwx.com/metar/{station}/decoded?count=3", headers=headers, timeout=10)
        t_res = requests.get(f"https://api.checkwx.com/taf/{station}/decoded", headers=headers, timeout=10)
        m_data = m_res.json()
        metars = [apply_tactical_highlights(r.get('raw_text', '')) for r in m_data.get('data', [])]
        for i in range(len(metars)):
            if "SPECI" in metars[i]: metars[i] = metars[i].replace("SPECI", '<span style="color: #E58E26; font-weight: bold;">SPECI</span>')
        
        taf_raw = t_res.json().get('data', [{}])[0].get('raw_text', "NO ACTIVE TAF")
        taf_final = re.sub(r'\b(FM\d{6}|TEMPO|PROB\d{2}|BECMG)\b', r'<br><b>\1</b>', apply_tactical_highlights(taf_raw))
        return "<br>".join(metars) if metars else "NO DATA", taf_final
    except Exception: return "LINK FAILURE", "LINK FAILURE"

@st.cache_data(ttl=600)
def fetch_mission_data(lat, lon, model_url):
    p_levels = [1000, 950, 925, 900, 850, 800, 700, 600, 500, 400]
    hourly = ["temperature_2m", "relative_humidity_2m", "wind_speed_10m", "wind_direction_10m", "weather_code", "freezing_level_height"]
    if "gem" in model_url: hourly += ["wind_gusts_10m", "wind_speed_80m", "wind_speed_120m", "wind_direction_80m", "wind_direction_120m"]
    else: hourly += ["wind_speed_100m", "wind_direction_100m"]
    hourly += [f"temperature_{p}hPa" for p in p_levels] + [f"dewpoint_{p}hPa" for p in p_levels] + [f"geopotential_height_{p}hPa" for p in p_levels] + [f"wind_speed_{p}hPa" for p in p_levels] + [f"wind_direction_{p}hPa" for p in p_levels]
    res = requests.get(model_url, params={"latitude": lat, "longitude": lon, "hourly": hourly, "wind_speed_unit": "kn", "forecast_hours": 48, "timezone": "UTC"})
    return res.json() if res.status_code == 200 else None

# 6. RENDER
data = fetch_mission_data(lat, lon, model_api_map[model_choice])
metar_raw, taf_raw = get_aviation_weather(icao)

st.title("Atmospheric Risk Management")
st.caption("Vector Check Aerial Group Inc.")
st.markdown(f'<div style="background-color: #1B1E23; padding: 15px; border-radius: 5px;"><div class="obs-text"><strong style="color: #8E949E;">METAR/SPECI</strong><br>{metar_raw}<br><br><strong style="color: #8E949E;">TAF</strong><br>{taf_raw}</div></div>', unsafe_allow_html=True)
st.divider()

if data and "hourly" in data:
    h = data["hourly"]
    times = [datetime.fromisoformat(t).strftime("%d %b %H:%M Z") for t in h["time"]]
    selected_time = st.sidebar.select_slider("Forecast Hour:", options=times, value=times[0])
    idx = times.index(selected_time)
    
    t, rh, w_spd, wx = h['temperature_2m'][idx], h['relative_humidity_2m'][idx], h['wind_speed_10m'][idx], h['weather_code'][idx]
    td = t - ((100 - rh) / 5) if (t is not None and rh is not None) else t
    sfc_dir = int(h['wind_direction_10m'][idx])
    frz_raw = h.get('freezing_level_height', [None]*len(h['time']))[idx]
    frz_disp = "SFC" if t <= 0 else (f"{int(round(frz_raw * 3.28, -2)):,} ft" if frz_raw else "N/A")
    c_base = int((t - td)*400) if (t is not None and td is not None) else 10000

    c = st.columns(8)
    c[0].metric("Temp", f"{t}°C"); c[1].metric("RH", f"{rh}%"); c[2].metric("Wind Dir", f"{sfc_dir:03d}°")
    c[3].metric("Wind Spd", f"{int(w_spd)} kt"); c[4].metric("Precip Type", get_precip_type(wx))
    c[5].metric("Vis (Est)", f"{int((100-rh)/5 * 1.13)} sm"); c[6].metric("Freezing LVL", frz_disp); c[7].metric("Cloud Base", f"{c_base} ft")

    # WIND/TURB CALCS
    raw_gst = h.get('wind_gusts_10m', [w_spd]*len(h['time']))[idx]
    gst = (w_spd * 1.25) if raw_gst <= w_spd else raw_gst
    if "gem" in model_api_map[model_choice]: u_v, u_dir, u_h = h['wind_speed_120m'][idx], h['wind_direction_120m'][idx], 120
    else: u_v, u_dir, u_h = h['wind_speed_100m'][idx], h['wind_direction_100m'][idx], 100
    
    icing_cond = calculate_icing_profile(h, idx, wx)
    t_950 = h.get('temperature_950hPa', [t])[idx]
    is_stable = t_950 is not None and t_950 > (t - 2.0)

    # HAZARD LOGIC HELPER
    def get_turb_ice(alt, spd, cur_gst, u_v, sfc_dir, u_dir):
        shear_per_1000 = ((spd - w_spd) / alt) * 1000 if alt > 0 else 0
        if wx in [95, 96, 99]: t_type, t_sev = "CVCTV", ("SEV" if cur_gst > 25 else "MDT")
        elif is_stable and shear_per_1000 >= 20: t_type, t_sev = "LLWS", ("SEV" if shear_per_1000 >= 40 else "MDT")
        else:
            t_type = "MECH"
            max_w = max(spd, cur_gst)
            if max_w < 15: t_sev = "NONE"
            elif max_w < 25: t_sev = "LGT"
            elif max_w < 35: t_sev = "MOD"
            else: t_sev = "SEV"
        ice = "NONE"
        if icing_cond["base"] <= alt <= icing_cond["top"]: ice = f"{icing_cond['sev']} {icing_cond['type']}"
        elif icing_cond["base"] == 0 and alt < icing_cond["top"]: ice = f"{icing_cond['sev']} {icing_cond['type']}"
        return f"{t_sev} {t_type}" if t_sev != "NONE" else "NONE", ice

    # --- TABLES ---
    st.subheader("Tactical Hazard Stack (0-400ft AGL)")
    stack_tactical = []
    for alt in [400, 300, 200, 100]:
        s_c = w_spd + (u_v - w_spd) * (math.log(alt*0.3048/10) / math.log(u_h/10))
        g_c = s_c * (gst / max(w_spd, 1))
        d_c = (sfc_dir + ((u_dir - sfc_dir + 180) % 360 - 180) * (min(alt*0.3048, u_h) / u_h)) % 360
        turb, ice = get_turb_ice(alt, s_c, g_c, u_v, sfc_dir, u_dir)
        stack_tactical.append({"Alt (AGL)": f"{alt}ft", "Dir": f"{int(d_c):03d}°", "Spd (kt)": int(s_c), "Gust (kt)": int(g_c), "Turbulence": turb, "Icing": ice})
    st.table(pd.DataFrame(stack_tactical).set_index("Alt (AGL)"))

    st.subheader("Extended Trajectory (1,000-5,000ft AGL)")
    p_levels_traj = [1000, 950, 925, 900, 850, 800, 700, 600]
    p_profile = sorted([{'h': h.get(f'geopotential_height_{p}hPa')[idx]*3.28, 's': h.get(f'wind_speed_{p}hPa')[idx], 'd': h.get(f'wind_direction_{p}hPa')[idx]} for p in p_levels_traj if h.get(f'wind_speed_{p}hPa')[idx] is not None], key=lambda x: x['h'])
    stack_ext = []
    for alt in [5000, 4000, 3000, 2000, 1000]:
        pts = [{'h': u_h*3.28, 's': u_v, 'd': u_dir}] + p_profile
        blw, abv = pts[0], pts[-1]
        for i in range(len(pts)-1):
            if pts[i]['h'] <= alt <= pts[i+1]['h']: blw, abv = pts[i], pts[i+1]; break
        frac = (alt - blw['h']) / (abv['h'] - blw['h']) if abv['h'] != blw['h'] else 0
        s_e, d_e = blw['s'] + frac * (abv['s'] - blw['s']), (blw['d'] + ((abv['d'] - blw['d'] + 180) % 360 - 180) * frac) % 360
        turb, ice = get_turb_ice(alt, s_e, s_e, u_v, sfc_dir, u_dir)
        stack_ext.append({"Alt (AGL)": f"{alt}ft", "Dir": f"{int(d_e):03d}°", "Spd (kt)": int(s_e), "Turbulence": turb, "Icing": ice})
    st.table(pd.DataFrame(stack_ext).set_index("Alt (AGL)"))

    st.divider()
    
    # SKEW-T
    p_levs_plot = [1000, 950, 925, 900, 850, 800, 700, 600, 500, 400]
    t_plot, td_plot = [h.get(f'temperature_{p}hPa')[idx] for p in p_levs_plot], [h.get(f'dewpoint_{p}hPa')[idx] for p in p_levs_plot]
    if all(v is not None for v in t_plot):
        fig = plt.figure(figsize=(9, 9)); fig.patch.set_facecolor('#222222')
        skew = SkewT(fig, rotation=45); skew.ax.set_facecolor('#222222')
        skew.plot(p_levs_plot, np.array(t_plot) * units.degC, '#e74c3c', linewidth=2.5)
        skew.plot(p_levs_plot, np.array(td_plot) * units.degC, '#3498db', linewidth=2.5)
        skew.ax.set_ylim(1000, 400); skew.ax.set_xlim(-40, 40); skew.ax.axvline(0, color='#B976AC', linestyle='--')
        skew.plot_dry_adiabats(alpha=0.2, color='#e67e22'); skew.plot_moist_adiabats(alpha=0.2, color='#27ae60')
        skew.ax.tick_params(colors='white'); plt.figtext(0.12, 0.05, f"elev: {int(data.get('elevation', 0)*3.28)}ft", color='#A0A4AB')
        st.pyplot(fig)
