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
    """Evaluates base icing condition and type from surface parameters."""
    t_raw = h.get('temperature_2m', [0])[idx]
    rh_raw = h.get('relative_humidity_2m', [0])[idx]
    
    t = float(t_raw) if t_raw is not None else 0.0
    rh = int(rh_raw) if rh_raw is not None else 0
    wx = int(wx_code) if wx_code is not None else 0
    
    if wx in [66, 67]:
        return "SEV CLR"
    elif wx in [56, 57]:
        return "SEV MIXED"
    elif wx == 48:
        return "MOD RIME"
    
    if t <= 0:
        if wx >= 50:
            return "MOD MIXED"
        elif rh >= 90:
            return "MOD RIME"
        elif rh >= 80:
            return "LGT RIME"
            
    return "NIL"

def get_turb_ice(alt, wind_spd, sfc_spd, gust, wx, is_stable, icing_cond, t_temp, terrain_type="Land"):
    """
    Evaluates turbulence using Vector Check's proprietary intensity matrix.
    Urban environment utilizes a pragmatic 1.25x Venturi multiplier on the 
    forecasted base wind to balance safety with operational viability.
    """
    w_spd = float(wind_spd) if wind_spd is not None else 0.0
    g_spd = float(gust) if gust is not None else 0.0
    wx_val = int(wx) if wx is not None else 0
    t_val = float(t_temp) if t_temp is not None else 0.0
    
    max_wind = max(w_spd, g_spd)
    
    turb_type = "MECH" 
    turb_sev = "NIL"
    
    # --- TRUE PHYSICS MATRIX ---
    if terrain_type == "Water":
        if max_wind >= 40: turb_sev = "MOD-SEV"
        elif max_wind >= 35: turb_sev = "MOD"
        elif max_wind >= 15: turb_sev = "LGT"
        
    elif terrain_type == "Mountains":
        if max_wind >= 35: turb_sev = "SEV"
        elif max_wind >= 20: turb_sev = "MOD"
        elif max_wind >= 15: turb_sev = "LGT"

    elif terrain_type == "Urban":
        # 1.25x Pragmatic Shear Model (e.g., 32 forecast = ~40 actual)
        if max_wind >= 32: turb_sev = "SEV"        
        elif max_wind >= 28: turb_sev = "MOD-SEV"  
        elif max_wind >= 20: turb_sev = "MOD"      
        elif max_wind >= 12: turb_sev = "LGT"      
        
    else: # Land (Default)
        if max_wind >= 40: turb_sev = "SEV"
        elif max_wind >= 35: turb_sev = "MOD-SEV"
        elif max_wind >= 25: turb_sev = "MOD"
        elif max_wind >= 15: turb_sev = "LGT"

    if wx_val in [95, 96, 99]:
        turb_type = "CONV"
        turb_sev = "SEV"

    turb = f"{turb_sev} {turb_type}" if turb_sev != "NIL" else "NIL"
        
    # --- ICING ALOFT LOGIC ---
    ice = icing_cond
    
    if alt > 0:
        alt_temp = t_val - ((alt / 1000.0) * 1.98)
        if ice == "NIL" and alt_temp <= 0:
            if wx_val in [66, 67]:
                ice = "SEV CLR"
            elif wx_val in [56, 57, 48]:
                ice = "MOD RIME"
            elif wx_val >= 50:
                ice = "MOD MIXED"
            
    return turb, ice

def apply_tactical_highlights(text):
    """Applies HTML highlighting to critical METAR/TAF elements."""
    if not text or text == "NIL" or text == "UNAVAILABLE":
        return text
        
    text = re.sub(r'\b(FZ[A-Z]+)\b', r'<span class="fz-warn">\1</span>', text)
    text = re.sub(r'\b(BKN|OVC)(0[0-0][0-9])\b', r'<span class="ifr-text">\1\2</span>', text)
    text = re.sub(r'\b(BKN|OVC)(0[1-2][0-9]|030)\b', r'<span class="mvfr-text">\1\2</span>', text)
    text = re.sub(r'\b([M]?[0-2](?:\s?[1-3]/[2-4])?SM)\b', r'<span class="ifr-text">\1</span>', text)
    
    return text
