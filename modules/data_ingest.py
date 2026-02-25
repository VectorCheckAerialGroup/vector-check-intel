import urllib.request
import json
import ssl

def fetch_mission_data(lat, lon, model_url):
    """
    Fetches raw atmospheric column data.
    Uses a Dual-Fetch architecture to seamlessly merge 2.5km HRDPS surface data 
    with 10km RDPS upper-air data without triggering API schema validation errors.
    """
    is_hrdps = "gem" in model_url

    try:
        # Ignore SSL certificate verification to prevent firewall/cloud blockages
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        if is_hrdps:
            # ---------------------------------------------------------
            # FETCH 1: Pure 2.5km HRDPS (Boundary Layer & Surface)
            # ---------------------------------------------------------
            hrdps_params = (
                "temperature_2m,relative_humidity_2m,weather_code,"
                "wind_speed_10m,wind_direction_10m,wind_gusts_10m,"
                "freezing_level_height,wind_speed_120m,wind_direction_120m"
            )
            url_sfc = f"{model_url}?latitude={lat}&longitude={lon}&hourly={hrdps_params}&models=gem_hrdps_continental&timezone=UTC&wind_speed_unit=knots"
            
            req_sfc = urllib.request.Request(url_sfc, headers={'User-Agent': 'VectorCheck-App/3.0'})
            with urllib.request.urlopen(req_sfc, context=ctx, timeout=10) as response:
                data_master = json.loads(response.read().decode('utf-8'))

            # ---------------------------------------------------------
            # FETCH 2: Pure 10km RDPS (Upper Air Trajectory)
            # ---------------------------------------------------------
            rdps_params_list = ["temperature_950hPa"]
            for p in [1000, 950, 925, 900, 850, 800, 700, 600]:
                rdps_params_list.extend([
                    f"geopotential_height_{p}hPa", 
                    f"wind_speed_{p}hPa", 
                    f"wind_direction_{p}hPa"
                ])
            rdps_params = ",".join(rdps_params_list)
            url_upr = f"{model_url}?latitude={lat}&longitude={lon}&hourly={rdps_params}&models=gem_regional&timezone=UTC&wind_speed_unit=knots"
            
            req_upr = urllib.request.Request(url_upr, headers={'User-Agent': 'VectorCheck-App/3.0'})
            with urllib.request.urlopen(req_upr, context=ctx, timeout=10) as response:
                data_upr = json.loads(response.read().decode('utf-8'))

            # ---------------------------------------------------------
            # MERGE: Stitch the upper air data into the master payload
            # ---------------------------------------------------------
            for key, val_array in data_upr['hourly'].items():
                if key != "time":
                    data_master['hourly'][key] = val_array

            return data_master

        else:
            # ---------------------------------------------------------
            # STANDARD FETCH: ECMWF (Global 9km)
            # ---------------------------------------------------------
            ecmwf_params_list = [
                "temperature_2m", "relative_humidity_2m", "weather_code", 
                "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", 
                "freezing_level_height", "temperature_950hPa",
                "wind_speed_100m", "wind_direction_100m"
            ]
            for p in [1000, 950, 925, 900, 850, 800, 700, 600]:
                ecmwf_params_list.extend([
                    f"geopotential_height_{p}hPa", 
                    f"wind_speed_{p}hPa", 
                    f"wind_direction_{p}hPa"
                ])
                
            params_str = ",".join(ecmwf_params_list)
            url_ecmwf = f"{model_url}?latitude={lat}&longitude={lon}&hourly={params_str}&models=ecmwf_ifs04&timezone=UTC&wind_speed_unit=knots"
            
            req_ecmwf = urllib.request.Request(url_ecmwf, headers={'User-Agent': 'VectorCheck-App/3.0'})
            with urllib.request.urlopen(req_ecmwf, context=ctx, timeout=10) as response:
                return json.loads(response.read().decode('utf-8'))

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
        print(f"Error fetching AWC text data: {e}")
        
    return metar, taf
