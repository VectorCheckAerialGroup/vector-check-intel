import re
import math

def apply_tactical_highlights(text):
    if not text: return ""
    def precip_match(m): return f'<span class="fz-warn">{m.group(0)}</span>'
    text = re.sub(r'(?<!\S)[-+]?[A-Z]*(?:FZ|PL|TS|GR|SQ)[A-Z]*(?!\S)', precip_match, text)
    
    def vis_match_sm(m):
        raw = m.group(0)
        try:
            clean = raw.upper().replace('SM', '').replace('P', '').replace('M', '').strip()
            val = 0.0
            for p in clean.split():
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

    def vis_match_m(m):
        raw = m.group(1)
        try:
            val_m = int(raw)
            if val_m == 9999: return raw 
            val_sm = val_m / 1609.34 
            if val_sm < 3: return f'<span class="ifr-text">{raw}</span>'
            if 3 <= val_sm <= 5: return f'<span class="mvfr-text">{raw}</span>'
        except: pass
        return raw
    text = re.sub(r'(?<!\S)(\d{4})(?![Z/\d])(?!\S)', vis_match_m, text)

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

def calculate_icing_profile(hourly_data, idx, wx_code):
    p_levels = [1000, 950, 925, 900, 850, 800, 700, 600, 500, 400]
    profile = []
    for p in p_levels:
        t_v = hourly_data.get(f"temperature_{p}hPa")[idx]
        td_v = hourly_data.get(f"dewpoint_{p}hPa")[idx]
        h_m = hourly_data.get(f"geopotential_height_{p}hPa")[idx]
        if t_v is not None and td_v is not None and h_m is not None:
            profile.append({"p": p, "t": t_v, "td": td_v, "h_ft": h_m * 3.28084})
    
    cloud_layers = []
    curr = {"base": None, "top": None, "min_t": 100, "max_t": -100, "inv": False}
    for i, lvl in enumerate(profile):
        if (lvl["t"] - lvl["td"]) <= 3.0:
            if curr["base"] is None: 
                curr["base"] = lvl["h_ft"]
                curr["bottom_t"] = lvl["t"]
            curr["top"] = lvl["h_ft"]
            curr["min_t"] = min(curr["min_t"], lvl["t"])
            curr["max_t"] = max(curr["max_t"], lvl["t"])
            if i > 0 and lvl["t"] > profile[i-1]["t"]: curr["inv"] = True
        else:
            if curr["base"] is not None:
                curr["thick"] = curr["top"] - curr["base"]
                cloud_layers.append(curr)
                curr = {"base": None, "top": None, "min_t": 100, "max_t": -100, "inv": False}
    
    if wx_code in [66, 67, 56, 57]: return {"type": "CLR (FZRA)", "sev": "SEV", "base": 0, "top": 10000}
    for layer in cloud_layers:
        if layer["max_t"] <= 0.5:
            i_t, i_s = "RIME", "LGT"
            if layer["thick"] > 1500 or layer["inv"]: i_t, i_s = "MXD", "MOD"
            if layer["thick"] > 4000: i_s = "SEV"
            return {"type": i_t, "sev": i_s, "base": layer["base"], "top": layer["top"]}
    return {"type": "NONE", "sev": "NONE", "base": 99999, "top": -99999}

def get_turb_ice(alt, spd, w_spd, cur_gst, wx, is_stable, icing_cond):
    sh_1k = ((spd - w_spd) / alt) * 1000 if alt > 0 else 0
    if wx in [95, 96, 99]: 
        t_type, t_sev = "CVCTV", ("SEV" if cur_gst > 25 else "MDT")
    elif is_stable and sh_1k >= 20: 
        t_type, t_sev = "LLWS", ("SEV" if sh_1k >= 40 else "MDT")
    else:
        t_type = "MECH"
        max_w = max(spd, cur_gst)
        if max_w < 15: t_sev = "NONE"
        elif max_w < 25: t_sev = "LGT"
        elif max_w < 35: t_sev = "MOD"
        else: t_sev = "SEV"
        
    ice = "NONE"
    if icing_cond["base"] <= alt <= icing_cond["top"]: 
        ice = f"{icing_cond['sev']} {icing_cond['type']}"
    elif icing_cond["base"] == 0 and alt < icing_cond["top"]: 
        ice = f"{icing_cond['sev']} {icing_cond['type']}"
        
    return f"{t_sev} {t_type}" if t_sev != "NONE" else "NONE", ice
