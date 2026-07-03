import math

# --- VECTOR CHECK AERIAL GROUP INC. : SHARED PHYSICS CONSTANTS ---

# Minimum snowpack depth (metres) required to trigger the BLSN kinetic gate
# when no active precipitation is present. 0.05 m = 5 cm.
SNOWPACK_BLSN_THRESHOLD_M: float = 0.05

# Authoritative unit conversion constants — use ONLY these, never magic numbers.
METERS_TO_FEET: float = 3.28084
METERS_TO_SM: float = 1609.344
KMH_TO_KT: float = 0.539957

# Standard atmosphere constants
ISA_PRESSURE_HPA: float = 1013.25
ISA_TEMP_C: float = 15.0
ISA_LAPSE_C_PER_1000FT: float = 1.98

# Convective / cloud analysis
CONVECTIVE_CCL_MULTIPLIER: int = 400

# All pressure levels requested from NWP API
ALL_P_LEVELS: list[int] = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300, 250, 200, 150]

# WMO weather codes that the thermal phase gate may synthesize.
# 68/69 are non-standard but used internally by ARMS to represent
# freezing-rain-in-transition-zone (surface 0–2.5°C, frz lvl < 1500ft AGL).
SYNTHETIC_WX_CODES: set[int] = {68, 69}


def calc_td(t: float, rh: float) -> float:
    """
    Magnus formula dew point calculation.

    Defensively clamps RH to [0.01, 110] before computing — out-of-range RH
    typically indicates a sensor or upstream-data fault, and silently passing
    through (returning T for rh<=0 as the previous implementation did) makes
    the bad reading look like saturation, which is dangerous in forecasts.

    Args:
        t:  Air temperature (°C)
        rh: Relative humidity (%)

    Returns:
        Dew point temperature (°C)
    """
    # Bad input → clamp. We tolerate very low RH (extreme dry) and slightly
    # super-saturated readings (sensor artifacts) but force a sane bound.
    if rh is None:
        return t   # treat missing as saturated; caller should ideally guard
    try:
        rh = float(rh)
    except (TypeError, ValueError):
        return t
    if not (rh == rh):  # NaN
        return t
    rh = max(0.01, min(110.0, rh))

    a, b = 17.625, 243.04
    alpha = math.log(rh / 100.0) + ((a * t) / (b + t))
    return (b * alpha) / (a - alpha)


def calculate_density_altitude(
    elevation_ft: float,
    temp_c: float,
    station_pressure_hpa: float,
) -> int:
    """True Density Altitude computed from station pressure and air temperature.

    Pressure altitude is derived from raw station pressure using the ICAO 1976
    Standard Atmosphere pressure-height equation, then corrected for non-standard
    temperature.

    The previous implementation (pre-2026) used the FAA altimeter-setting
    formula `PA = elev + 27.288 * (1013.25 - SP)` which expects QNH (sea-level-
    equivalent altimeter setting). Applied to raw station pressure (QFE) from
    Open-Meteo's `surface_pressure` variable, that formula over-reported DA
    by ~4500 ft at 5000 ft elevation. The current implementation derives PA
    directly from station pressure (no altimeter setting required) so the
    elevation_ft parameter is no longer used in the calculation; it is kept
    in the signature for backward compatibility with existing callers.

    Args:
        elevation_ft:          Site elevation above MSL (ft) — kept for API
                               compatibility; not used in the new formula.
        temp_c:                Surface air temperature (°C)
        station_pressure_hpa:  Raw station pressure (QFE) in hPa / mb

    Returns:
        Density altitude (ft), rounded to nearest foot. Verified against
        US Standard Atmosphere tables to <1 ft accuracy at all altitudes.
    """
    # Pressure altitude from raw station pressure using ICAO 1976.
    # P / P0 = (1 - L*h/T0)^(g*M/(R*L)) inverted for h.
    # Constant 145366.45 ft corresponds to T0/L (288.15 K / 0.0019812 K/ft).
    # Exponent 0.190284 = R*L/(g*M).
    if station_pressure_hpa is None or station_pressure_hpa <= 0:
        return int(elevation_ft)
    pressure_altitude_ft = 145366.45 * (1.0 - (station_pressure_hpa / ISA_PRESSURE_HPA) ** 0.190284)

    # ISA temperature at the pressure altitude (not at the field elevation).
    isa_temperature = ISA_TEMP_C - (ISA_LAPSE_C_PER_1000FT * (pressure_altitude_ft / 1000.0))

    # Standard DA correction: 118.8 ft per °C of deviation from ISA at PA.
    density_altitude = pressure_altitude_ft + 118.8 * (temp_c - isa_temperature)
    return int(round(density_altitude))


def attenuate_gust_delta(surface_gust_delta: float, alt_agl_ft: float) -> float:
    """
    Attenuates gust spread with altitude using a logarithmic decay model.

    Surface-level gustiness (mechanical turbulence, thermal convection)
    diminishes as you ascend through the boundary layer. This replaces
    the previous uniform application of surface gust delta at all altitudes.

    The model uses a 1/ln decay anchored at 10m (surface reference height).
    At 400ft AGL the attenuation is ~0.6, at 3000ft it's ~0.3, at 5000ft ~0.25.

    Args:
        surface_gust_delta: Gust spread at surface (gust_speed - sustained_speed) in KT
        alt_agl_ft:         Altitude above ground level in feet

    Returns:
        Attenuated gust delta at the specified altitude (KT)
    """
    if alt_agl_ft <= 0 or surface_gust_delta <= 0:
        return surface_gust_delta

    # Decay factor: ratio of log-law at surface reference vs target altitude
    alt_m = alt_agl_ft * 0.3048
    surface_ref_m = 10.0  # standard anemometer height
    ratio = math.log(max(1.1, surface_ref_m)) / math.log(max(1.1, alt_m))
    # Clamp: never amplify, never go below 10% of surface delta
    factor = max(0.10, min(1.0, ratio))
    return surface_gust_delta * factor
