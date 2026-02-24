import re

def get_weather_element(wx_code, wind_spd=0):
    """Translates WMO weather codes into standard aviation weather types and METAR abbreviations."""
    wx_map = {
        0: "NIL", 1: "NIL", 2: "NIL", 3: "NIL",
        45: "Fog (FG)", 48: "Freezing Fog (FZFG)",
        51: "Light Drizzle (DZ)", 53: "Moderate Drizzle (DZ)", 55: "Dense Drizzle (DZ)",
        56: "Light Freezing Drizzle (FZDZ)", 57: "Dense Freezing Drizzle (FZDZ)",
        61: "Light Rain (RA)", 63: "Moderate Rain (RA)", 65: "Heavy Rain (RA)",
        66: "Light Freezing Rain (FZRA)", 67: "Heavy Freezing Rain (FZRA)",
        71: "Light Snow (SN)", 73: "Moderate Snow (SN)", 75: "Heavy Snow (SN)",
        77: "Snow Grains (SG)",
        80: "Light Rain Showers (SHRA)", 81: "Moderate Rain Showers (SHRA)", 82: "Violent Rain Showers (SHRA)",
        85: "Light Snow Showers (SHSN)", 86: "Heavy Snow Showers (SHSN)",
        95: "Thunderstorm (TS)", 96: "Thunderstorm w/ Hail (TSGR)", 99: "Heavy Thunderstorm w/ Hail (TSGR)"
    }
    
    base_wx = wx_map.get(wx_code, "NIL")
    
    # Dynamic Blowing Snow (BLSN) calculation based on Vector Check doctrine
    if wx_code in [71, 73, 75, 85, 86] and wind_spd >= 15:
        return "Blowing Snow (BLSN)"
        
    return base_wx

def calculate_icing_profile(h, idx, wx):
    """Determines general icing conditions based on RH, Temp, and active precipitation."""
    rh = h['relative_humidity_2m'][idx]
    temp = h['temperature_2m'][idx]
    
    # Visible moisture + freezing temps
    if rh >= 85 and -20 <= temp <= 0: return True
    # Specific freezing/winter precipitation WMO codes
    if wx in [48, 56, 57, 66, 67, 71, 73, 75, 77, 85, 86]: return True
        
    return False

def get_turb_ice(alt, wind_alt, wind_sfc, gust_alt, wx, is_stable, icing_cond, airframe_class, t_temp):
    """
    Calculates altitude-specific turbulence and icing using explicit meteorological doctrine.
    Strictly aligns with TAF/Tac Prog thresholds and segregates boundary layer from upper-level shear.
    """
    # 1. BASE VARIABLES
    shear_total = abs(wind_alt - wind_sfc)
    shear_rate_1000 = (shear_total / alt) * 1000 if alt > 0 else 0
    mech_wind = max(wind_alt, gust_alt)
    
    # Airframe scaling applies ONLY to Mechanical wind resistance
    scale = 0.4 if "Micro" in airframe_class else (0.6 if "Small" in airframe_class else 1.0)

    # 2. EVALUATE CONVECTIVE TURBULENCE 
    conv_lvl = 0
    if wx in [80, 81, 82, 85, 86, 95, 96, 97, 98, 99]: 
        conv_lvl = 3 if wx >= 95 else 2 

    threats = []
    if conv_lvl > 0:
        threats.append((conv_lvl, "CVCTV"))

    # 3. SEPARATE BOUNDARY LAYER (<=3000) FROM UPPER SHEAR
    if alt <= 3000:
        
        # EVALUATE LLWS
        is_llws = False
        # Absolute minimum gate: Prevent micro-layer rate hallucinations
        if shear_total >= 20: 
            if (alt <= 5000 and shear_rate_1000 >= 20) or \
               (alt <= 500 and shear_total >= 25) or \
               (alt <= 1000 and shear_total >= 40) or \
               (alt <= 1500 and shear_total >= 50):
                is_llws = True

        # EVALUATE MECH
        mech_lvl = 0
        if mech_wind >= (40 * scale): mech_lvl = 3
        elif mech_wind >= (25 * scale): mech_lvl = 2
        elif mech_wind >= (15 * scale): mech_lvl = 1

        # MUTUAL EXCLUSIVITY
        if is_llws:
            threats.append((3, "LLWS")) 
        elif mech_lvl > 0:
            threats.append((mech_lvl, "MECH"))

    else:
        # EVALUATE UPPER LEVEL SHEAR (CAT/Frontal) > 3000ft
        shear_lvl = 0
        if shear_rate_1000 >= 10: shear_lvl = 3
        elif shear_rate_1000 >= 6: shear_lvl = 2
        elif shear_rate_1000 >= 3: shear_lvl = 1
        
        if shear_lvl > 0:
            threats.append((shear_lvl, "SHEAR"))

    # 4. RESOLVE DOMINANT TURBULENCE
    turb_str = "NIL"
    if threats:
        # Sort by highest severity first
        threats.sort(key=lambda x: x[0], reverse=True)
        top_sev, top_type = threats[0]
        
        sev_str = "SEV" if top_sev == 3 else ("MDT" if top_sev == 2 else "LGT")
        turb_str = f"{sev_str} {top_type}"

    # 5. ICING PROFILES
    t_alt = t_temp - (alt / 1000.0) * 2.0
    ice_sev = "NIL"
    ice_type = ""

    if icing_cond or wx in [48, 56, 57, 66, 67, 71, 73, 75, 77, 85, 86]:
        if 0 >= t_alt >= -20:
            if wx in [56, 57, 66, 67]: 
                ice_sev = "SEV"
            elif wx in [73, 75, 86] or (icing_cond and t_alt >= -10):
                ice_sev = "MDT"
            else:
                ice_sev = "LGT"

            if t_alt >= -5:
                ice_type = "CLR"
            elif t_alt >= -15:
                ice_type = "MXD"
            else:
                ice_type = "RIME"

    ice_str = f"{ice_sev} {ice_type}" if ice_sev != "NIL" else "NIL"

    return turb_str, ice_str

def apply_tactical_highlights(raw_text):
    """Injects warning colors into raw METAR/TAF strings for rapid parsing."""
    if not raw_text or "UNAVAILABLE" in raw_text:
        return raw_text
    
    hazards = ["FZRA", "FZDZ", "TSRA", "TS", "GR", "FC", r"\+FC", "LLWS", "SEV", "ICE"]
    for hazard in hazards:
        raw_text = re.sub(rf"\b({hazard})\b", r'<span class="ifr-text">\1</span>', raw_text)
        
    return raw_text
