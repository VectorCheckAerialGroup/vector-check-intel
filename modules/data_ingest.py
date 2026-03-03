import urllib.request
import json

def get_aviation_weather(icao):
    """Fetches real-time METAR and TAF for the specified ICAO code."""
    try:
        url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=raw"
        req = urllib.request.Request(url, headers={'User-Agent': 'VectorCheck-App/2.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            metar = response.read().decode('utf-8').strip()
            
        url_taf = f"https://aviationweather.gov/api/data/taf?ids={icao}&format=raw"
        req_taf = urllib.request.Request(url_taf, headers={'User-Agent': 'VectorCheck-App/2.0'})
        with urllib.request.urlopen(req_taf, timeout=5) as response_taf:
            taf = response_taf.read().decode('utf-8').strip()
            
        return metar if metar else "NIL", taf if taf else "NIL"
    except Exception:
        return "NIL", "NIL"

def fetch_mission_data(lat, lon, model_url):
    """Fetches tactical surface and 15-layer upper-air NWP data from Open-Meteo."""
    
    # Core surface and newly added thermodynamic/aerodynamic variables (Surface Pressure added)
    hourly_vars = (
        "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,"
        "wind_gusts_10m,weather_code,visibility,freezing_level_height,"
        "precipitation_probability,precipitation,cape,boundary_layer_height,surface_pressure"
    )
    
    # 15-Layer Tactical Column
    p_levels = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300, 250, 200, 150]
    for p in p_levels:
        hourly_vars += f",temperature_{p}hPa,relative_humidity_{p}hPa,geopotential_height_{p}hPa,wind_speed_{p}hPa,wind_direction_{p}hPa"
    
    url = f"{model_url}?latitude={lat}&longitude={lon}&hourly={hourly_vars}&elevation=nan&timezone=UTC"
    
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'VectorCheck-App/2.0'})
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        return {"error": True, "message": str(e)}
