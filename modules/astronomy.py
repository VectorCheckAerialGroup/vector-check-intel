import ephem
import math
from datetime import datetime, timezone

def get_cardinal_direction(azimuth_deg):
    """Converts a 360-degree azimuth into an 8-point cardinal direction."""
    directions = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    idx = int(round(azimuth_deg / 45.0)) % 8
    return directions[idx]

def get_astronomical_data(lat, lon, time_utc, local_tz, tz_abbr):
    """Calculates high-precision astronomical data using discrete observer states."""
    
    # 1. THE CURRENT STATE (Wired to your slider)
    obs_current = ephem.Observer()
    obs_current.lat = str(lat)
    obs_current.lon = str(lon)
    obs_current.date = time_utc

    sun_current = ephem.Sun(obs_current)
    moon_current = ephem.Moon(obs_current)
    
    sun_az_deg = math.degrees(sun_current.az)
    sun_alt_deg = int(math.degrees(sun_current.alt))
    moon_az_deg = math.degrees(moon_current.az)
    moon_alt_deg = int(math.degrees(moon_current.alt))
    moon_ill = int(moon_current.phase)

    # 2. THE DAILY EVENT STATE (Locked to midnight for forward-sweeping)
    midnight = datetime(time_utc.year, time_utc.month, time_utc.day, tzinfo=timezone.utc)
    obs_daily = ephem.Observer()
    obs_daily.lat = str(lat)
    obs_daily.lon = str(lon)
    obs_daily.date = midnight

    # Create separate bodies so we do not mutate the current slider positions
    sun_daily = ephem.Sun()
    moon_daily = ephem.Moon()

    def get_event(func, body):
        try:
            dt_utc = func(body).datetime().replace(tzinfo=timezone.utc)
            dt_local = dt_utc.astimezone(local_tz)
            return dt_local.strftime("%H:%M")
        except ephem.AlwaysUpError:
            return "UP 24H"
        except ephem.NeverUpError:
            return "DOWN 24H"
        except Exception:
            return "N/A"

    obs_daily.horizon = '-0:34' 
    sunrise = get_event(obs_daily.next_rising, sun_daily)
    sunset = get_event(obs_daily.next_setting, sun_daily)
    
    obs_daily.horizon = '0' 
    moonrise = get_event(obs_daily.next_rising, moon_daily)
    moonset = get_event(obs_daily.next_setting, moon_daily)

    obs_daily.horizon = '-6' 
    dawn = get_event(obs_daily.next_rising, sun_daily)
    dusk = get_event(obs_daily.next_setting, sun_daily)

    return {
        "sun_dir": get_cardinal_direction(sun_az_deg),
        "sun_alt": sun_alt_deg,
        "moon_dir": get_cardinal_direction(moon_az_deg),
        "moon_alt": moon_alt_deg,
        "moon_ill": moon_ill,
        "sunrise": sunrise,
        "sunset": sunset,
        "dawn": dawn,
        "dusk": dusk,
        "moonrise": moonrise,
        "moonset": moonset,
        "tz": tz_abbr
    }
