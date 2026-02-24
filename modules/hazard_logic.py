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
    if rh >= 85 and -20 <= temp <= 0: return True
    # Specific freezing/winter precipitation WMO codes
    if wx in [48, 56, 57, 66, 67, 71, 73, 75, 77, 85, 86]: return True
        
    return False

def get_turb_ice(alt, wind_alt, wind_sfc, gust_alt, wx, is_stable, icing_cond, airframe_class, t_temp):
    """
    Calculates altitude-specific turbulence and icing using explicit meteorological doctrine.
    """
    # ==========================================
    # 1. TURBULENCE ENGINE
    # ==========================================
    shear_total = abs(wind_alt - wind_sfc)
    shear_rate_1000 = (shear_total / alt) * 1000 if alt > 0 else 0
    mech_wind = max(wind_alt, gust_alt)
    scale = 0.4 if "Micro" in airframe_class else (0.6 if "Small" in airframe_class else 1.0)

    conv_lvl = 0
    if wx in [80, 81, 82, 85, 86, 95, 96, 97, 98, 99]: 
        conv_lvl = 3 if wx >= 95 else 2 

    threats = []
    if conv_lvl > 0:
        threats.append((conv_lvl, "CVCTV"))

    is_llws = False
    if alt <= 3000:
        # EVALUATE LLWS
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
        if is_llws: threats.append((3, "LLWS")) 
        elif mech_lvl > 0: threats.append((mech_lvl, "MECH"))
    else:
        # EVALUATE UPPER LEVEL SHEAR (CAT/Frontal) > 3000ft
        shear_lvl = 0
        if shear_rate_1000 >= 10: shear_lvl = 3
        elif shear_rate_1000 >= 6: shear_lvl = 2
        elif shear_rate_1000 >= 3: shear_lvl = 1
        if shear_lvl > 0: threats.append((shear_lvl, "SHEAR"))

    turb_str = "NIL"
    if threats:
        threats.sort(key=lambda x: x[0], reverse=True)
        top_sev, top_type = threats[0]
        sev_str = "SEV" if top_sev == 3 else ("MDT" if top_sev == 2 else "LGT")
        turb_str = f"{sev_str} {top_type}"

    # ==========================================
    # 2. ICING ENGINE (Tables 1-4 Doctrine)
    # ==========================================
    t_alt = t_temp - (alt / 1000.0) * 2.0
    ice_sev = "NIL"
    ice_type = ""

    # Icing generally requires visible moisture (RH >= 85%) or active precip, and T between 0C and -40C
    if (icing_cond or wx in [48, 56, 57, 66, 67, 71, 73, 75, 77, 85, 86]) and 0 >= t_alt >= -40:
        
        # A. FREEZING PRECIPITATION (Table 3)
        if wx in [66, 67]: # FZRA
            ice_sev, ice_type = "SEV", "CLR"
        elif wx in [56, 57, 77]: # FZDZ, SG
            ice_sev, ice_type = "MDT", "MX"
            
        # B. CONVECTIVE CLOUD ICING (Table 1 & Text)
        elif wx in [95, 96, 97, 98, 99]: # CB (Thunderstorm)
            ice_sev, ice_type = "SEV", "CLR"
        elif wx in [80, 81, 82, 85, 86]: # CU/TCU (Showers)
            ice_sev, ice_type = "MDT", "MX"
            
        # C. STABLE / STRATIFORM CLOUD ICING (Table 2, Table 4 & Text)
        else:
            ice_type = "RIME"
            
            if t_alt < -20:
                # Unlikely to find severe icing < -20C due to low LWC
                ice_sev = "LGT"
            else:
                # Between 0C and -20C
                # Check for Heavy Snow (WMO 75) which implies thick cloud -> MDT RIME
                if wx == 75:
                    ice_sev = "MDT"
                else:
                    ice_sev = "LGT" # Default for stable clouds without extreme thickness
                
                # Table 4 SLD Modifiers (Low-Level Cloud SLD)
                # Strong wind shear through the cloud layer enhances SLD (+1 cat to MDT MX)
                if is_llws or (alt > 3000 and shear_rate_1000 >= 10):
                    ice_sev = "MDT"
                    ice_type = "MX"

    ice_str = f"{ice_sev} {ice_type}" if ice_sev != "NIL" else "NIL"

    return turb_str, ice_str

def apply_tactical_highlights(raw_text):
    """Injects warning colors into raw METAR/TAF strings for rapid parsing."""
    if not raw_text or "UNAVAILABLE" in raw_text: return raw_text
    hazards = ["FZRA", "FZDZ", "TSRA", "TS", "GR", "FC", r"\+FC", "LLWS", "SEV", "ICE"]
    for hazard in hazards:
        raw_text = re.sub(rf"\b({hazard})\b", r'<span class="ifr-text">\1</span>', raw_text)
    return raw_text
