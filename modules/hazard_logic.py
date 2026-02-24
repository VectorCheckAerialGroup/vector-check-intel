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
        
        # 3. Apply Category Colors
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

# ... (rest of get_precip_type, calculate_icing_profile, get_turb_ice remain the same)
