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
                             parameter="precip_1h:mm"):
    """Fetches one Meteomatics WMS GetMap image (model=mix) SERVER-SIDE with
    API credentials and returns (data_uri, bounds) for a folium ImageOverlay.
    Browsers refuse credentialed tile URLs, so tiles are not an option — a
    single authenticated image fetch is. Returns (None, None) on failure."""
    try:
        lon_span = 360.0 * width / (256.0 * (2 ** zoom))
        lat_span = lon_span * (height / width) * max(0.2, math.cos(math.radians(lat)))
        b = (lat - lat_span / 2, lon - lon_span / 2,
             lat + lat_span / 2, lon + lon_span / 2)
        url = ("https://api.meteomatics.com/wms?service=WMS&version=1.3.0"
               f"&request=GetMap&layers={parameter}&model=mix"
               f"&crs=EPSG:4326&bbox={b[0]:.4f},{b[1]:.4f},{b[2]:.4f},{b[3]:.4f}"
               f"&width={width}&height={height}&format=image/png&transparent=true")
        auth = base64.b64encode(f"{username}:{password}".encode()).decode()
        req = urllib.request.Request(url, headers={
            "Authorization": f"Basic {auth}",
            "User-Agent": "VectorCheck-ARMS/2.1"})
        with urllib.request.urlopen(req, timeout=15) as r:
            img = r.read()
        if not img or len(img) < 500:
            return None, None
        uri = "data:image/png;base64," + base64.b64encode(img).decode()
        bounds = [[b[0], b[1]], [b[2], b[3]]]
        return uri, bounds
    except Exception as e:
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
