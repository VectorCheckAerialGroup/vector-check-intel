"""
VECTOR CHECK AERIAL GROUP INC. — Spatial Products (v4)

Chrome-free animated map panes for the Spatial workspace:
  Radar      — IEM NEXRAD N0Q + RainViewer, looping the recent frames
  Satellite  — GOES-East GeoColor loop at native resolution (no overzoom blur)
  Elevation  — hypsometric colour elevation (ASTER GDEM colour shaded relief)
               over Esri hillshade: a true elevation heat map with relief
  MIX precip — Meteomatics MIX precipitation fetched server-side (WMS GetMap
               with API credentials; browsers block credentialed tile URLs,
               so the image is embedded as an overlay instead)

All panes suppress the Leaflet attribution bar (minimal mode) for clean
imagery; source credit lives in the workspace caption instead.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import urllib.request

import folium
from folium.raster_layers import WmsTileLayer, TileLayer, ImageOverlay
from branca.element import Element

logger = logging.getLogger("arms.spatial")

_DARK_TILES = "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
_DARK_ATTR = "CARTO / OSM"
GEOMET_WMS = "https://geo.weather.gc.ca/geomet"
GIBS_WMTS = ("https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/"
             "{layer}/default/{time}/GoogleMapsCompatible_Level{maxz}/{{z}}/{{y}}/{{x}}.png")


def _base_map(lat, lon, zoom, tiles="dark", minimal=False, max_zoom=20):
    m = folium.Map(
        location=[lat, lon], zoom_start=zoom, tiles=None,
        control_scale=not minimal, prefer_canvas=True,
        zoom_control=not minimal, attributionControl=not minimal,
        max_zoom=max_zoom,
    )
    if tiles == "dark":
        TileLayer(_DARK_TILES, attr=_DARK_ATTR, name="Base",
                  max_zoom=20, subdomains="abcd").add_to(m)
    folium.CircleMarker([lat, lon], radius=7, color="#E58E26", weight=2,
                        fill=True, fill_opacity=0.15).add_to(m)
    return m


def _add_frame_loop(m, layers, opacity: float, interval_ms: int = 800):
    """Animates a list of TileLayers by cycling opacity — a lightweight
    tile-layer loop (all frames stay loaded; no flicker)."""
    if len(layers) < 2:
        if layers:
            layers[0].options["opacity"] = opacity
        return
    names = ",".join(l.get_name() for l in layers)
    js = f"""
    setTimeout(function() {{
        var frames = [{names}];
        var op = {opacity};
        var i = frames.length - 1;
        frames.forEach(function(l, j) {{ l.setOpacity(j === i ? op : 0); }});
        setInterval(function() {{
            i = (i + 1) % frames.length;
            frames.forEach(function(l, j) {{ l.setOpacity(j === i ? op : 0); }});
        }}, {interval_ms});
    }}, 1200);
    """
    m.get_root().script.add_child(Element(js))


# ---------------------------------------------------------------- RADAR ----
def fetch_rainviewer_frames(n: int = 5) -> list:
    """Last n RainViewer frame paths (10-minute cadence)."""
    try:
        req = urllib.request.Request(
            "https://api.rainviewer.com/public/weather-maps.json",
            headers={"User-Agent": "VectorCheck-ARMS/2.1"})
        with urllib.request.urlopen(req, timeout=8) as r:
            cat = json.load(r)
        past = (cat.get("radar") or {}).get("past") or []
        return [f.get("path") for f in past[-n:] if f.get("path")]
    except Exception as e:
        logger.warning("RainViewer catalog failed: %s", e)
        return []


def fetch_rainviewer_catalog(n: int = 6) -> dict:
    """Exact frame catalogs from RainViewer: the last n radar frames AND the
    last n GOES infrared satellite frames, each with its true unix timestamp.
    Animating only catalogued frames is what makes loops reliable — no
    guessed timestamps, no 404-invisible frames."""
    try:
        req = urllib.request.Request(
            "https://api.rainviewer.com/public/weather-maps.json",
            headers={"User-Agent": "VectorCheck-ARMS/2.1"})
        with urllib.request.urlopen(req, timeout=8) as r:
            cat = json.load(r)
        radar = [{"path": f["path"], "ts": f["time"]}
                 for f in ((cat.get("radar") or {}).get("past") or [])[-n:]]
        sat = [{"path": f["path"], "ts": f["time"]}
               for f in ((cat.get("satellite") or {}).get("infrared") or [])[-n:]]
        return {"radar": radar, "sat": sat}
    except Exception as e:
        logger.warning("RainViewer catalog failed: %s", e)
        return {"radar": [], "sat": []}


def fetch_rainviewer_frame() -> str | None:
    """Back-compat single latest frame."""
    fr = fetch_rainviewer_frames(1)
    return fr[-1] if fr else None


def build_radar_map(lat, lon, zoom, rv_frames=None, opacity=0.8,
                    minimal=True, loop=True, **_kw):
    """Looping radar: IEM NEXRAD N0Q frames (5-min archive tiles) animated,
    with the latest RainViewer composite beneath for out-of-network areas."""
    m = _base_map(lat, lon, zoom, minimal=minimal)
    rv_frames = rv_frames or []
    if rv_frames:
        TileLayer(
            f"https://tilecache.rainviewer.com{rv_frames[-1]}/512/{{z}}/{{x}}/{{y}}/4/1_1.png",
            attr="RainViewer", opacity=opacity * 0.7, max_zoom=12).add_to(m)
    # IEM N0Q archive suffixes: now, -15, -30, -45 minutes -> 15-min loop
    suffixes = ["-m45m", "-m30m", "-m15m", ""] if loop else [""]
    frames = []
    for suf in suffixes:
        lyr = TileLayer(
            f"https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/"
            f"nexrad-n0q-900913{suf}/{{z}}/{{x}}/{{y}}.png",
            attr="IEM/NEXRAD", opacity=0 if loop else opacity, max_zoom=12)
        lyr.add_to(m)
        frames.append(lyr)
    if loop:
        _add_frame_loop(m, frames, opacity)
    return m


# ------------------------------------------------------------ SATELLITE ----
def build_satellite_map(lat, lon, zoom, product="GeoColor", times=None,
                        minimal=True, **_kw):
    """GOES-East loop at NATIVE resolution only — max zoom is pinned to the
    sensor's tile level so imagery is never upscaled into blur. `times` is a
    list of GIBS ISO timestamps (15-min steps) to animate; empty -> latest."""
    prods = {
        "GeoColor": ("GOES-East_ABI_GeoColor", 7),
        "Band 13 IR": ("GOES-East_ABI_Band13_Clean_Infrared", 7),
    }
    layer_id, maxz = prods.get(product, prods["GeoColor"])
    zoom = min(zoom, maxz)
    m = _base_map(lat, lon, zoom, minimal=minimal, max_zoom=maxz)
    times = times or ["default"]
    frames = []
    for t in times:
        lyr = TileLayer(
            GIBS_WMTS.format(layer=layer_id, time=t, maxz=maxz),
            attr="NASA GIBS/NOAA", opacity=0 if len(times) > 1 else 1.0,
            max_zoom=maxz)
        lyr.add_to(m)
        frames.append(lyr)
    if len(frames) > 1:
        _add_frame_loop(m, frames, 1.0)
    return m


# ------------------------------------------------------------- ELEVATION ----
def build_elevation_map(lat, lon, zoom, minimal=True, **_kw):
    """High-resolution elevation heat map: ASTER GDEM colour shaded relief
    (hypsometric tint — blue/green lowlands through amber ridgelines) with
    Esri World Hillshade multiplied over it for crisp terrain relief."""
    m = folium.Map(location=[lat, lon], zoom_start=zoom, tiles=None,
                   control_scale=not minimal, zoom_control=not minimal,
                   attributionControl=not minimal, max_zoom=12)
    TileLayer(
        GIBS_WMTS.format(layer="ASTER_GDEM_Color_Shaded_Relief",
                         time="default", maxz=12),
        attr="NASA GIBS/ASTER", max_zoom=12).add_to(m)
    TileLayer(
        "https://server.arcgisonline.com/ArcGIS/rest/services/Elevation/"
        "World_Hillshade/MapServer/tile/{z}/{y}/{x}",
        attr="Esri/USGS", opacity=0.45, max_zoom=16).add_to(m)
    folium.CircleMarker([lat, lon], radius=7, color="#E58E26", weight=2,
                        fill=True, fill_opacity=0.15).add_to(m)
    return m


# Back-compat alias
build_topo_map = build_elevation_map


# ------------------------------------------------------------ MIX PRECIP ----
def fetch_mix_precip_overlay(lat, lon, zoom, username, password,
                             width=1024, height=720,
                             parameter="precip_1h:mm",
                             valid_iso=None):
    """Fetches one Meteomatics WMS GetMap image (model=mix) SERVER-SIDE with
    API credentials and returns (data_uri, bounds) for a folium ImageOverlay.
    Browsers refuse credentialed tile URLs, so tiles are not an option — a
    single authenticated image fetch is. Returns (None, None) on failure.

    Breaker-aware: skips instantly while the Meteomatics circuit is open, and
    reports network failures to the breaker so a dead provider can never make
    the Spatial workspace hang."""
    try:
        from modules.meteomatics_provider import mm_circuit_open
        if mm_circuit_open():
            return None, None
    except ImportError:
        mm_circuit_open = None
    try:
        lon_span = 360.0 * width / (256.0 * (2 ** zoom))
        lat_span = lon_span * (height / width) * max(0.2, math.cos(math.radians(lat)))
        b = (lat - lat_span / 2, lon - lon_span / 2,
             lat + lat_span / 2, lon + lon_span / 2)
        # styles= is MANDATORY in WMS GetMap — omitting it makes servers
        # return an XML exception document, which downstream code was
        # treating as an image (the corrupt/stacked overlays). TIME= per
        # the Meteomatics WMS documentation.
        url = ("https://api.meteomatics.com/wms?service=WMS&version=1.3.0"
               f"&request=GetMap&layers={parameter}&styles=&model=mix"
               f"&crs=EPSG:4326&bbox={b[0]:.4f},{b[1]:.4f},{b[2]:.4f},{b[3]:.4f}"
               f"&width={width}&height={height}&format=image/png&transparent=true")
        if valid_iso:
            url += f"&TIME={valid_iso}"
        auth = base64.b64encode(f"{username}:{password}".encode()).decode()
        req = urllib.request.Request(url, headers={
            "Authorization": f"Basic {auth}",
            "User-Agent": "VectorCheck-ARMS/2.1"})
        from modules.meteomatics_provider import _MM_GATE
        with _MM_GATE:
            with urllib.request.urlopen(req, timeout=15) as r:
                img = r.read()
        # Must be a real PNG (magic bytes) — an XML exception or HTML error
        # page must never be embedded as imagery.
        if not img or img[:8] != b"\x89PNG\r\n\x1a\n":
            logger.warning("MIX WMS returned non-PNG response (%d bytes)",
                           len(img) if img else 0)
            return None, None
        uri = "data:image/png;base64," + base64.b64encode(img).decode()
        bounds = [[b[0], b[1]], [b[2], b[3]]]
        return uri, bounds
    except urllib.error.HTTPError as e:
        # 4xx = service alive (bad param/auth) — never trips the breaker
        logger.warning("MIX WMS overlay HTTP %s", getattr(e, "code", "?"))
        return None, None
    except Exception as e:
        # Deliberately NOT reported to the circuit breaker: the WMS image
        # service is a different subsystem from the forecast API, and spatial
        # imagery must never be able to mark the forecast provider as down.
        logger.warning("MIX WMS overlay fetch failed: %s", e)
        return None, None


def build_mix_precip_map(lat, lon, zoom, overlay_uri=None, overlay_bounds=None,
                         opacity=0.75, minimal=True, **_kw):
    """Meteomatics MIX precipitation pane. If the authenticated overlay is
    unavailable, falls back to GeoMet HRDPS precip so the pane never blanks."""
    m = _base_map(lat, lon, zoom, minimal=minimal)
    if overlay_uri and overlay_bounds:
        ImageOverlay(image=overlay_uri, bounds=overlay_bounds,
                     opacity=opacity, name="MIX precip").add_to(m)
    else:
        WmsTileLayer(url=GEOMET_WMS, layers="HRDPS.CONTINENTAL_PR",
                     fmt="image/png", transparent=True, version="1.3.0",
                     opacity=opacity, attr="ECCC GeoMet").add_to(m)
    return m


def build_model_precip_map(lat, lon, zoom, layer="HRDPS.CONTINENTAL_PR",
                           opacity=0.7, minimal=False, **_kw):
    """Legacy GeoMet model-precip builder (kept for compatibility)."""
    m = _base_map(lat, lon, zoom, minimal=minimal)
    WmsTileLayer(url=GEOMET_WMS, layers=layer, fmt="image/png",
                 transparent=True, version="1.3.0", opacity=opacity,
                 attr="ECCC GeoMet").add_to(m)
    if not minimal:
        folium.LayerControl(collapsed=True).add_to(m)
    return m


MODEL_PRECIP_LAYERS = {
    "HRDPS 2.5 km — precip rate": "HRDPS.CONTINENTAL_PR",
    "HRDPS 2.5 km — 24h accum": "HRDPS.CONTINENTAL_PN-SLC",
    "RDPS 10 km — precip rate": "RDPS.ETA_PR",
    "GDPS 15 km — precip rate": "GDPS.ETA_PR",
}


# ------------------------------------------------------- SINGLE-STATION ----
# NEXRAD sites relevant to Canadian-border and detachment operations.
# id -> (name, lat, lon). Products via IEM RIDGE single-site tile cache.
NEXRAD_STATIONS = {
    "KTYX": ("Fort Drum / Montague NY", 43.756, -75.680),
    "KBUF": ("Buffalo NY", 42.949, -78.737),
    "KBGM": ("Binghamton NY", 42.200, -75.985),
    "KENX": ("Albany NY", 42.586, -74.064),
    "KCXX": ("Burlington VT", 44.511, -73.166),
    "KCBW": ("Caribou ME", 46.039, -67.806),
    "KGYX": ("Portland ME", 43.891, -70.256),
    "KDTX": ("Detroit MI", 42.700, -83.472),
    "KAPX": ("Gaylord MI", 44.906, -84.720),
    "KMQT": ("Marquette MI", 46.531, -87.548),
    "KDLH": ("Duluth MN", 46.837, -92.210),
    "KMVX": ("Grand Forks ND", 47.528, -97.325),
    "KBIS": ("Bismarck ND", 46.771, -100.760),
    "KMBX": ("Minot ND", 48.393, -100.865),
    "KGGW": ("Glasgow MT", 48.206, -106.625),
    "KTFX": ("Great Falls MT", 47.460, -111.385),
    "KOTX": ("Spokane WA", 47.680, -117.627),
    "KCLE": ("Cleveland OH", 41.413, -81.860),
    "KGRB": ("Green Bay WI", 44.499, -88.111),
    "KPBZ": ("Pittsburgh PA", 40.532, -80.218),
}

# Lowest-tilt (0.5 deg) products — precipitation beam + Doppler velocity
STATION_PRODUCTS = {
    "Reflectivity 0.5\u00b0 (N0Q)": "N0Q",
    "Velocity 0.5\u00b0 Doppler (N0U)": "N0U",
}


def nearest_stations(lat: float, lon: float, n: int = 8) -> list:
    """Nearest NEXRAD stations as [(id, name, km), ...] by great-circle."""
    out = []
    for sid, (nm, slat, slon) in NEXRAD_STATIONS.items():
        dlat = math.radians(slat - lat)
        dlon = math.radians(slon - lon)
        a = (math.sin(dlat / 2) ** 2 +
             math.cos(math.radians(lat)) * math.cos(math.radians(slat)) *
             math.sin(dlon / 2) ** 2)
        km = 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))
        out.append((sid, nm, km))
    out.sort(key=lambda x: x[2])
    return out[:n]


def beam_height_ft(dist_km: float, elev_deg: float = 0.5) -> float:
    """Radar beam centreline height (ft) above the radar level at a given
    range, using the standard 4/3-effective-earth-radius propagation model:
        h = r*sin(theta) + r^2 / (2 * (4/3)*Re)
    Ignores radar tower height (~20-30 m) and site elevation differences —
    adequate for operational beam-overshoot reasoning."""
    r_m = dist_km * 1000.0
    re_eff = (4.0 / 3.0) * 6.371e6
    h_m = r_m * math.sin(math.radians(elev_deg)) + (r_m ** 2) / (2.0 * re_eff)
    return h_m * 3.28084


def build_station_radar_map(site_lat: float, site_lon: float,
                            station_id: str, product: str = "N0Q",
                            opacity: float = 0.85,
                            minimal: bool = True,
                            loop: bool = True) -> folium.Map:
    """Single-site NEXRAD view: the station's own lowest-tilt imagery (via
    IEM RIDGE tiles), the radar location marked, and range rings at
    60 / 120 / 180 / 230 km (230 km = N0Q product range). Centred between
    the operating site and the radar so both stay in view."""
    st_nm, st_lat, st_lon = NEXRAD_STATIONS.get(
        station_id, (station_id, site_lat, site_lon))
    c_lat = (site_lat + st_lat) / 2
    c_lon = (site_lon + st_lon) / 2
    m = _base_map(c_lat, c_lon, 7, minimal=minimal)
    # RIDGE keeps the 5 most recent volume scans as frame indices 4 (oldest)
    # .. 0 (latest) — a ~20-25 minute loop at typical VCP update rates.
    frame_idx = [4, 3, 2, 1, 0] if loop else [0]
    frames = []
    for fi in frame_idx:
        lyr = TileLayer(
            f"https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/"
            f"ridge::{station_id}-{product}-{fi}/{{z}}/{{x}}/{{y}}.png",
            attr="IEM RIDGE / NOAA NEXRAD",
            opacity=0 if loop else opacity, max_zoom=12,
        )
        lyr.add_to(m)
        frames.append(lyr)
    if loop:
        _add_frame_loop(m, frames, opacity)
    # Radar site marker + range rings
    folium.CircleMarker([st_lat, st_lon], radius=5, color="#4ade80",
                        weight=2, fill=True, fill_opacity=0.9,
                        tooltip=f"{station_id} \u2014 {st_nm}").add_to(m)
    for rk in (60, 120, 180, 230):
        folium.Circle([st_lat, st_lon], radius=rk * 1000,
                      color="#4ade80", weight=1, opacity=0.35,
                      fill=False, dash_array="4 6",
                      tooltip=f"{rk} km").add_to(m)
    # Operating site marker (amber, consistent with the rest of ARMS)
    folium.CircleMarker([site_lat, site_lon], radius=7, color="#E58E26",
                        weight=2, fill=True, fill_opacity=0.15,
                        tooltip="Detachment").add_to(m)
    return m


def fetch_mix_precip_frames(lat, lon, zoom, username, password, times):
    """Fetches a MIX overlay per timestamp for the quad loop. Returns
    (list_of_data_uris, bounds) — bounds identical across frames. Frames that
    fail fetch are skipped; empty list means caller falls back."""
    uris, bounds = [], None
    for t in times:
        uri, b = fetch_mix_precip_overlay(lat, lon, zoom, username, password,
                                          valid_iso=t)
        if uri:
            uris.append(uri)
            bounds = b
        else:
            # First failure -> stop immediately. Four sequential timeouts
            # against an unavailable WMS would stall the Spatial page for
            # a minute; one probe answers the question.
            break
    if not uris:
        # Timed requests failing but the service may still serve the plain
        # latest image (the originally-proven call, no time parameter).
        # A static latest MIX beats the HRDPS fallback every time.
        uri, b = fetch_mix_precip_overlay(lat, lon, zoom, username, password)
        if uri:
            return [uri], b
    return uris, bounds


def fetch_ridge_scans(station: str, product: str = "N0Q", n: int = 5) -> list:
    """Confirmed volume scans for a single NEXRAD site from IEM's JSON
    catalog. Returns [{"index": i, "ts": unix}, ...] oldest-first, where
    index i maps to the RIDGE tile frame ridge::SITE-PROD-i (0 = newest).
    Empty list -> caller falls back; never animate unconfirmed frames."""
    try:
        url = ("https://mesonet.agron.iastate.edu/json/radar"
               f"?operation=list&radar={station}&product={product}")
        req = urllib.request.Request(url, headers={
            "User-Agent": "VectorCheck-ARMS/2.1"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.load(r)
        scans = data.get("scans") or []
        out = []
        from datetime import datetime as _dt, timezone as _tz
        # newest-last in IEM ordering variants — normalize by parsing ts
        parsed = []
        for sc in scans:
            t = sc.get("ts") or sc.get("timestamp")
            if not t:
                continue
            try:
                dt = _dt.strptime(t.replace("Z", ""), "%Y-%m-%dT%H:%M")
            except ValueError:
                try:
                    dt = _dt.strptime(t.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    continue
            parsed.append(dt.replace(tzinfo=_tz.utc))
        parsed.sort()
        recent = parsed[-n:]
        total = len(recent)
        for k, dt in enumerate(recent):
            # newest scan = index 0, previous = 1, ...
            out.append({"index": total - 1 - k, "ts": int(dt.timestamp())})
        return out
    except Exception as e:
        logger.warning("RIDGE scan catalog failed for %s: %s", station, e)
        return []


# ---------------------------------------------------- STAR SATELLITE ----
# CoD-class pre-rendered sector imagery, from the origin: NOAA STAR CDN.
# Worldwide via nearest geostationary bird; regional sectors where defined,
# full disk elsewhere. Bands: 13 (IR w/ colorbar), 02 (vis), GEOCOLOR,
# Sandwich. Frames are hotlinked (browser loads them), ARMS only fetches
# the directory listing to learn which frames exist.
import re as _re

STAR_BASE = "https://cdn.star.nesdis.noaa.gov"
STAR_SATS = [
    # (name, cdn_dir, sub-satellite lon, sectors [(id, latmin,latmax,lonmin,lonmax)])
    ("GOES-East", "GOES19", -75.2, [
        ("ne",  36.0, 48.5, -82.5, -66.0),
        ("cgl", 40.0, 50.5, -94.0, -75.0),
        ("se",  24.0, 38.0, -91.0, -75.0),
        ("sp",  25.0, 39.0, -107.0, -90.0),
        ("nr",  38.0, 52.0, -117.0, -95.0),
        ("sr",  28.0, 42.0, -117.0, -100.0),
        ("can", 47.0, 62.0, -125.0, -65.0),
        ("CONUS", 20.0, 55.0, -130.0, -60.0),
    ]),
    ("GOES-West", "GOES18", -137.0, [
        ("wus", 30.0, 50.0, -130.0, -110.0),
        ("ak",  50.0, 72.0, -170.0, -130.0),
        ("hi",  15.0, 26.0, -162.0, -152.0),
        ("CONUS", 20.0, 55.0, -140.0, -100.0),
    ]),
    ("Himawari", "HIMAWARI", 140.7, [
        ("jp", 24.0, 46.0, 125.0, 150.0),
        ("aus", -45.0, -10.0, 110.0, 155.0),
    ]),
]
STAR_BANDS = {"IR 13": "13", "Vis 02": "02",
              "GeoColor": "GEOCOLOR", "Sandwich": "Sandwich"}


def pick_star_view(lat: float, lon: float):
    """Nearest bird by sub-satellite longitude; smallest sector containing
    the point, else full disk. Returns (sat_name, cdn_dir, sector_id)."""
    def londist(a, b):
        d = abs(a - b) % 360
        return min(d, 360 - d)
    # Preference: named regional sector (purpose-built view) > CONUS-class
    # wide sector > full disk from the nearest bird. A dedicated sector from
    # the farther bird beats a wide-sector edge view from the nearer one
    # (e.g. Cold Lake: GOES-East 'can' over GOES-West CONUS edge).
    candidates = []
    for name, cdn, slon, sectors in STAR_SATS:
        d = londist(lon, slon)
        for sid, la0, la1, lo0, lo1 in sectors:
            if la0 <= lat <= la1 and lo0 <= lon <= lo1:
                named = 0 if sid not in ("CONUS", "FD") else 1
                area = (la1 - la0) * (lo1 - lo0)
                candidates.append((named, area, d, name, cdn, sid))
    if candidates:
        candidates.sort()
        _, _, _, name, cdn, sid = candidates[0]
        for n2, c2, _sl, secs in STAR_SATS:
            if c2 == cdn:
                for s2, la0, la1, lo0, lo1 in secs:
                    if s2 == sid:
                        return name, cdn, sid, (la0, la1, lo0, lo1)
        return name, cdn, sid, None
    name, cdn, _slon, _secs = min(STAR_SATS, key=lambda s: londist(lon, s[2]))
    return name, cdn, "FD", None


def fetch_star_frames(cdn_dir: str, sector: str, band: str, n: int = 6):
    """Last n frame URLs+times from the STAR directory listing. Filenames
    embed YYYYDDDHHMM; we animate only frames the listing confirms."""
    try:
        base = f"{STAR_BASE}/{cdn_dir}/ABI/"
        base += "FD/" if sector == "FD" else f"SECTOR/{sector}/"
        base += f"{band}/"
        req = urllib.request.Request(base, headers={
            "User-Agent": "VectorCheck-ARMS/2.1"})
        with urllib.request.urlopen(req, timeout=10) as r:
            listing = r.read().decode("utf-8", "replace")
        # Prefer 1200px-class files; FD uses 1808; fall back to any size
        pat = _re.compile(r'href="((\d{11})_[^"]*?-(1200x1200|1808x1808)\.jpg)"')
        hits = pat.findall(listing)
        if not hits:
            pat = _re.compile(r'href="((\d{11})_[^"]*?\.jpg)"')
            hits = [(f, t, "") for f, t in pat.findall(listing)]
        seen, frames = set(), []
        for fname, tstr, _res in hits:
            if tstr in seen:
                continue
            seen.add(tstr)
            from datetime import datetime as _dt, timezone as _tz
            dt = _dt.strptime(tstr, "%Y%j%H%M").replace(tzinfo=_tz.utc)
            frames.append({"url": base + fname, "ts": int(dt.timestamp())})
        frames.sort(key=lambda f: f["ts"])
        return frames[-n:]
    except Exception as e:
        logger.warning("STAR listing failed %s/%s/%s: %s",
                       cdn_dir, sector, band, e)
        return []
