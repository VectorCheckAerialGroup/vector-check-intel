import re

def get_weather_element(wx_code, wind_spd):
    """Translates WMO weather codes into human-readable text."""
    wx_map = {
        0: "NIL", 1: "NIL", 2: "NIL", 3: "NIL",
        45: "Fog", 48: "Freezing Fog",
        51: "Light Drizzle", 53: "Drizzle", 55: "Heavy Drizzle", 
        56: "Freezing Drizzle", 57: "Heavy Freezing Drizzle",
        61: "Light Rain", 63: "Rain", 65: "Heavy Rain", 
        66: "Light Freezing Rain", 67: "Heavy Freezing Rain",
        71: "Light Snow", 73: "Snow", 75: "Heavy Snow", 77: "Snow Grains",
        80: "Light Rain Showers", 81: "Rain Showers", 82: "Heavy Rain Showers",
        85: "Light Snow Showers", 86: "Heavy Snow Showers",
        95: "Thunderstorms", 96: "Thunderstorms with Hail", 99: "Severe Thunderstorms"
    }
    return wx_map.get(wx_code, "NIL")

def calculate_icing_profile(h, idx, wx_code):
    """Evaluates base surface icing condition utilizing official Vector Check matrices."""
    t_raw = h.get('temperature_2m', [0])[idx]
    rh_raw = h.get('relative_humidity_2m', [0])[idx]
    
    t = float(t_raw) if t_raw is not None else 0.0
    rh = int(rh_raw) if rh_raw is not None else 0
    wx = int(wx_code) if wx_code is not None else 0
    
    if wx in [66, 67]: 
        return "SEV CLR"
    elif wx in [56, 57, 77]: 
        return "MOD MX"
    elif wx == 48: 
        return "MOD RIME"
    
    if t <= 0:
        if wx >= 50:
            return "MOD MX"
        elif rh >= 90:
            return "MOD RIME"
        elif rh >= 80:
            return "LGT RIME"
            
    return "NIL"

def get_turb_ice(alt, wind_spd, sfc_spd, gust, wx, is_stable, icing_cond, t_temp, rh, terrain_type="Land"):
    """
    Evaluates turbulence and icing risk.
    Mechanical turbulence is capped at 3000ft AGL. 
    Icing is evaluated against temperature bands, cloud types, and LWC proxies.
    """
    w_spd = float(wind_spd) if wind_spd is not None else 0.0
    s_spd = float(sfc_spd) if sfc_spd is not None else 0.0
    g_spd = float(gust) if gust is not None else 0.0
    wx_val = int(wx) if wx is not None else 0
    t_val = float(t_temp) if t_temp is not None else 0.0
    rh_val = int(rh) if rh is not None else 0
    
    max_wind = max(w_spd, g_spd)
    gust_delta = max(0, g_spd - s_spd) 
    
    turb_type = "MECH" 
    turb_sev = "NIL"
    
    # --- TURBULENCE LOGIC ---
    if alt <= 3000:
        if terrain_type == "Water":
            if max_wind >= 40: turb_sev = "MOD-SEV"
            elif max_wind >= 35: turb_sev = "MOD"
            elif max_wind >= 15: turb_sev = "LGT"
            
        elif terrain_type == "Mountains":
            if max_wind >= 35: turb_sev = "SEV"
            elif max_wind >= 20: turb_sev = "MOD"
            elif max_wind >= 15: turb_sev = "LGT"

        elif terrain_type == "Urban":
            if max_wind >= 32: turb_sev = "SEV"        
            elif max_wind >= 28: turb_sev = "MOD-SEV"  
            elif max_wind >= 20: turb_sev = "MOD"      
            elif max_wind >= 12: turb_sev = "LGT"      
            
        else: # Land
            if max_wind >= 40: turb_sev = "SEV"
            elif max_wind >= 35: turb_sev = "MOD-SEV"
            elif max_wind >= 25: turb_sev = "MOD"
            elif max_wind >= 15: turb_sev = "LGT"
    else:
        turb_type = "SHEAR"
        if gust_delta >= 15: turb_sev = "SEV"
        elif gust_delta >= 10: turb_sev = "MOD"

    if wx_val in [95, 96, 99]:
        turb_type = "CONV"
        turb_sev = "SEV"

    turb = f"{turb_sev} {turb_type}" if turb_sev != "NIL" else "NIL"
        
    # --- ICING ALOFT LOGIC ---
    ice = "NIL"
    
    if alt > 0:
        alt_temp = t_val - ((alt / 1000.0) * 1.98)
        
        if alt_temp > 0 or alt_temp < -40:
            ice = "NIL"
        else:
            if wx_val in [66, 67]: 
                ice = "SEV CLR"
            elif wx_val in [95, 96, 99]: 
                ice = "SEV CLR"
            elif wx_val in [56, 57, 77]: 
                ice = "MOD MX"
            elif wx_val in [80, 81, 82, 85, 86]: 
                ice = "MOD MX"
            elif rh_val >= 80: 
                if 0 >= alt_temp >= -15:
                    if rh_val >= 90:
                        ice = "MOD RIME" 
                    else:
                        ice = "LGT RIME"
                elif -15 > alt_temp >= -20:
                    ice = "LGT RIME"
                elif alt_temp < -20:
                    if rh_val >= 95:
                        ice = "LGT RIME"
                    else:
                        ice = "NIL"
            
    return turb, ice

def apply_tactical_highlights(text):
    """
    Applies HTML highlighting to METAR/TAF.
    Strict Audit Rule: Evaluates each line and applies a single color 
    based on the lowest flight category present in that line.
    """
    if not text or text == "NIL" or text == "UNAVAILABLE":
        return text
        
    lines = text.split('\n')
    formatted_lines = []
    
    for line in lines:
        # Priority 1: Freezing Conditions (Worst Case)
        if re.search(r'\b(FZ[A-Z]*)\b', line):
            formatted_lines.append(f'<span class="fz-warn">{line}</span>')
            continue
            
        # Priority 2: IFR (Ceiling < 1000ft, OR VV, OR Vis < 3SM)
        is_ifr = False
        if re.search(r'\b(BKN|OVC|VV)(00[0-9])\b', line): # 000 to 009
            is_ifr = True
        elif re.search(r'\b([M]?[0-2](?:\s?[1-3]/[2-4])?SM)\b', line): # Matches < 3SM fractions
            is_ifr = True
            
        if is_ifr:
            formatted_lines.append(f'<span class="ifr-text">{line}</span>')
            continue
            
        # Priority 3: MVFR (Ceiling 1000-3000ft OR Vis 3-5SM)
        is_mvfr = False
        if re.search(r'\b(BKN|OVC)(0[1-2][0-9]|030)\b', line): # 010 to 030
            is_mvfr = True
        elif re.search(r'\b([3-5]SM)\b', line): # 3SM, 4SM, 5SM
            is_mvfr = True
            
        if is_mvfr:
            formatted_lines.append(f'<span class="mvfr-text">{line}</span>')
            continue
            
        # Priority 4: VFR / No Highlight
        formatted_lines.append(line)
        
    return '\n'.join(formatted_lines)
