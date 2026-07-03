"""
VECTOR CHECK AERIAL GROUP INC. — Magnetic Declination Helper

Computes magnetic declination (variation) for any location on Earth.

PRIMARY PATH:
    Uses pygeomag (https://pypi.org/project/pygeomag/) which is a pure-Python
    port of the official NOAA World Magnetic Model (WMM). The package bundles
    the WMM 2025 coefficient file and requires no external data. Accurate to
    ~0.5° globally, valid 2025.0 - 2030.0.

FALLBACK PATH:
    If pygeomag is not installed OR fails to initialize, falls back to a
    coarse 15° lookup table (typical error 2-5°). Still within Kestrel 5500
    vane tolerance (~±5°) but much less precise.

SIGN CONVENTION:
    Positive = East declination (magnetic north is east of true north)
    Negative = West declination (magnetic north is west of true north)

    To convert a magnetic bearing to true bearing:
        true_bearing = magnetic_bearing + declination
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger("arms.geomag")

# Try to import and initialize pygeomag. The library ships a default WMM
# coefficient file with the package, so no external file is required.
_geomag_instance = None
try:
    from pygeomag import GeoMag
    # No coefficients_file argument = uses packaged default (WMM 2025)
    _geomag_instance = GeoMag()
    _geomag_available = True
except Exception as e:
    _geomag_available = False
    logger.info(
        "pygeomag unavailable (%s); falling back to coarse declination lookup. "
        "Install with: pip install pygeomag",
        e,
    )


# =============================================================================
# FALLBACK LOOKUP TABLE — coarse 15° grid, WMM 2025 epoch
# Used only when pygeomag is unavailable. Not as accurate, but works offline.
# =============================================================================

_FALLBACK_GRID = {
    # North America
    (60, -135): 17.0, (60, -120): 14.0, (60, -105): 10.0, (60, -90): -1.0, (60, -75): -18.0, (60, -60): -28.0,
    (45, -120): 14.0, (45, -105): 9.0, (45, -90): -1.0, (45, -75): -14.0, (45, -60): -21.0,
    (30, -105): 7.0, (30, -90): -1.0, (30, -75): -11.0,
    # South America
    (15, -75): -7.0, (0, -75): -8.0, (-15, -75): -6.0, (-30, -60): -11.0, (-45, -75): -5.0,
    # Europe / Africa
    (60, 0): -2.0, (60, 15): 7.0, (60, 30): 13.0, (60, 45): 14.0,
    (45, -15): -6.0, (45, 0): 1.0, (45, 15): 5.0, (45, 30): 8.0, (45, 45): 8.0,
    (30, 0): 1.0, (30, 15): 4.0, (30, 30): 5.0, (30, 45): 5.0,
    (15, 0): -4.0, (15, 15): 0.0, (15, 30): 3.0, (15, 45): 3.0,
    (0, 15): -4.0, (0, 30): 0.0, (0, 45): 0.0,
    (-15, 15): -12.0, (-15, 30): -5.0, (-15, 45): -5.0,
    (-30, 15): -18.0, (-30, 30): -18.0,
    # Asia
    (45, 60): 9.0, (45, 75): 7.0, (45, 90): 8.0, (45, 105): 8.0, (45, 120): 7.0, (45, 135): 6.0, (45, 150): 4.0,
    (30, 60): 4.0, (30, 75): 2.0, (30, 90): 1.0, (30, 105): 0.0, (30, 120): 2.0, (30, 135): 2.0,
    (15, 60): 2.0, (15, 75): 0.0, (15, 90): -1.0, (15, 105): -1.0, (15, 120): -2.0,
    # Australia / NZ / Pacific
    (-15, 120): 2.0, (-15, 135): 5.0, (-15, 150): 8.0, (-15, 165): 12.0,
    (-30, 120): 1.0, (-30, 135): 9.0, (-30, 150): 13.0, (-30, 165): 18.0,
    (-45, 150): 14.0, (-45, 165): 21.0, (-45, 180): 25.0,
    # Pacific / Alaska
    (60, -165): 14.0, (60, -150): 18.0,
    (45, -165): 9.0, (45, -150): 13.0,
    # Polar (rough)
    (75, 0): 12.0, (75, 90): 22.0, (75, -90): -20.0, (75, 180): -5.0,
    (-75, 0): -50.0, (-75, 90): 120.0, (-75, -90): 40.0, (-75, 180): 150.0,
}


def _fallback_lookup(lat: float, lon: float) -> float:
    """Inverse-distance-weighted interpolation from the coarse grid."""
    while lon > 180: lon -= 360
    while lon < -180: lon += 360

    total_w = 0.0
    total_wv = 0.0
    for (g_lat, g_lon), val in _FALLBACK_GRID.items():
        dlat = lat - g_lat
        dlon = lon - g_lon
        if dlon > 180: dlon -= 360
        if dlon < -180: dlon += 360
        d = (dlat * dlat + dlon * dlon) ** 0.5
        if d < 0.1:
            return round(val, 1)
        w = 1.0 / (d ** 3)
        total_w += w
        total_wv += w * val

    if total_w == 0:
        return 0.0
    return round(total_wv / total_w, 1)


# =============================================================================
# PUBLIC API
# =============================================================================

def get_magnetic_declination(lat: float, lon: float, date: datetime = None) -> float:
    """Returns magnetic declination in degrees for the given location.

    Args:
        lat:  Latitude in decimal degrees (-90 to 90)
        lon:  Longitude in decimal degrees (-180 to 180)
        date: Date for the calculation (defaults to today)

    Returns:
        Declination in degrees. Positive = East, Negative = West.

        With pygeomag: typical accuracy < 0.5°.
        Without pygeomag: typical accuracy 2-5°.
    """
    if date is None:
        date = datetime.now(timezone.utc)

    # Convert to decimal year for WMM. Normalize to tz-aware UTC first so the
    # anchor subtraction below never mixes naive and aware datetimes.
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    year_start = datetime(date.year, 1, 1, tzinfo=timezone.utc)
    year_end = datetime(date.year + 1, 1, 1, tzinfo=timezone.utc)
    year_frac = date.year + (date - year_start).total_seconds() / (year_end - year_start).total_seconds()

    if _geomag_available and _geomag_instance is not None:
        try:
            result = _geomag_instance.calculate(glat=lat, glon=lon, alt=0, time=year_frac)
            return round(result.d, 1)
        except Exception as e:
            logger.warning("pygeomag calculation failed at %f,%f: %s — falling back", lat, lon, e)

    return _fallback_lookup(lat, lon)
