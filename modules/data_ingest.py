import requests
import streamlit as st
import re
from modules.hazard_logic import apply_tactical_highlights

@st.cache_data(ttl=60)
def get_aviation_weather(station):
    headers = {"X-API-Key": "c453505478304bbbae7761f99c8a84ba"}
    try:
        m_res = requests.get(f"https://api.checkwx.com/metar/{station}/decoded?count=3", headers=headers, timeout=10)
        t_res = requests.get(f"https://api.checkwx.com/taf/{station}/decoded", headers=headers, timeout=10)
        m_data = m_res.json()
        metars = [apply_tactical_highlights(r.get('raw_text', '')) for r in m_data.get('data', [])]
        for i in range(len(metars)):
            if "SPECI" in metars[i]: metars[i] = metars[i].replace("SPECI", '<span style="color: #E58E26; font-weight: bold;">SPECI</span>')
        taf_raw = t_res.json().get('data', [{}])[0].get('raw_text', "NO ACTIVE TAF")
        taf_final = re.sub(r'\b(FM\d{6}|TEMPO|PROB\d{2}|BECMG)\b', r'<br><b>\1</b>', apply_tactical_highlights(taf_raw))
        return "<br>".join(metars) if metars else "NO DATA", taf_final
    except Exception: 
        return "LINK FAILURE", "LINK FAILURE"

@st.cache_data(ttl=600)
def fetch_mission_data(lat, lon, base_url):
    """Fetches high-resolution weather model data via Commercial API if available."""
    
    # 1. Intercept and swap to commercial endpoint if a key exists in the vault
    api_key = None
    try:
        if "openmeteo" in st.secrets and "api_key" in st.secrets["openmeteo"]:
            api_key = st.secrets["openmeteo"]["api_key"]
            if "api.open-meteo.com" in base_url:
                base_url = base_url.replace("api.open-meteo.com", "customer-api.open-meteo.com")
    except Exception:
        pass # Silently fallback to the free tier if no key is configured
        
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m,weather_code,freezing_level_height",
        "timezone": "UTC"
    }
    
    # 2. Append the required vertical atmospheric profile layers
    if "gem" in base_url: # HRDPS Model
        params["hourly"] += ",wind_speed_120m,wind_direction_120m,temperature_950hPa,geopotential_height_1000hPa,geopotential_height_950hPa,geopotential_height_925hPa,geopotential_height_900hPa,geopotential_height_850hPa,geopotential_height_800hPa,geopotential_height_700hPa,geopotential_height_600hPa,wind_speed_1000hPa,wind_direction_1000hPa,wind_speed_950hPa,wind_direction_950hPa,wind_speed_925hPa,wind_direction_925hPa,wind_speed_900hPa,wind_direction_900hPa,wind_speed_850hPa,wind_direction_850hPa,wind_speed_800hPa,wind_direction_800hPa,wind_speed_700hPa,wind_direction_700hPa,wind_speed_600hPa,wind_direction_600hPa"
    else: # ECMWF Model
        params["hourly"] += ",wind_speed_100m,wind_direction_100m,temperature_950hPa,geopotential_height_1000hPa,geopotential_height_950hPa,geopotential_height_925hPa,geopotential_height_900hPa,geopotential_height_850hPa,geopotential_height_800hPa,geopotential_height_700hPa,geopotential_height_600hPa,wind_speed_1000hPa,wind_direction_1000hPa,wind_speed_950hPa,wind_direction_950hPa,wind_speed_925hPa,wind_direction_925hPa,wind_speed_900hPa,wind_direction_900hPa,wind_speed_850hPa,wind_direction_850hPa,wind_speed_800hPa,wind_direction_800hPa,wind_speed_700hPa,wind_direction_700hPa,wind_speed_600hPa,wind_direction_600hPa"
        
    # 3. Inject the Commercial API Key into the request
    if api_key:
        params["apikey"] = api_key
        
    try:
        response = requests.get(base_url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as err:
        st.error(f"🚨 DATALINK SEVERED: HTTP Error {err.response.status_code}")
        return None
    except Exception as e:
        st.error(f"🚨 DATALINK SEVERED: Connection Timeout or System Error.")
        return None
