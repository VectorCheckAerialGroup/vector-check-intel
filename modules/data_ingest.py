import json
import urllib.request
import streamlit as st

def get_aviation_weather(icao):
    try:
        url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=raw&taf=true"
        req = urllib.request.Request(url, headers={'User-Agent': 'VectorCheck-App/2.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = response.read().decode('utf-8')
            
        parts = data.split('\n')
        metar = parts[0] if len(parts) > 0 else "NIL"
        taf = '\n'.join(parts[1:]) if len(parts) > 1 else "NIL"
        return metar, taf
    except Exception:
        return "NIL", "NIL"

def fetch_mission_data(lat, lon, model_url):
    try:
        # COMMERCIAL SLA UPGRADE: Detect secure API key and reroute to dedicated servers
        api_key = st.secrets.get("open_meteo", {}).get("api_key", None)
        
        if api_key:
            model_url = model_url.replace("https://api.open-meteo.com", "https://customer-api.open-meteo.com")

        # Base surface variables
        vars_list = [
            "temperature_2m", "relative_humidity_2m", "wind_speed_10m", 
            "wind_gusts_10m", "wind_direction_10m", "weather_code", 
            "visibility", "freezing_level_height"
        ]
        
        # Dynamic Pressure Level Resolution
        if "gem" in model_url:
            p_levels = [1000, 925, 850, 700, 500, 250]
        else:
            p_levels = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300, 250, 200, 150]
            
        for p in p_levels:
            vars_list.extend([
                f"temperature_{p}hPa", f"relative_humidity_{p}hPa", 
                f"geopotential_height_{p}hPa", f"wind_speed_{p}hPa", 
                f"wind_direction_{p}hPa"
            ])
            
        vars_str = ",".join(vars_list)
        
        # Build URL
        url = f"{model_url}?latitude={lat}&longitude={lon}&hourly={vars_str}&timezone=UTC"
        
        # Inject API Key for Commercial Authorization
        if api_key:
            url += f"&apikey={api_key}"
        
        req = urllib.request.Request(url, headers={'User-Agent': 'VectorCheck-App/2.0'})
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode('utf-8'))
            return data
            
    except urllib.error.HTTPError as e:
        return {"error": True, "message": f"HTTP {e.code}: {e.read().decode('utf-8')}"}
    except Exception as e:
        return {"error": True, "message": str(e)}
