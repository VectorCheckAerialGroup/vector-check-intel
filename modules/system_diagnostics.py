"""
VECTOR CHECK AERIAL GROUP INC. — System Diagnostics

Administrator-only view of ARMS internals: live component status, provider
health, quota pressure, cache state, and per-location capability resolution.

ACCESS: restricted to the VCAG administrator profile. The panel exposes
provider identity, request volumes and configuration presence, none of which
belongs in an operator's brief.

DESIGN RULES
  - READ-ONLY. Nothing here mutates application state. Probes are explicit,
    operator-triggered actions, never run on page load.
  - NO SECRETS. Only presence/absence of credentials is ever reported.
  - NON-BLOCKING. Every check is wrapped; a failed check reports its own
    failure and never propagates an exception into the dashboard.
  - HONEST. "Unknown" is a valid status. Nothing is inferred from silence.
"""

from __future__ import annotations

import logging
import platform
import sys
import time
from datetime import datetime, timezone

logger = logging.getLogger("arms.diagnostics")

ADMIN_OPERATORS = ("VCAG",)

STATUS_OK = "OK"
STATUS_WARN = "DEGRADED"
STATUS_FAIL = "FAIL"
STATUS_OFF = "NOT CONFIGURED"
STATUS_UNKNOWN = "UNKNOWN"

_STATUS_COLOUR = {
    STATUS_OK: "#4ade80",
    STATUS_WARN: "#e8b04a",
    STATUS_FAIL: "#ff6b4a",
    STATUS_OFF: "#6B7280",
    STATUS_UNKNOWN: "#9CA3AF",
}


def is_admin(operator: str | None) -> bool:
    """True only for the VCAG administrator profile."""
    return bool(operator) and operator in ADMIN_OPERATORS


def status_colour(status: str) -> str:
    return _STATUS_COLOUR.get(status, "#9CA3AF")


# ---------------------------------------------------------------------------
# COMPONENT CHECKS — each returns (status, detail). Never raises.
# ---------------------------------------------------------------------------

def check_meteomatics() -> tuple:
    """Credential presence, circuit state, and discovered parameter gaps.
    Makes NO network call — reports in-process state only."""
    try:
        from modules.meteomatics_provider import (
            has_credentials, mm_circuit_open, _MM_CIRCUIT,
            _UNSUPPORTED_CACHE, _ELEV_CACHE, METEOMATICS_BATCH_SIZE,
        )
    except ImportError as e:
        return STATUS_FAIL, f"provider module unavailable: {e}"
    try:
        if not has_credentials():
            return STATUS_OFF, "no credentials in secrets"
        bits = [f"batch size {METEOMATICS_BATCH_SIZE}"]
        if mm_circuit_open():
            remain = max(0, _MM_CIRCUIT.get("open_until", 0) - time.time())
            return STATUS_WARN, (f"circuit OPEN, {remain:.0f}s remaining; "
                                 + "; ".join(bits))
        bits.append("circuit closed")
        if _UNSUPPORTED_CACHE:
            n_sites = len(_UNSUPPORTED_CACHE)
            n_par = sum(len(v) for v in _UNSUPPORTED_CACHE.values())
            bits.append(f"{n_par} unsupported param(s) mapped across "
                        f"{n_sites} site(s)")
        if _ELEV_CACHE:
            bits.append(f"{len(_ELEV_CACHE)} elevation(s) cached")
        return STATUS_OK, "; ".join(bits)
    except Exception as e:
        return STATUS_UNKNOWN, f"state read failed: {e}"


def probe_meteomatics_live(timeout: float = 10.0) -> tuple:
    """EXPLICIT live probe — one tiny authenticated request, bypassing cache
    and circuit. Only ever called from the diagnostic button."""
    import base64
    import urllib.request
    import urllib.error
    try:
        from modules.meteomatics_provider import _get_credentials
        creds = _get_credentials()
        if not creds:
            return STATUS_OFF, "no credentials configured"
        url = "https://api.meteomatics.com/now/t_2m:C/44.16,-77.38/json"
        auth = base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
        req = urllib.request.Request(url, headers={
            "Authorization": f"Basic {auth}",
            "User-Agent": "VectorCheck-ARMS-diag/1.0"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ms = (time.time() - t0) * 1000
                return STATUS_OK, f"HTTP {r.status} in {ms:.0f} ms"
        except urllib.error.HTTPError as e:
            ms = (time.time() - t0) * 1000
            if e.code in (401, 403):
                return STATUS_FAIL, (f"HTTP {e.code} in {ms:.0f} ms — "
                                     "credentials or subscription state")
            if e.code == 429:
                return STATUS_FAIL, (f"HTTP 429 in {ms:.0f} ms — "
                                     "QUOTA EXHAUSTED")
            if e.code == 404:
                return STATUS_WARN, (f"HTTP 404 in {ms:.0f} ms — parameter "
                                     "coverage limit, service is healthy")
            return STATUS_FAIL, f"HTTP {e.code} in {ms:.0f} ms — server side"
        except Exception as e:
            ms = (time.time() - t0) * 1000
            return STATUS_FAIL, f"network failure after {ms:.0f} ms: {e}"
    except Exception as e:
        return STATUS_UNKNOWN, f"probe could not run: {e}"


def check_supabase() -> tuple:
    try:
        import streamlit as st
        cfg = st.secrets.get("supabase", {})
        if not (cfg.get("url") and cfg.get("key")):
            return STATUS_OFF, "no supabase credentials — JSON fallback in use"
        return STATUS_OK, "credentials present (connection is lazy)"
    except Exception as e:
        return STATUS_UNKNOWN, f"config read failed: {e}"


def check_secret(section: str, keys: tuple) -> tuple:
    """Presence-only check. NEVER returns or logs a secret value."""
    try:
        import streamlit as st
        cfg = st.secrets.get(section, {})
        missing = [k for k in keys if not cfg.get(k)]
        if not cfg:
            return STATUS_OFF, "section absent"
        if missing:
            return STATUS_WARN, f"missing key(s): {', '.join(missing)}"
        return STATUS_OK, f"{len(keys)} key(s) present"
    except Exception as e:
        return STATUS_UNKNOWN, f"read failed: {e}"


def check_location_capabilities(lat: float, lon: float) -> list:
    """What ARMS can actually deliver at this coordinate. This is the panel
    that explains 'why is X missing here' without guesswork."""
    rows = []
    try:
        from modules.ensemble_analysis import (
            _select_regional_model, _is_conus_coverage, _is_hrdps_coverage)
        reg, _ = _select_regional_model(lat, lon)
        rows.append(("Regional NWP",
                     STATUS_OK if reg != "Best Match" else STATUS_OFF,
                     reg if reg != "Best Match"
                     else "no regional high-res model covers this site"))
        rows.append(("HRDPS coverage",
                     STATUS_OK if _is_hrdps_coverage(lat, lon) else STATUS_OFF,
                     "in domain" if _is_hrdps_coverage(lat, lon)
                     else "outside 40-75N / 145-50W"))
        rows.append(("CONUS mesoscale",
                     STATUS_OK if _is_conus_coverage(lat, lon) else STATUS_OFF,
                     "HRRR + NAM available" if _is_conus_coverage(lat, lon)
                     else "HRRR/NAM out of domain"))
    except Exception as e:
        rows.append(("Model coverage", STATUS_UNKNOWN, str(e)))

    try:
        from modules.spatial_products import pick_star_view
        sat, cdn, sec, _b = pick_star_view(lat, lon)
        if sec == "NONE":
            rows.append(("Satellite", STATUS_OFF,
                         f"no geostationary coverage (nearest {sat})"))
        else:
            rows.append(("Satellite", STATUS_OK, f"{sat} · sector {sec}"))
    except Exception as e:
        rows.append(("Satellite", STATUS_UNKNOWN, str(e)))

    try:
        from modules.spatial_quad import (nearest_stations, station_in_range,
                                          beam_height_ft,
                                          STATION_MAX_RANGE_KM)
        near = [t for t in nearest_stations(lat, lon, 10)
                if station_in_range(t[2])]
        if near:
            sid, nm, km, _cc = near[0]
            bft = beam_height_ft(km)
            rows.append(("Single-site radar", STATUS_OK,
                         f"{len(near)} in range; nearest {sid} at {km:.0f} km, "
                         f"0.5° beam ≈ {bft:,.0f} ft"))
        else:
            rows.append(("Single-site radar", STATUS_OFF,
                         f"no catalogued site within "
                         f"{int(STATION_MAX_RANGE_KM)} km — composite only"))
    except Exception as e:
        rows.append(("Single-site radar", STATUS_UNKNOWN, str(e)))

    try:
        from modules.meteomatics_provider import (_surface_pressure_hpa,
                                                  _PRESSURE_LEVELS)
        # Elevation is not fetched here (no network in a status panel); the
        # levels shown are those a site at this pressure would retain.
        rows.append(("Isobaric levels", STATUS_OK,
                     f"{len(_PRESSURE_LEVELS)} standard levels; below-ground "
                     "levels are filtered by site elevation at fetch time"))
    except Exception as e:
        rows.append(("Isobaric levels", STATUS_UNKNOWN, str(e)))
    return rows


def check_runtime() -> list:
    """Interpreter, key library versions, and process uptime."""
    rows = [("Python", STATUS_OK, sys.version.split()[0]),
            ("Platform", STATUS_OK, platform.platform())]
    for mod in ("streamlit", "pandas", "numpy", "plotly", "folium",
                "supabase", "bcrypt"):
        try:
            m = __import__(mod)
            rows.append((mod, STATUS_OK, getattr(m, "__version__", "present")))
        except ImportError:
            rows.append((mod, STATUS_OFF, "not installed"))
        except Exception as e:
            rows.append((mod, STATUS_UNKNOWN, str(e)))
    return rows


def collect_all(lat: float, lon: float) -> dict:
    """Assembles the full diagnostic payload. Never raises."""
    out = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
           "providers": [], "config": [], "location": [], "runtime": []}
    try:
        out["providers"].append(("Meteomatics",) + check_meteomatics())
        out["providers"].append(("Open-Meteo", STATUS_OK,
                                 "keyless public API — no stored state"))
        out["providers"].append(("Supabase",) + check_supabase())
        out["config"].append(("[meteomatics]",) +
                             check_secret("meteomatics", ("user", "password")))
        out["config"].append(("[supabase]",) +
                             check_secret("supabase", ("url", "key")))
        out["config"].append(("[synoptic]",) +
                             check_secret("synoptic", ("token",)))
        out["config"].append(("[passwords]",) +
                             check_secret("passwords", ("VCAG",)))
        out["location"] = check_location_capabilities(lat, lon)
        out["runtime"] = check_runtime()
    except Exception as e:
        logger.warning("diagnostics collection partial failure: %s", e)
    return out
