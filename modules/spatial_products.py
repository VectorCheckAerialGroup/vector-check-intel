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
              tiles: str = "dark") -> folium.Map:
    """Folium map centred on the site with the ARMS dark base (or topo)."""
    m = folium.Map(
        location=[lat, lon], zoom_start=zoom,
        tiles=None, control_scale=True, prefer_canvas=True,
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
                    opacity: float = 0.75) -> folium.Map:
    """Radar composite: ECCC 1 km rain rate + RainViewer global smooth."""
    m = _base_map(lat, lon, zoom, tiles="dark")
    if show_geomet:
        WmsTileLayer(
            url=GEOMET_WMS,
            layers="RADAR_1KM_RRAI",
            fmt="image/png", transparent=True, version="1.3.0",
            opacity=opacity, name="ECCC 1 km rain rate",
            attr="Environment and Climate Change Canada",
        ).add_to(m)
    if show_rainviewer and rainviewer_path:
        TileLayer(
            f"https://tilecache.rainviewer.com{rainviewer_path}/512/{{z}}/{{x}}/{{y}}/4/1_1.png",
            attr="RainViewer.com", name="RainViewer composite",
            opacity=opacity, max_zoom=12, overlay=True,
        ).add_to(m)
    folium.LayerControl(collapsed=True).add_to(m)
    return m


def build_satellite_map(lat: float, lon: float, zoom: int,
                        product: str = "GeoColor",
                        opacity: float = 1.0) -> folium.Map:
    """GOES-East via NASA GIBS. GeoColor = day-vis/night-IR blend (the
    operational 'sandwich'); Band 13 = clean IR window."""
    m = _base_map(lat, lon, zoom, tiles="dark")
    if product == "GeoColor":
        gibs_layer, maxz, nm = "GOES-East_ABI_GeoColor", 7, "GOES-East GeoColor"
    else:
        gibs_layer, maxz, nm = "GOES-East_ABI_Band13_Clean_Infrared", 7, "GOES-East Band 13 IR"
    TileLayer(
        GIBS_WMTS.format(layer=gibs_layer, maxz=maxz),
        attr="NASA GIBS / NOAA GOES-East", name=nm,
        opacity=opacity, max_zoom=12, max_native_zoom=maxz, overlay=True,
    ).add_to(m)
    folium.LayerControl(collapsed=True).add_to(m)
    return m


def build_topo_map(lat: float, lon: float, zoom: int,
                   show_toporama: bool = True) -> folium.Map:
    """Topographic base: OpenTopoMap, with NRCan Toporama WMS overlay."""
    m = _base_map(lat, lon, zoom, tiles="opentopo")
    if show_toporama:
        WmsTileLayer(
            url="https://maps.geogratis.gc.ca/wms/toporama_en",
            layers="WMS-Toporama",
            fmt="image/png", transparent=False, version="1.3.0",
            opacity=0.65, name="NRCan Toporama",
            attr="Natural Resources Canada",
        ).add_to(m)
    folium.LayerControl(collapsed=True).add_to(m)
    return m


def build_model_precip_map(lat: float, lon: float, zoom: int,
                           layer: str = "HRDPS.CONTINENTAL_PR",
                           opacity: float = 0.7) -> folium.Map:
    """NWP model precipitation served as WMS imagery from ECCC GeoMet.
    Default HRDPS.CONTINENTAL_PR = HRDPS 2.5 km precipitation rate for the
    latest run; GeoMet resolves the current reference time automatically."""
    m = _base_map(lat, lon, zoom, tiles="dark")
    WmsTileLayer(
        url=GEOMET_WMS,
        layers=layer,
        fmt="image/png", transparent=True, version="1.3.0",
        opacity=opacity, name=layer,
        attr="ECCC GeoMet — model guidance",
    ).add_to(m)
    folium.LayerControl(collapsed=True).add_to(m)
    return m


# Model-precip layer catalog for the selector (name → GeoMet WMS layer id)
MODEL_PRECIP_LAYERS = {
    "HRDPS 2.5 km — precip rate": "HRDPS.CONTINENTAL_PR",
    "HRDPS 2.5 km — 24h accum": "HRDPS.CONTINENTAL_PN-SLC",
    "RDPS 10 km — precip rate": "RDPS.ETA_PR",
    "GDPS 15 km — precip rate": "GDPS.ETA_PR",
}
