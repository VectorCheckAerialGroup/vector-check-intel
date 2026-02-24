import re

def apply_tactical_highlights(text):
    """
    Parses raw METAR/TAF strings and injects HTML styling for rapid tactical briefings.
    Streamlined to Vector Check strict Go/No-Go thresholds: 
    Highlights ONLY IFR conditions (<1000ft / <3SM) and Freezing phenomena.
    """
    if not text or text in ["N/A", "NIL", "UNAVAILABLE"]:
        return text
        
    # Split text by temporal markers into independent forecast periods.
    periods = re.split(r'(?=\bFM\d{6}\b|\bTEMPO\b|\bBECMG\b|\bPROB\d{2}\b)', text)
    
    formatted_periods = []
    
    for period in periods:
        if not period.strip(): continue

        # 1. AIRFRAME THREAT: Freezing Precipitation & Fog
        # Targets any group containing FZ (e.g., -FZRA, FZDZ, FZFG)
        period = re.sub(r'\b([+-]?FZ[A-Z]*)\b', r'<span style="background-color: #FF4B4B; color: white; padding: 2px; border-radius: 3px; font-weight: bold;">\1</span>', period)

        # 2. IFR CEILINGS: < 1000 ft
        # Targets OVC, BKN, or VV from 000 up to 009 (900 ft)
        period = re.sub(r'\b(OVC00[0-9]|BKN00[0-9]|VV00[0-9])\b', r'<span style="color: #FF4B4B; font-weight: bold;">\1</span>', period)

        # 3. IFR VISIBILITY: < 3 SM
        # Targets 0SM, 1SM, 2SM, and all aviation fractions (1/4SM, 1/2SM, 1 1/2SM, etc.)
        period = re.sub(r'\b(0SM|[1-2]SM|[1-2]\s?[1-3]/[248]SM|M?1/[248]SM|3/4SM)\b', r'<span style="color: #FF4B4B; font-weight: bold;">\1</span>', period)

        # 4. STRUCTURAL SPACING (Line breaks for temporal markers)
        period = re.sub(r'\b(FM\d{6})\b', r'<br><span style="color: #9CA3AF; font-weight: bold;">\1</span>', period)
        period = re.sub(r'\b(TEMPO)\b', r'<br>&nbsp;&nbsp;&nbsp;&nbsp;<span style="color: #9CA3AF; font-weight: bold;">\1</span>', period)
        period = re.sub(r'\b(BECMG)\b', r'<br>&nbsp;&nbsp;&nbsp;&nbsp;<span style="color: #9CA3AF; font-weight: bold;">\1</span>', period)
        period = re.sub(r'\b(PROB\d{2})\b', r'<br>&nbsp;&nbsp;&nbsp;&nbsp;<span style="color: #9CA3AF; font-weight: bold;">\1</span>', period)

        formatted_periods.append(period)
        
    return "".join(formatted_periods)

def get_precip_type(wx_code):
    """
    Translates WMO weather codes into standard aviation precip types.
    """
    wx_mapping = {
        0: "Clear", 1: "Mainly Clear", 2: "Partly Cloudy", 3: "Overcast",
        45: "Fog", 48: "Freezing Fog",
        51: "Light Drizzle", 53: "Moderate Drizzle", 55: "Dense Drizzle",
        56: "Light FZ Drizzle", 57: "Dense FZ Drizzle",
        61: "Light Rain", 63: "Moderate Rain", 65: "Heavy Rain",
        66: "Light FZ Rain", 67: "Heavy FZ Rain",
        71: "Light Snow", 73: "Moderate Snow", 75: "Heavy Snow",
        77: "Snow Grains",
        80: "Light Rain Showers", 81: "Moderate Rain Showers", 82: "Violent Rain Showers",
        85: "Light Snow Showers", 86: "Heavy Snow Showers",
        95: "Thunderstorms", 96: "TSRA w/ Hail", 99: "Heavy TSRA w/ Hail"
    }
    return wx_mapping.get(wx_code, f"Code {wx_code}")

def calculate_icing_profile(h, idx, wx_code):
    """
    Evaluates atmospheric conditions for icing potential based on Temp, RH, and Precip.
    Requires visible moisture (RH > 85% or active precip) and sub-freezing temps.
    """
    temp = h['temperature_2m'][idx]
    rh = h['relative_humidity_2m'][idx]
    
    visible_moisture = rh >= 85 or (wx_code >= 50 and wx_code <= 99)
    freezing_precip = wx_code in [48, 56, 57, 66, 67]
    
    if freezing_precip:
        return "SEVERE (FZRA/FZDZ)"
    elif visible_moisture and -20 <= temp <= 0:
        if -10 <= temp <= 0:
            return "MODERATE (Clear/Mixed)"
        else:
            return "LIGHT (Rime)"
    elif temp < -20:
        return "TRACE (Ice Crystals)"
    else:
        return "NIL"

def get_turb_ice(alt, s_c, w_spd, g_c, wx, is_stable, icing_cond, airframe_class):
    """
    Efficacy-Audited Turbulence & Icing Engine.
    Scales hazard severity based purely on Transport Canada airframe weight classifications.
    """
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
    else: # Rotary (Helicopter)
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
