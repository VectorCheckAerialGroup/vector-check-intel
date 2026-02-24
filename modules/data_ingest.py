# modules/data_ingest.py
import requests
import time
import streamlit as st
from modules.hazard_logic import apply_tactical_highlights

@st.cache_data(ttl=900)
def get_aviation_weather(icao):
    """
    Fetches raw METAR and TAF strings directly from the Aviation Weather Center
    using an active retry/backoff loop to prevent timeout crashes.
    """
    if not icao or icao == "UNKNOWN":
        return "N/A", "N/A"
        
    def fetch_with_retry(url, retries=3, timeout=10):
        """Helper function to hit the API multiple times before failing."""
        for attempt in range(retries):
            try:
                response = requests.get(url, timeout=timeout)
                if response.status_code == 200:
                    text = response.text.strip()
                    return text if text else "NIL"
                return "UNAVAILABLE"
            except requests.exceptions.RequestException:
                if attempt < retries - 1:
                    time.sleep(2)  # Backoff for 2 seconds before striking again
                else:
                    return f"API ERROR: Connection Timed Out after {retries} attempts."
        return "UNAVAILABLE"

    try:
        # Fetch METAR
        metar_url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=raw"
        metar_raw = fetch_with_retry(metar_url)

        # Fetch TAF
        taf_url = f"https://aviationweather.gov/api/data/taf?ids={icao}&format=raw"
        taf_raw = fetch_with_retry(taf_url)

        # Apply HTML formatting before passing to the UI
        formatted_metar = apply_tactical_highlights(metar_raw)
        formatted_taf = apply_tactical_highlights(taf_raw)

        return formatted_metar, formatted_taf
        
    except Exception as e:
        return f"API ERROR: {e}", f"API ERROR: {e}"

@st.cache_data(ttl=900)
def fetch_mission_data(lat, lon, model_api_url):
    """
    Fetches the high-resolution atmospheric column from Open-Meteo.
    Timeout increased to 15 seconds to handle massive spatial payloads.
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
        
        p_levels = [1000, 950, 925, 900, 850, 800, 700, 600]
        for p in p_levels:
            params["hourly"].extend([
                f"temperature_{p}hPa",
                f"relative_humidity_{p}hPa",
                f"geopotential_height_{p}hPa",
                f"wind_speed_{p}hPa",
                f"wind_direction_{p}hPa"
            ])

        # Increased timeout to 15 seconds for heavy data pulls
        response = requests.get(model_api_url, params=params, timeout=15)
        
        if response.status_code == 200:
            return response.json()
        else:
            print(f"API Request Failed: Status {response.status_code}")
            return None
            
    except Exception as e:
        print(f"Data Fetch Critical Error: {e}")
        return None
