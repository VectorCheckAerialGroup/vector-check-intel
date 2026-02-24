import requests
import streamlit as st

@st.cache_data(ttl=900)  # Caches for 15 minutes to prevent API throttling
def get_aviation_weather(icao):
    """
    Fetches raw METAR and TAF strings directly from the Aviation Weather Center.
    """
    if not icao or icao == "UNKNOWN":
        return "N/A", "N/A"
        
    try:
        # Fetch METAR
        metar_url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=raw"
        metar_resp = requests.get(metar_url, timeout=5)
        metar_raw = metar_resp.text.strip() if metar_resp.status_code == 200 else "UNAVAILABLE"
        if not metar_raw: 
            metar_raw = "NIL"

        # Fetch TAF
        taf_url = f"https://aviationweather.gov/api/data/taf?ids={icao}&format=raw"
        taf_resp = requests.get(taf_url, timeout=5)
        taf_raw = taf_resp.text.strip() if taf_resp.status_code == 200 else "UNAVAILABLE"
        if not taf_raw: 
            taf_raw = "NIL"

        return metar_raw, taf_raw
        
    except Exception as e:
        return f"API ERROR: {e}", f"API ERROR: {e}"

@st.cache_data(ttl=900)
def fetch_mission_data(lat, lon, model_api_url):
    """
    Fetches the high-resolution atmospheric column from Open-Meteo.
    """
    try:
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": [
                "temperature_2m", "relative_humidity_2m", "weather_code", 
                "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
                "wind_speed_100m", "wind_direction_100m",
                "wind_speed_120m", "wind_direction_120m",
                "temperature_950hPa", "freezing_level_height"
            ],
            "timezone": "UTC"
        }
        
        # Append all pressure levels required for the Extended Trajectory and Sounding
        p_levels = [1000, 950, 925, 900, 850, 800, 700, 600]
        for p in p_levels:
            params["hourly"].extend([
                f"temperature_{p}hPa",
                f"relative_humidity_{p}hPa",
                f"geopotential_height_{p}hPa",
                f"wind_speed_{p}hPa",
                f"wind_direction_{p}hPa"
            ])

        response = requests.get(model_api_url, params=params, timeout=10)
        
        if response.status_code == 200:
            return response.json()
        else:
            print(f"API Request Failed: Status {response.status_code}")
            return None
            
    except Exception as e:
        print(f"Data Fetch Critical Error: {e}")
        return None
