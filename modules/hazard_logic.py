# modules/hazard_logic.py
import re

def apply_tactical_highlights(text):
    """
    Parses raw METAR/TAF strings and injects HTML styling.
    Colors: LIFR (Orange: #FFA500), IFR (Red: #FF4B4B), MVFR (Yellow: #FFFF00).
    """
    if not text or text in ["N/A", "NIL", "UNAVAILABLE"]:
        return text
        
    periods = re.split(r'(?=\bFM\d{6}\b|\bTEMPO\b|\bBECMG\b|\bPROB\d{2}\b)', text)
    formatted_periods = []
    
    for period in periods:
        if not period.strip(): continue
        
        # 1. Evaluate Lowest Category
        is_lifr = bool(re.search(r'\b(OVC00[0-4]|BKN00[0-4]|VV00[0-4])\b', period)) or \
                  bool(re.search(r'\b(0SM|M?1/[248]SM|3/4SM)\b', period))
                  
        is_ifr = bool(re.search(r'\b(OVC00[5-9]|BKN00[5-9]|VV00[5-9])\b', period)) or \
                 bool(re.search(r'\b([1-2]SM|[1-2]\s?[1-3]/[248]SM)\b', period))
                 
        is_mvfr = bool(re.search(r'\b(OVC0[1-2][0-9]|OVC030|BKN0[1-2][0-9]|BKN030)\b', period)) or \
                  bool(re.search(r'\b([3-5]SM)\b', period))

        # 2. Apply Red Hazard Badges (FZ, TS, etc.)
        period = re.sub(r'\b([+-]?(?:FZ|TS|GR|FC(?!ST)|PL)[A-Z]*)\b', 
                        r'<span style="background-color: #FF4B4B; color: white; padding: 2px 4px; border-radius: 3px; font-weight: bold;">\1</span>', period)
        
        # 3. Apply Category Colors (Mutual Exclusivity)
        if is_lifr:
            period = re.sub(r'\b(OVC00[0-4]|BKN00[0-4]|VV00[0-4]|0SM|M?1/[248]SM|3/4SM)\b', 
                            r'<span style="color: #FFA500; font-weight: bold;">\1</span>', period)
        elif is_ifr:
            period = re.sub(r'\b(OVC00[5-9]|BKN00[5-9]|VV00[5-9]|[1-2]SM|[1-2]\s?[1-3]/[248]SM)\b', 
                            r'<span style="color: #FF4B4B; font-weight: bold;">\1</span>', period)
        elif is_mvfr:
            period = re.sub(r'\b(OVC0[1-2][0-9]|OVC030|BKN0[1-2][0-9]|BKN030|[3-5]SM)\b', 
                            r'<span style="color: #FFFF00; font-weight: bold;">\1</span>', period)

        # 4. Markers & Spacing
        period = re.sub(r'\b(FM\d{6}|TEMPO|BECMG|PROB\d{2})\b', 
                        r'<br><span style="color: #9CA3AF; font-weight: bold;">\1</span>', period)

        formatted_periods.append(period)
        
    return "".join(formatted_periods)

def get_precip_type(wx_code):
    wx_mapping = {
        0: "Clear", 1: "Mainly Clear", 2: "Partly Cloudy", 3: "Overcast",
        45: "Fog", 48: "Freezing Fog", 51: "Light Drizzle", 53: "Moderate Drizzle",
        55: "Dense Drizzle", 56: "Light FZ Drizzle", 57: "Dense FZ Drizzle",
        61: "Light Rain", 63: "Moderate Rain", 65: "Heavy Rain",
        66: "Light FZ Rain", 67: "Heavy FZ Rain", 71: "Light Snow",
        73: "Moderate Snow", 75: "Heavy Snow", 77: "Snow Grains",
        80: "Light Rain Showers", 81: "Moderate Rain Showers", 82: "Violent Rain Showers",
        85: "Light Snow Showers", 86: "Heavy Snow Showers",
        95: "Thunderstorms", 96: "TSRA w/ Hail", 99: "Heavy TSRA w/ Hail"
    }
    return wx_mapping.get(wx_code, f"Code {wx_code}")

def calculate_icing_profile(h, idx, wx_code):
    temp = h['temperature_2m'][idx]
    rh = h['relative_humidity_2m'][idx]
    visible_moisture = rh >= 85 or (wx_code >= 50 and wx_code <= 99)
    freezing_precip = wx_code in [48, 56, 57, 66, 67]
    if freezing_precip:
        return "SEVERE (FZRA/FZDZ)"
    elif visible_moisture and -20 <= temp <= 0:
        if -10 <= temp <= 0: return "MODERATE (Clear/Mixed)"
        else: return "LIGHT (Rime)"
    elif temp < -20: return "TRACE (Ice Crystals)"
    else: return "NIL"

def get_turb_ice(alt, s_c, w_spd, g_c, wx, is_stable, icing_cond, airframe_class):
    gust_spread = max(0, g_c - s_c)
    turb_risk = "NIL"
    if "Micro" in airframe_class:
        sev_spread, sev_sus, sev_gst = 10, 15, 20
        mod_spread, mod_sus = 5, 10
    elif "Small" in airframe_class:
        sev_spread, sev_sus, sev_gst = 15, 25, 30
        mod_spread, mod_sus = 10, 15
    elif "Heavy" in airframe_class:
        sev_spread, sev_sus, sev_gst = 20, 35, 40
        mod_spread, mod_sus = 15, 25
    else: 
        sev_spread, sev_sus, sev_gst = 25, 45, 50
        mod_spread, mod_sus = 15, 30
    if gust_spread >= sev_spread or s_c >= sev_sus or g_c >= sev_gst or wx in [95, 96, 99]:
        turb_risk = "SEVERE"
    elif gust_spread >= mod_spread or s_c >= mod_sus or (not is_stable and alt <= 400 and s_c >= mod_sus - 5):
        turb_risk = "MODERATE"
    elif gust_spread >= (mod_spread / 2) or s_c >= (mod_sus / 2) or not is_stable:
        turb_risk = "LIGHT"
    ice_risk = icing_cond
    if "Rotary" in airframe_class and ice_risk in ["TRACE (Ice Crystals)", "LIGHT (Rime)"]:
        ice_risk = "MODERATE (Rotor Degradation)"
    return turb_risk, ice_risk
