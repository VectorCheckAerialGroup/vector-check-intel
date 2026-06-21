import ephem
import math
from datetime import datetime, timezone, timedelta

def get_cardinal_direction(azimuth_deg):
    """Converts a 360-degree azimuth into an 8-point cardinal direction."""
    directions = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    idx = int(round(azimuth_deg / 45.0)) % 8
    return directions[idx]


def get_light_planning_window(lat, lon, start_date, n_days, local_tz):
    """Computes per-night light-planning data for a multi-day window.

    For each day produces the civil-twilight dark window (last light → first
    light next morning), the moon's rise/set, and illumination — all expressed
    as LOCAL decimal hours on an 18:00→06:00(+1) operational frame so the
    dashboard chart can plot them directly.

    Args:
        lat, lon:    site coordinates (floats)
        start_date:  a date (or datetime) for the first night, local
        n_days:      number of nights to compute
        local_tz:    a pytz/zoneinfo tzinfo for local-time conversion

    Returns a list of dicts, one per night:
        {
          "day_abbr": "FRI", "day_num": "19", "date_label": "19 Jun",
          "last_light":  21.57,   # local decimal hour, civil dusk (-6°)
          "first_light": 4.78,    # local decimal hour next morning (-6°)
          "moonrise":    16.47,   # local decimal hour (may be <18 or None)
          "moonset":     26.0,    # local decimal hour, >24 if after midnight
          "moon_ill":    75,      # percent illuminated (0-100)
          "moon_up_all_night": False,
        }
    Values that can't be computed are None; callers render those gracefully.
    """
    obs = ephem.Observer()
    obs.lat = str(lat)
    obs.lon = str(lon)
    obs.pressure = 0          # geometric horizon (standard for twilight defs)

    # Normalize start to a date
    if isinstance(start_date, datetime):
        start_date = start_date.date()

    def _local_decimal_hour(dt_utc, day_anchor):
        """Convert a UTC datetime to local decimal hours on a frame anchored to
        day_anchor's local midnight. Evening/after-midnight hours that belong to
        the NEXT calendar day are expressed as >24 so the 18→06 frame is
        continuous (e.g. 01:30 next morning → 25.5)."""
        if dt_utc is None:
            return None
        local = dt_utc.astimezone(local_tz)
        h = local.hour + local.minute / 60.0 + local.second / 3600.0
        # If this event's local date is the day AFTER the anchor, add 24h
        delta_days = (local.date() - day_anchor).days
        return h + 24.0 * delta_days

    rows = []
    for d in range(n_days):
        night_date = start_date + timedelta(days=d)
        # Anchor the observer near local noon of this date so next_setting finds
        # THIS evening's dusk and next_rising finds tomorrow morning's first light.
        noon_local = datetime(night_date.year, night_date.month, night_date.day,
                              12, 0, 0, tzinfo=local_tz)
        noon_utc = noon_local.astimezone(timezone.utc)

        sun = ephem.Sun()
        moon = ephem.Moon()

        # --- Civil twilight: sun at -6° ---
        obs.horizon = '-6'
        obs.date = noon_utc
        try:
            last_light_utc = obs.next_setting(sun).datetime().replace(tzinfo=timezone.utc)
        except (ephem.AlwaysUpError, ephem.NeverUpError):
            last_light_utc = None
        try:
            first_light_utc = obs.next_rising(sun).datetime().replace(tzinfo=timezone.utc)
        except (ephem.AlwaysUpError, ephem.NeverUpError):
            first_light_utc = None

        # --- Moon rise/set (geometric horizon) ---
        # Anchor the moon search at LOCAL MIDNIGHT (start of the night's date),
        # not noon — the moon can rise at any hour, and anchoring at noon would
        # skip a pre-noon moonrise to the next day. We want the rise/set events
        # that fall on this calendar day (which then map onto the 18→06 frame;
        # events before 18:00 simply fall left of the frame and are clipped).
        midnight_start_local = datetime(night_date.year, night_date.month,
                                        night_date.day, 0, 0, 0, tzinfo=local_tz)
        midnight_start_utc = midnight_start_local.astimezone(timezone.utc)
        obs.horizon = '0'
        obs.date = midnight_start_utc
        try:
            moonrise_utc = obs.next_rising(moon).datetime().replace(tzinfo=timezone.utc)
        except (ephem.AlwaysUpError, ephem.NeverUpError):
            moonrise_utc = None
        # Moonset must be the set that FOLLOWS this night's moonrise (so the
        # rise→set pair describes one continuous up-period). Searching from
        # midnight can return this morning's set (belonging to the previous
        # night's moon); anchor the set search at the moonrise instead.
        if moonrise_utc is not None:
            obs.date = moonrise_utc
        else:
            obs.date = midnight_start_utc
        try:
            moonset_utc = obs.next_setting(moon).datetime().replace(tzinfo=timezone.utc)
        except (ephem.AlwaysUpError, ephem.NeverUpError):
            moonset_utc = None

        # --- Illumination at local midnight of the night ---
        midnight_local = datetime(night_date.year, night_date.month, night_date.day,
                                  23, 59, 0, tzinfo=local_tz)
        obs.date = midnight_local.astimezone(timezone.utc)
        moon_mid = ephem.Moon(obs)
        moon_ill = int(round(moon_mid.phase))

        ll = _local_decimal_hour(last_light_utc, night_date)
        fl = _local_decimal_hour(first_light_utc, night_date)
        mr = _local_decimal_hour(moonrise_utc, night_date)
        ms = _local_decimal_hour(moonset_utc, night_date)

        # Detect moon-up-all-night: moon rises before dusk AND sets after first
        # light (or never sets within the window).
        moon_up_all = False
        if ll is not None and fl is not None:
            rise_before_dark = (mr is None) or (mr <= ll)
            set_after_dark = (ms is None) or (ms >= fl)
            if rise_before_dark and set_after_dark and (mr is not None or ms is not None):
                moon_up_all = True

        rows.append({
            "day_abbr": night_date.strftime("%a").upper(),
            "day_num": night_date.strftime("%d").lstrip("0") or "0",
            "date_label": night_date.strftime("%d %b"),
            "last_light": round(ll, 3) if ll is not None else None,
            "first_light": round(fl, 3) if fl is not None else None,
            "moonrise": round(mr, 3) if mr is not None else None,
            "moonset": round(ms, 3) if ms is not None else None,
            "moon_ill": moon_ill,
            "moon_up_all_night": moon_up_all,
        })

    return rows


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
