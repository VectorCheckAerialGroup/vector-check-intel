import urllib.request
import json
import ssl

def fetch_mission_data(lat, lon, model_url):
    """
    Fetches raw atmospheric column data from Open-Meteo.
    Explicitly forces High-Resolution models to prevent low-res defaults.
    """
    # 1. Force High-Resolution Models
    if "gem" in model_url:
        model_param = "gem_hrdps_continental" # 2.5km Canadian Mesoscale
    elif "ecmwf" in model_url:
        model_param = "ecmwf_ifs04" # 9km Global ECMWF
    else:
        model_param = "best_match"

    # 2. Build the parameter payload for the hazard matrix
    hourly_params = [
        "temperature_2m", "relative_humidity_2m", "weather_code", 
        "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", 
        "freezing_level_height", "temperature_950hPa"
    ]
    
    # GEM outputs low-level wind at 120m, ECMWF at 100m
    if "gem" in model_url:
        hourly_params.extend(["wind_speed_120m", "wind_direction_120m"])
    else:
        hourly_params.extend(["wind_speed_100m", "wind_direction_100m"])

    # Pressure levels for the 1,000 to 5,000ft upper trajectory stack
    pressure_levels = [1000, 950, 925, 900, 850, 800, 700, 600]
    for p in pressure_levels:
        hourly_params.extend([
            f"geopotential_height_{p}hPa",
            f"wind_speed_{p}hPa",
            f"wind_direction_{p}hPa"
        ])

    params_str = ",".join(hourly_params)
    
    # 3. Construct URL and force knot conversion on the server side
    url = f"{model_url}?latitude={lat}&longitude={lon}&hourly={params_str}&models={model_param}&timezone=UTC&wind_speed_unit=knots"

    try:
        # Ignore SSL certificate verification to prevent firewall/cloud blockages
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        req = urllib.request.Request(url, headers={'User-Agent': 'VectorCheck-App/2.0'})
        with urllib.request.urlopen(req, context=ctx, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
            return data
    except Exception as e:
        print(f"Error fetching model data: {e}")
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

        # Fetch latest METAR (hours=1 grabs the most recent issuance)
        metar_url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=raw&hours=1"
        req_m = urllib.request.Request(metar_url, headers={'User-Agent': 'VectorCheck-App/2.0'})
        with urllib.request.urlopen(req_m, context=ctx, timeout=5) as resp:
            m_data = resp.read().decode('utf-8').strip()
            if m_data:
                # If multiple METARs exist in the hour (e.g., a SPECI), grab the top one
                metar = m_data.split('\n')[0]

        # Fetch active TAF
        taf_url = f"https://aviationweather.gov/api/data/taf?ids={icao}&format=raw"
        req_t = urllib.request.Request(taf_url, headers={'User-Agent': 'VectorCheck-App/2.0'})
        with urllib.request.urlopen(req_t, context=ctx, timeout=5) as resp:
            t_data = resp.read().decode('utf-8').strip()
            if t_data:
                taf = t_data

    except Exception as e:
        print(f"Error fetching AWC text data: {e}")
        
    return metar, taf
