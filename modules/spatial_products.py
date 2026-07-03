"""
VECTOR CHECK AERIAL GROUP INC. — Spatial Products

Live map products for the Spatial tab: high-resolution radar, GOES satellite,
topographic basemaps, and NWP model precipitation — each rendered as a folium
map centred on the selected detachment.

DATA SOURCES (all free/public tile or WMS services)
  Radar:
    - ECCC GeoMet WMS  RADAR_1KM_RRAI  — 1 km Canadian composite rain rate,
      the highest-resolution public radar over Canada (RadarScope-class).
    - RainViewer tile API — global smoothed composite, 10-minute updates.
      Latest frame timestamp is fetched from their public catalog.
  Satellite:
    - NASA GIBS WMTS — GOES-East ABI GeoColor (CIRA). GeoColor is the
      day-visible / night-IR blend, the operational equivalent of the College
      of DuPage "sandwich" product. Clean-IR Band 13 offered as an option.
  Topo:
    - OpenTopoMap XYZ tiles (global contour topo)
    - NRCan Toporama WMS (authoritative Canadian topographic base)
  Model precip:
    - ECCC GeoMet WMS  HRDPS.CONTINENTAL_PR — HRDPS 2.5 km model
      precipitation rate, served directly as WMS imagery. Actual NWP output
      on the map, not a nowcast blend.

All layers are LIVE external services: availability follows the provider.
Map construction is pure client-side tile referencing — ARMS makes only one
tiny server-side call (the RainViewer frame catalog, cached 5 minutes).
"""

from __future__ import annotations

import json
import logging
import urllib.request

import folium
from folium.raster_layers import WmsTileLayer, TileLayer

logger = logging.getLogger("arms.spatial")

# Dark basemap matching the ARMS monochrome aesthetic
_DARK_TILES = "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
_DARK_ATTR = "&copy; OpenStreetMap contributors &copy; CARTO"

GEOMET_WMS = "https://geo.weather.gc.ca/geomet"
GIBS_WMTS = ("https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/"
             "{layer}/default/default/GoogleMapsCompatible_Level{maxz}/{{z}}/{{y}}/{{x}}.png")


def _base_map(lat: float, lon: float, zoom: int = 7,
              tiles: str = "dark", minimal: bool = False) -> folium.Map:
    """Folium map centred on the site with the ARMS dark base (or topo).
    minimal=True strips zoom buttons and the scale bar for clean quad panes
    (Windy/Pivotal-style chrome-free imagery)."""
    m = folium.Map(
        location=[lat, lon], zoom_start=zoom,
        tiles=None, control_scale=not minimal, prefer_canvas=True,
        zoom_control=not minimal,
    )
    if tiles == "dark":
        TileLayer(_DARK_TILES, attr=_DARK_ATTR, name="Base (dark)",
                  max_zoom=20, subdomains="abcd").add_to(m)
    elif tiles == "opentopo":
        TileLayer(
            "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
            attr="&copy; OpenStreetMap, SRTM | style &copy; OpenTopoMap (CC-BY-SA)",
            name="OpenTopoMap", max_zoom=17, subdomains="abc",
        ).add_to(m)
    # Site marker — discreet ring in ARMS amber
    folium.CircleMarker(
        [lat, lon], radius=7, color="#E58E26", weight=2,
        fill=True, fill_opacity=0.15, tooltip="Detachment",
    ).add_to(m)
    return m


def fetch_rainviewer_frame() -> str | None:
    """Latest RainViewer radar frame path (e.g. '/v2/radar/1700000000').
    Caller caches this (5-min TTL) — frames update every 10 minutes."""
    try:
        req = urllib.request.Request(
            "https://api.rainviewer.com/public/weather-maps.json",
            headers={"User-Agent": "VectorCheck-ARMS/2.1"},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            cat = json.load(r)
        frames = (cat.get("radar") or {}).get("past") or []
        if frames:
            return frames[-1].get("path")
    except Exception as e:
        logger.warning("RainViewer catalog fetch failed: %s", e)
    return None


def build_radar_map(lat: float, lon: float, zoom: int,
                    rainviewer_path: str | None,
                    show_geomet: bool = True,
                    show_rainviewer: bool = True,
                    opacity: float = 0.75,
                    minimal: bool = False) -> folium.Map:
    """Premium radar stack, best layer on top:

    1. IEM NEXRAD N0Q composite (Iowa State Mesonet tile cache) — the
       high-quality 1 km base-reflectivity XYZ tiles that most polished web
       radars are built on. Smooth at every zoom (native slippy tiles, no WMS
       rescaling artifacts). NEXRAD coverage extends into southern Canada.
    2. ECCC 1 km rain rate (WMS) — fills Canadian coverage north of NEXRAD.
       Drawn beneath IEM so NEXRAD wins where both exist.
    3. RainViewer global composite — worldwide fallback context.
    """
    m = _base_map(lat, lon, zoom, tiles="dark", minimal=minimal)
    if show_rainviewer and rainviewer_path:
        TileLayer(
            f"https://tilecache.rainviewer.com{rainviewer_path}/512/{{z}}/{{x}}/{{y}}/4/1_1.png",
            attr="RainViewer.com", name="RainViewer composite",
            opacity=opacity * 0.85, max_zoom=12, overlay=True,
        ).add_to(m)
    if show_geomet:
        WmsTileLayer(
            url=GEOMET_WMS,
            layers="RADAR_1KM_RRAI",
            fmt="image/png", transparent=True, version="1.3.0",
            opacity=opacity, name="ECCC 1 km rain rate",
            attr="Environment and Climate Change Canada",
        ).add_to(m)
    # Top layer: IEM NEXRAD N0Q — the premium reflectivity tiles
    TileLayer(
        "https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/nexrad-n0q-900913/{z}/{x}/{y}.png",
        attr="Iowa Environmental Mesonet / NOAA NEXRAD",
        name="NEXRAD N0Q composite", opacity=opacity,
        max_zoom=12, overlay=True,
    ).add_to(m)
    if not minimal:
        folium.LayerControl(collapsed=True).add_to(m)
    return m


def build_satellite_map(lat: float, lon: float, zoom: int,
                        product: str = "GeoColor",
                        opacity: float = 1.0,
                        minimal: bool = False) -> folium.Map:
    """GOES-East via NASA GIBS. GeoColor = day-vis/night-IR blend (the
    operational 'sandwich'); Band 13 = clean IR window."""
    m = _base_map(lat, lon, zoom, tiles="dark", minimal=minimal)
    _SAT_PRODUCTS = {
        "GeoColor": ("GOES-East_ABI_GeoColor", 7, "GOES-East GeoColor"),
        "Visible hi-res": ("GOES-East_ABI_Band2_Red_Visible_1km", 8,
                            "GOES-East Band 2 visible (1 km, daytime)"),
        "Band 13 IR": ("GOES-East_ABI_Band13_Clean_Infrared", 7,
                        "GOES-East Band 13 clean IR"),
    }
    gibs_layer, maxz, nm = _SAT_PRODUCTS.get(product, _SAT_PRODUCTS["GeoColor"])
    # max_native_zoom + higher max_zoom = Leaflet upscales the native tiles by
    # smooth interpolation (soft, Windy-style) instead of hard pixel blocks.
    TileLayer(
        GIBS_WMTS.format(layer=gibs_layer, maxz=maxz),
        attr="NASA GIBS / NOAA GOES-East", name=nm,
        opacity=opacity, max_zoom=12, max_native_zoom=maxz, overlay=True,
    ).add_to(m)
    if not minimal:
        folium.LayerControl(collapsed=True).add_to(m)
    return m


def build_topo_map(lat: float, lon: float, zoom: int,
                   show_toporama: bool = False,
                   minimal: bool = False) -> folium.Map:
    """Terrain-relief topo: Esri World Hillshade (high-resolution shaded
    elevation, ~10-30 m source DEMs) as the base, with the Esri topographic
    layer blended over it so contours/names sit on visible relief. Optional
    NRCan Toporama overlay for authoritative Canadian cartography."""
    m = folium.Map(
        location=[lat, lon], zoom_start=zoom,
        tiles=None, control_scale=not minimal, prefer_canvas=True,
        zoom_control=not minimal,
    )
    TileLayer(
        "https://server.arcgisonline.com/ArcGIS/rest/services/Elevation/"
        "World_Hillshade/MapServer/tile/{z}/{y}/{x}",
        attr="Esri, USGS | World Hillshade", name="Hillshade relief",
        max_zoom=16,
    ).add_to(m)
    TileLayer(
        "https://server.arcgisonline.com/ArcGIS/rest/services/"
        "World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        attr="Esri | World Topographic Map", name="Topo overlay",
        opacity=0.55, max_zoom=16, overlay=True,
    ).add_to(m)
    if show_toporama:
        WmsTileLayer(
            url="https://maps.geogratis.gc.ca/wms/toporama_en",
            layers="WMS-Toporama",
            fmt="image/png", transparent=False, version="1.3.0",
            opacity=0.5, name="NRCan Toporama",
            attr="Natural Resources Canada",
        ).add_to(m)
    folium.CircleMarker(
        [lat, lon], radius=7, color="#E58E26", weight=2,
        fill=True, fill_opacity=0.15, tooltip="Detachment",
    ).add_to(m)
    if not minimal:
        folium.LayerControl(collapsed=True).add_to(m)
    return m


def build_model_precip_map(lat: float, lon: float, zoom: int,
                           layer: str = "HRDPS.CONTINENTAL_PR",
                           opacity: float = 0.7,
                           minimal: bool = False) -> folium.Map:
    """NWP model precipitation served as WMS imagery from ECCC GeoMet.
    Default HRDPS.CONTINENTAL_PR = HRDPS 2.5 km precipitation rate for the
    latest run; GeoMet resolves the current reference time automatically."""
    m = _base_map(lat, lon, zoom, tiles="dark", minimal=minimal)
    WmsTileLayer(
        url=GEOMET_WMS,
        layers=layer,
        fmt="image/png", transparent=True, version="1.3.0",
        opacity=opacity, name=layer,
        attr="ECCC GeoMet — model guidance",
    ).add_to(m)
    if not minimal:
        folium.LayerControl(collapsed=True).add_to(m)
    return m


# Model-precip layer catalog for the selector (name → GeoMet WMS layer id)
MODEL_PRECIP_LAYERS = {
    "HRDPS 2.5 km — precip rate": "HRDPS.CONTINENTAL_PR",
    "HRDPS 2.5 km — 24h accum": "HRDPS.CONTINENTAL_PN-SLC",
    "RDPS 10 km — precip rate": "RDPS.ETA_PR",
    "GDPS 15 km — precip rate": "GDPS.ETA_PR",
}
