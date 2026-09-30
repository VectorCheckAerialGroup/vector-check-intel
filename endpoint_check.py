#!/usr/bin/env python3
"""
ARMS TIER 3 — EXTERNAL ENDPOINT LIVENESS
Vector Check Aerial Group Inc.

Catches the failure class that no static analysis can see: a third-party
service changing its terms, paths, or auth requirements while ARMS code stays
untouched. CARTO began requiring an API key and served watermarked tiles
across all four Spatial panes without a single line of ARMS changing.

    python3 endpoint_check.py

Run WEEKLY and before any demo. Requires network. Makes no authenticated
calls and needs no credentials — Meteomatics health belongs in the VCAG
diagnostics panel, not here.

EXIT 0 = all reachable and clean, 1 = something needs attention.
"""

from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request

UA = {"User-Agent": "VectorCheck-ARMS-endpoint-check/1.0"}

# Tile endpoints: (label, url, expected content-type prefix)
# Tile coordinates chosen over eastern Ontario so a returned image is real.
TILES = [
    ("Esri dark canvas (quad basemap)",
     "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/"
     "World_Dark_Gray_Base/MapServer/tile/7/44/36", "image/"),
    ("Esri dark reference (labels)",
     "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/"
     "World_Dark_Gray_Reference/MapServer/tile/7/44/36", "image/"),
    ("Esri hillshade dark (elevation)",
     "https://server.arcgisonline.com/ArcGIS/rest/services/Elevation/"
     "World_Hillshade_Dark/MapServer/tile/7/44/36", "image/"),
    ("Esri transportation (elevation roads)",
     "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/"
     "World_Transportation/MapServer/tile/7/44/36", "image/"),
    ("AWS Terrarium DEM (elevation data)",
     "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/7/36/44.png",
     "image/"),
    ("IEM NEXRAD composite",
     "https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/"
     "nexrad-n0q-900913/7/36/44.png", "image/"),
]

# JSON/catalog endpoints: (label, url, substring that must appear)
CATALOGS = [
    ("RainViewer frame catalog (radar + IR loop)",
     "https://api.rainviewer.com/public/weather-maps.json", "radar"),
]

# Directory listings: (label, url, substring)
LISTINGS = [
    ("NOAA STAR GOES-East CONUS Band 13",
     "https://cdn.star.nesdis.noaa.gov/GOES19/ABI/CONUS/13/", ".jpg"),
    ("NOAA STAR GOES-West CONUS Band 13",
     "https://cdn.star.nesdis.noaa.gov/GOES18/ABI/CONUS/13/", ".jpg"),
]

# WMS GetMap checks: request the EXACT layer and parameters ARMS uses and
# require real PNG bytes back. This beats parsing GetCapabilities — that
# document is many megabytes on GeoMet, so any substring check against a
# truncated read reports a false failure. A GetMap either works or it does
# not, which is the only question that matters.
# (label, url, note)
WMS_GETMAP = [
    ("ECCC GeoMet HRDPS precip (MIX pane fallback)",
     "https://geo.weather.gc.ca/geomet?service=WMS&version=1.3.0"
     "&request=GetMap&layers=HRDPS.CONTINENTAL_PR&styles="
     "&crs=EPSG:4326&bbox=43.0,-79.0,45.0,-76.0"
     "&width=256&height=256&format=image/png&transparent=true",
     "layer id HRDPS.CONTINENTAL_PR"),
    ("ECCC GeoMet 1 km radar (Canadian composite)",
     "https://geo.weather.gc.ca/geomet?service=WMS&version=1.3.0"
     "&request=GetMap&layers=RADAR_1KM_RRAI&styles="
     "&crs=EPSG:4326&bbox=43.0,-79.0,45.0,-76.0"
     "&width=256&height=256&format=image/png&transparent=true",
     "layer id RADAR_1KM_RRAI"),
]

# Tiles that are watermarked rather than failed are the dangerous case: HTTP
# 200, valid PNG, useless content. Size heuristics catch the obvious ones.
#
# NOTE: a small radar or precip tile is NORMAL — a fully transparent PNG is
# the correct answer when there is no echo over the requested area. Only
# non-image content types and hard failures are treated as faults for those.
MIN_TILE_BYTES = 200
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

results: list = []


def record(status: str, label: str, detail: str):
    results.append((status, label, detail))
    mark = {"OK": "  ok  ", "WARN": " WARN ", "FAIL": " FAIL "}[status]
    print(f"{mark} {label}\n         {detail}")


def check_tile(label: str, url: str, want_type: str):
    try:
        t0 = time.time()
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read()
            ctype = r.headers.get("Content-Type", "")
            ms = (time.time() - t0) * 1000
        if not ctype.startswith(want_type):
            record("FAIL", label,
                   f"expected {want_type}*, got {ctype!r} ({len(body)} bytes) "
                   "— endpoint may now require a key or have moved")
            return
        if len(body) < MIN_TILE_BYTES:
            record("WARN", label,
                   f"{len(body)} bytes — suspiciously small, possible "
                   "placeholder or watermark tile")
            return
        record("OK", label, f"{ctype}, {len(body):,} bytes, {ms:.0f} ms")
    except urllib.error.HTTPError as e:
        record("FAIL", label, f"HTTP {e.code} — path changed or access revoked")
    except Exception as e:
        record("FAIL", label, f"unreachable: {type(e).__name__}: {e}")


def check_text(label: str, url: str, needle: str, kind: str):
    try:
        t0 = time.time()
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read(400_000).decode("utf-8", "replace")
            ms = (time.time() - t0) * 1000
        if needle not in body:
            record("FAIL", label,
                   f"reachable but {needle!r} not present — {kind} format or "
                   "layer naming has changed")
            return
        record("OK", label, f"{needle!r} present, {ms:.0f} ms")
    except urllib.error.HTTPError as e:
        record("FAIL", label, f"HTTP {e.code}")
    except Exception as e:
        record("FAIL", label, f"unreachable: {type(e).__name__}: {e}")


def check_getmap(label: str, url: str, note: str):
    """A WMS server answering an invalid layer returns an XML ServiceException
    with HTTP 200 — the exact failure that made MIX imagery silently useless
    before the styles= parameter was added. So: verify PNG magic bytes, not
    just the status code."""
    try:
        t0 = time.time()
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read()
            ms = (time.time() - t0) * 1000
        if body[:8] != PNG_MAGIC:
            head = body[:120].decode("utf-8", "replace").replace("\n", " ")
            record("FAIL", label,
                   f"not a PNG — {note} may have changed. Response began: "
                   f"{head!r}")
            return
        # Transparent is fine (no echo); we are testing the contract, not wx.
        record("OK", label, f"valid PNG, {len(body):,} bytes, {ms:.0f} ms")
    except urllib.error.HTTPError as e:
        record("FAIL", label, f"HTTP {e.code} — {note} rejected by server")
    except Exception as e:
        record("FAIL", label, f"unreachable: {type(e).__name__}: {e}")


def main() -> int:
    print("=" * 72)
    print("ARMS EXTERNAL ENDPOINT LIVENESS")
    print("=" * 72)

    print("\n-- Tile services --")
    for label, url, ctype in TILES:
        check_tile(label, url, ctype)

    print("\n-- Catalogs (drive the animation loops) --")
    for label, url, needle in CATALOGS:
        check_text(label, url, needle, "catalog")

    print("\n-- WMS layers (exact layer ids ARMS requests) --")
    for label, url, note in WMS_GETMAP:
        check_getmap(label, url, note)

    print("\n-- Satellite directory listings --")
    for label, url, needle in LISTINGS:
        check_text(label, url, needle, "listing")

    print("\n" + "=" * 72)
    fails = [r for r in results if r[0] == "FAIL"]
    warns = [r for r in results if r[0] == "WARN"]
    print(f"  {len(results) - len(fails) - len(warns)} ok, "
          f"{len(warns)} warning(s), {len(fails)} failure(s)")
    if fails:
        print("\n  Any FAIL means a Spatial pane is broken or silently "
              "degraded right now. Check the pane before the next flight "
              "brief and swap the source if the vendor has changed terms.")
    print("=" * 72)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
