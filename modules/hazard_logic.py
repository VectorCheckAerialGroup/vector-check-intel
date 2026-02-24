import re

def get_precip_type(wx_code):
    """Translates WMO weather codes into standard aviation weather types."""
    wx_map = {
        0: "Clear", 1: "Mainly Clear", 2: "Partly Cloudy", 3: "Overcast",
        45: "Fog", 48: "Freezing Fog",
        51: "Light Drizzle", 53: "Moderate Drizzle", 55: "Dense Drizzle",
        56: "Light Freezing Drizzle", 57: "Dense Freezing Drizzle",
        61: "Light Rain", 63: "Moderate Rain", 65: "Heavy Rain",
        66: "Light Freezing Rain", 67: "Heavy Freezing Rain",
        71: "Light Snow", 73: "Moderate Snow", 75: "Heavy Snow",
        77: "Snow Grains",
        80: "Light Rain Showers", 81: "Moderate Rain Showers", 82: "Violent Rain Showers",
        85: "Light Snow Showers", 86: "Heavy Snow Showers",
        95: "Thunderstorm", 96: "Thunderstorm w/ Hail", 99: "Heavy Thunderstorm w/ Hail"
    }
    return wx_map.get(wx_code, "Unknown")

def calculate_icing_profile(h, idx, wx):
    """Determines general icing conditions based on RH, Temp, and active precipitation."""
    rh = h['relative_humidity_2m'][idx]
    temp = h['temperature_2m'][idx]
    
    # Visible moisture + freezing temps
    if rh >= 85 and -20 <= temp <= 0:
        return True
    # Specific freezing/winter precipitation WMO codes
    if wx in [48, 56, 57, 66, 67, 71, 73, 75, 77, 85, 86]:
        return True
        
    return False

def get_turb_ice(alt, wind_alt, wind_sfc, gust_alt, wx, is_stable, icing_cond, airframe_class, t_temp):
    """
    Calculates altitude-specific turbulence and icing, formatted to standard aviation 
    reporting terminology (LGT/MDT/SEV | MECH/LLWS/CVCTV | CLR/RIME/MXD).
    """
    gust_delta = max(0, gust_alt - wind_alt)
    shear = abs(wind_alt - wind_sfc)

    # 1. DYNAMIC TURBULENCE THRESHOLDS BASED ON AIRFRAME MASS
    if "Micro" in airframe_class:
        sev_g, mdt_g, lgt_g = 12, 8, 4
    elif "Small" in airframe_class:
        sev_g, mdt_g, lgt_g = 15, 10, 5
    elif "Heavy" in airframe_class:
        sev_g, mdt_g, lgt_g = 20, 12, 6
    else: # Rotary / Manned equivalence
        sev_g, mdt_g, lgt_g = 25, 15, 8

    # Assign Turbulence Severity
    turb_sev = "Nil"
    if gust_delta >= sev_g or shear >= sev_g:
        turb_sev = "SEV"
    elif gust_delta >= mdt_g or shear >= mdt_g:
        turb_sev = "MDT"
    elif gust_delta >= lgt_g or shear >= lgt_g:
        turb_sev = "LGT"

    # Assign Turbulence Type
    turb_type = ""
    if turb_sev != "Nil":
        # Restrict CVCTV purely to convective precipitation codes (Showers & TS)
        if wx in [80, 81, 82, 85, 86, 95, 96, 97, 98, 99]:
            turb_type = "CVCTV"  # Convective
        elif shear > gust_delta and shear >= mdt_g:
            turb_type = "LLWS"   # Low-Level Wind Shear
        else:
            turb_type = "MECH"   # Mechanical

    turb_str = f"{turb_sev} {turb_type}" if turb_sev != "Nil" else "Nil"

    # 2. ICING PROFILES (Standard lapse rate 2°C per 1,000 ft)
    t_alt = t_temp - (alt / 1000.0) * 2.0
    ice_sev = "Nil"
    ice_type = ""

    if icing_cond or wx in [48, 56, 57, 66, 67, 71, 73, 75, 77, 85, 86]:
        # Icing generally occurs between 0C and -20C
        if 0 >= t_alt >= -20:
            
            # Severity Logic
            if wx in [56, 57, 66, 67]: # Freezing Rain/Drizzle is automatically Severe
                ice_sev = "SEV"
            elif wx in [73, 75, 86] or (icing_cond and t_alt >= -10):
                ice_sev = "MDT"
            else:
                ice_sev = "LGT"

            # Type Logic based on droplet freezing speed
            if t_alt >= -5:
                ice_type = "CLR"
            elif t_alt >= -15:
                ice_type = "MXD"
            else:
                ice_type = "RIME"

    ice_str = f"{ice_sev} {ice_type}" if ice_sev != "Nil" else "Nil"

    return turb_str, ice_str

def apply_tactical_highlights(raw_text):
    """Injects warning colors into raw METAR/TAF strings for rapid parsing."""
    if not raw_text or "UNAVAILABLE" in raw_text:
        return raw_text
    
    # Red highlights for tactical hazards
    hazards = ["FZRA", "FZDZ", "TSRA", "TS", "GR", "FC", r"\+FC", "LLWS", "SEV", "ICE"]
    for hazard in hazards:
        raw_text = re.sub(rf"\b({hazard})\b", r'<span class="ifr-text">\1</span>', raw_text)
        
    return raw_text
