import requests
from datetime import datetime, timezone

def evaluate_gnss_risk(kp):
    """Translates the raw Kp index into tactical drone operational impacts."""
    kp_int = int(round(kp))
    if kp_int <= 3:
        return {"kp": kp, "risk": "LOW", "impact": "Nominal GNSS lock and C2 integrity."}
    elif kp_int == 4:
        return {"kp": kp, "risk": "MODERATE", "impact": "Active state. Minor GNSS jitter. Possible RTK initialization delay."}
    elif kp_int == 5:
        return {"kp": kp, "risk": "HIGH (G1)", "impact": "Minor Geomagnetic Storm. Expect GPS signal degradation and C2 interference."}
    elif kp_int >= 6:
        return {"kp": kp, "risk": "SEVERE (G2+)", "impact": "Major Geomagnetic Storm. High risk of GNSS loss. Manual flight only."}
    
    return {"kp": kp, "risk": "UNKNOWN", "impact": "Data processing error."}

def get_kp_index(target_utc):
    """Fetches the NOAA Planetary K-index forecast with advanced string-exception handling."""
    url = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json"
    
    headers = {
        "User-Agent": "VectorCheckAerialGroup/1.0 (ops.vectorcheck.ca)"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status() 
        data = response.json()
        
        closest_kp = None
        min_diff = float('inf')
        
        # We loop through the entire payload natively instead of trying to slice headers
        for row in data:
            if not row: continue
            
            # 1. Protect the timestamp parser
            try:
                row_dt = datetime.strptime(str(row[0]), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue # If it says 'time_tag' or another header string, skip the row
                
            predicted_kp = None
            
            # 2. Protect the float converter
            try:
                if len(row) >= 4 and row[3]: predicted_kp = float(row[3])
                elif len(row) >= 3 and row[2]: predicted_kp = float(row[2])
                elif len(row) >= 2 and row[1]: predicted_kp = float(row[1])
            except ValueError:
                continue # If it encounters 'observed' or 'estimated', skip the data point
                
            if predicted_kp is not None:
                diff = abs((target_utc - row_dt).total_seconds())
                if diff < min_diff:
                    min_diff = diff
                    closest_kp = predicted_kp
                    
        if closest_kp is not None:
            return evaluate_gnss_risk(closest_kp)
        else:
            return {"kp": "ERR", "risk": "PARSE_FAIL", "impact": "Connected to NOAA, but data format unrecognized."}
            
    except requests.exceptions.HTTPError as err:
        return {"kp": "ERR", "risk": "HTTP_ERR", "impact": f"NOAA Firewall/Server Block: {err}"}
    except Exception as e:
        return {"kp": "ERR", "risk": "SYS_ERR", "impact": f"System Exception: {str(e)}"}
