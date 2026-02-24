# modules/hazard_logic.py

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
    VTOL aerodynamic considerations have been stripped.
    """
    gust_spread = max(0, g_c - s_c)
    turb_risk = "NIL"
    
    # Define scaling thresholds based purely on weight class
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

    # 1. EVALUATE TURBULENCE RISK 
    if gust_spread >= sev_spread or s_c >= sev_sus or g_c >= sev_gst or wx in [95, 96, 99]:
        turb_risk = "SEVERE"
    elif gust_spread >= mod_spread or s_c >= mod_sus or (not is_stable and alt <= 400 and s_c >= mod_sus - 5):
        turb_risk = "MODERATE"
    elif gust_spread >= (mod_spread / 2) or s_c >= (mod_sus / 2) or not is_stable:
        turb_risk = "LIGHT"

    # 2. EVALUATE ICING RISK
    ice_risk = icing_cond
    if "Rotary" in airframe_class and ice_risk in ["TRACE (Ice Crystals)", "LIGHT (Rime)"]:
        ice_risk = "MODERATE (Rotor Degradation)"

    return turb_risk, ice_risk
