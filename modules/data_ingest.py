import urllib.request
import json
import ssl

def fetch_mission_data(lat, lon, model_url):
    """
    Fetches raw atmospheric column data.
    Uses the master /v1/forecast endpoint to natively blend 2.5km HRDPS and 10km RDPS,
    eliminating Bad Request API crashes from missing parameters.
    """
    is_hrdps = "gem" in model_url
    
    # The magic bullet: hitting the master endpoint and passing the fallback array
    base_url = "https://api.open-meteo.com/v1/forecast"
    model_param = "gem_hrdps_continental,gem_regional" if is_hrdps else "ecmwf_ifs04"

    hourly_params = [
        "temperature_2m", "relative_humidity_2m", "weather_code", 
        "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", 
        "freezing_level_height", "temperature_950hPa",
        "wind_speed_100m", "wind_direction_100m"
    ]
    
    # Safely request pressure levels. The server will natively pull these from RDPS if HRDPS lacks them.
    pressure_levels = [1000, 950, 925, 900, 850, 800, 700, 600]
    for p in pressure_levels:
        hourly_params.extend([
            f"geopotential_height_{p}hPa",
            f"wind_speed_{p}hPa",
            f"wind_direction_{p}hPa"
        ])

    params_str = ",".join(hourly_params)
    url = f"{base_url}?latitude={lat}&longitude={lon}&hourly={params_str}&models={model_param}&timezone=UTC&wind_speed_unit=knots"

    try:
        # Ignore SSL certificate verification to prevent firewall/cloud blockages
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        req = urllib.request.Request(url, headers={'User-Agent': 'VectorCheck-App/4.0'})
        with urllib.request.urlopen(req, context=ctx, timeout=10) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"API Error: {e}")
        return None

def get_aviation_weather(icao):
    """Fetches raw METAR and TAF directly from the Aviation Weather Center API."""
    metar = "UNAVAILABLE"
    taf = "UNAVAILABLE"
    
    if not icao or icao == "NONE" or icao == "N/A":
        return metar, taf
        
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        metar_url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=raw&hours=1"
        req_m = urllib.request.Request(metar_url, headers={'User-Agent': 'VectorCheck-App/2.0'})
        with urllib.request.urlopen(req_m, context=ctx, timeout=5) as resp:
            m_data = resp.read().decode('utf-8').strip()
            if m_data:
                metar = m_data.split('\n')[0]

        taf_url = f"https://aviationweather.gov/api/data/taf?ids={icao}&format=raw"
        req_t = urllib.request.Request(taf_url, headers={'User-Agent': 'VectorCheck-App/2.0'})
        with urllib.request.urlopen(req_t, context=ctx, timeout=5) as resp:
            t_data = resp.read().decode('utf-8').strip()
            if t_data:
                taf = t_data

    except Exception as e:
        pass
        
    return metar, taf
