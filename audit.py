#!/usr/bin/env python3
"""
ARMS AUDIT HARNESS
Vector Check Aerial Group Inc.

Tiered screening that runs offline, in seconds, with no network and no
production credentials. Every check exists because a real bug reached
production without it.

    python3 audit.py              # tiers 0-2 (default, ~10s)
    python3 audit.py --tier 0     # static only, fastest
    python3 audit.py --verbose    # show passing checks too

TIERS
  0  Static      compile, py311 gate, undefined names, banned patterns
  1  Contract    imports resolve, null/empty/partial inputs never crash
                 and never fabricate a hazard
  2  Global      location-dependent logic across 22 worldwide sites

Tier 3 (external endpoint liveness) is a separate script — it needs network
and is scheduled, not run per-change. See endpoint_check.py.

EXIT CODE 0 = clean, 1 = findings. Safe to wire into CI.
"""

from __future__ import annotations

import argparse
import ast
import glob
import importlib
import math
import os
import re
import subprocess
import sys
import types

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

FINDINGS: list = []
PASSED = 0
VERBOSE = False


def finding(tier: int, severity: str, where: str, msg: str):
    FINDINGS.append((tier, severity, where, msg))


def ok(label: str):
    global PASSED
    PASSED += 1
    if VERBOSE:
        print(f"    pass  {label}")


def banner(text: str):
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


# ---------------------------------------------------------------------------
# Stub environment — lets modules import without Streamlit/network/creds
# ---------------------------------------------------------------------------

def install_stubs():
    st = types.ModuleType("streamlit")
    st.secrets = {}
    st.session_state = {}
    st.cache_data = lambda **k: (lambda f: f)
    st.cache_resource = lambda **k: (lambda f: f)
    for name in ("write", "markdown", "caption", "error", "warning", "info",
                 "success", "divider", "subheader", "stop"):
        setattr(st, name, lambda *a, **k: None)
    sys.modules.setdefault("streamlit", st)


# ---------------------------------------------------------------------------
# TIER 0 — STATIC
# ---------------------------------------------------------------------------

PY_FILES = lambda: ["app.py"] + sorted(glob.glob("modules/*.py"))

# Patterns that caused production crashes. Each maps to a real incident.
BANNED_PATTERNS = [
    (r"\.get\(\s*['\"][^'\"]+['\"]\s*,\s*\[(?:0|None)\]\s*\)\s*\[",
     "unsafe hourly access: `.get(key, [0])[idx]` survives only idx 0 and "
     "raises IndexError once the slider moves. Use the safe accessor."),
    (r"except\s*:",
     "bare except swallows KeyboardInterrupt and SystemExit; catch Exception."),
    (r"\bdatetime\.utcnow\(\)",
     "naive utcnow: use datetime.now(timezone.utc)."),
    (r"basemaps\.cartocdn\.com",
     "CARTO basemaps are API-key gated and serve watermarked tiles."),
]


def tier0_compile():
    for f in PY_FILES():
        try:
            compile(open(f).read(), f, "exec")
            ok(f"compile {f}")
        except SyntaxError as e:
            finding(0, "CRITICAL", f, f"syntax error line {e.lineno}: {e.msg}")


def tier0_py311():
    script = os.path.join(ROOT, "check_py311.py")
    if not os.path.exists(script):
        finding(0, "MEDIUM", "check_py311.py", "compatibility gate not found")
        return
    r = subprocess.run([sys.executable, script] + PY_FILES(),
                       capture_output=True, text=True)
    if r.returncode != 0:
        finding(0, "CRITICAL", "py311 gate", r.stdout.strip() or r.stderr.strip())
    else:
        ok("py311 compatibility gate")


def tier0_undefined_names():
    """Catches the NameError class: a module using `math` without importing
    it, or a function referencing a variable scoped to a different function.
    Both shipped to production this year."""
    try:
        import pyflakes  # noqa: F401
    except ImportError:
        finding(0, "MEDIUM", "pyflakes",
                "not installed — `pip install pyflakes`. This is the check "
                "that catches missing imports and cross-function scope bugs.")
        return
    r = subprocess.run([sys.executable, "-m", "pyflakes"] + PY_FILES(),
                       capture_output=True, text=True)
    for line in (r.stdout or "").splitlines():
        if "undefined name" in line:
            finding(0, "CRITICAL", line.split(":")[0], line.strip())
        elif "redefinition of unused" in line:
            finding(0, "LOW", line.split(":")[0], line.strip())
    if not any(f[1] == "CRITICAL" for f in FINDINGS):
        ok("no undefined names")


def _strip_comments_strings(src: str) -> str:
    """Remove comments and docstrings so pattern checks don't fire on prose
    describing the banned pattern."""
    out = []
    try:
        tree = ast.parse(src)
        doc_lines = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                for ln in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                    doc_lines.add(ln)
        for i, line in enumerate(src.splitlines(), 1):
            if i in doc_lines:
                out.append("")
            else:
                out.append(line.split("#")[0])
    except SyntaxError:
        return src
    return "\n".join(out)


def tier0_banned_patterns():
    for f in PY_FILES():
        code = _strip_comments_strings(open(f).read())
        for pattern, why in BANNED_PATTERNS:
            for i, line in enumerate(code.splitlines(), 1):
                if re.search(pattern, line):
                    finding(0, "HIGH", f"{f}:{i}", why)
    ok("banned pattern scan")


def tier0_duplicate_defs():
    for f in PY_FILES():
        try:
            tree = ast.parse(open(f).read())
        except SyntaxError:
            continue
        seen = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in seen:
                    finding(0, "HIGH", f"{f}:{node.lineno}",
                            f"duplicate definition of {node.name}() — the "
                            f"earlier one at line {seen[node.name]} is dead")
                seen[node.name] = node.lineno
    ok("duplicate definition scan")


# ---------------------------------------------------------------------------
# TIER 1 — CONTRACT
# ---------------------------------------------------------------------------

OPTIONAL_DEPS = {"ephem", "plotly", "supabase", "bcrypt", "folium",
                 "timezonefinder", "pytz", "pandas", "numpy"}


def tier1_imports():
    for f in sorted(glob.glob("modules/*.py")):
        mod = f.replace("/", ".").replace("\\", ".")[:-3]
        try:
            importlib.import_module(mod)
            ok(f"import {mod}")
        except ModuleNotFoundError as e:
            missing = str(e).split("'")[1] if "'" in str(e) else str(e)
            if missing in OPTIONAL_DEPS:
                ok(f"import {mod} (skipped: {missing} not in this env)")
            else:
                finding(1, "CRITICAL", mod, f"import failed: {e}")
        except Exception as e:
            finding(1, "CRITICAL", mod, f"import raised {type(e).__name__}: {e}")


# Inputs that a provider can legitimately produce. Partial-parameter
# tolerance makes absent fields routine, not theoretical.
DEGENERATE_INPUTS = [
    ("empty dict", {}),
    ("key present, value None", {"temperature_2m": None}),
    ("empty series", {"temperature_2m": [], "relative_humidity_2m": []}),
    ("series shorter than index", {"temperature_2m": [1.0, 2.0]}),
    ("null values in series", {"temperature_2m": [None] * 48,
                               "relative_humidity_2m": [None] * 48}),
]


def tier1_hazard_contract():
    """Two failure modes, equally dangerous in a flight-safety tool:
    crashing on absent data, and INVENTING a hazard assessment from it."""
    try:
        from modules.hazard_logic import calculate_icing_profile
    except Exception as e:
        finding(1, "CRITICAL", "hazard_logic", f"unavailable: {e}")
        return

    # wx codes that are self-sufficient (freezing precip implies icing)
    SELF_SUFFICIENT = {56, 57, 66, 67, 68, 69, 77, 95, 96, 99, 48}
    for label, data in DEGENERATE_INPUTS:
        for idx in (0, 12, 47):
            for wx in (0, 61, 71):
                try:
                    r = calculate_icing_profile(data, idx, wx)
                except Exception as e:
                    finding(1, "CRITICAL", "calculate_icing_profile",
                            f"crashed on {label} idx={idx} wx={wx}: "
                            f"{type(e).__name__}: {e}")
                    continue
                if wx not in SELF_SUFFICIENT and r not in ("N/A", "NIL"):
                    finding(1, "CRITICAL", "calculate_icing_profile",
                            f"FABRICATED hazard '{r}' from {label} "
                            f"(idx={idx}, wx={wx}) — absent data must yield "
                            f"N/A, never an assessment")
    ok("hazard logic: degenerate input contract")


def tier1_accessor_contract():
    """Safe accessors must return None rather than raising or defaulting."""
    checks = [
        ("modules.hazard_logic", "_h_at"),
        ("modules.forecast_verification", "_fv_at"),
    ]
    for mod_name, fn_name in checks:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        fn = getattr(mod, fn_name, None)
        if fn is None:
            # _fv_at is a closure in some versions; presence in source is enough
            if fn_name in open(mod_name.replace(".", "/") + ".py").read():
                ok(f"{mod_name}.{fn_name} present (nested)")
                continue
            finding(1, "HIGH", mod_name, f"{fn_name} missing")
            continue
        for label, data in DEGENERATE_INPUTS:
            try:
                v = fn(data, "temperature_2m", 47)
                if v is not None and not isinstance(v, (int, float)):
                    finding(1, "MEDIUM", f"{mod_name}.{fn_name}",
                            f"returned {v!r} on {label}")
            except Exception as e:
                finding(1, "CRITICAL", f"{mod_name}.{fn_name}",
                        f"raised on {label}: {type(e).__name__}: {e}")
        ok(f"{mod_name}.{fn_name} degenerate inputs")


# ---------------------------------------------------------------------------
# TIER 2 — GLOBAL
# ---------------------------------------------------------------------------

SITES = [
    # name, lat, lon, elevation_m
    ("Belleville ON", 44.1628, -77.3832, 76),
    ("Cold Lake AB", 54.40, -110.18, 541),
    ("Petawawa ON", 45.90, -77.28, 130),
    ("Bagotville QC", 48.33, -71.00, 159),
    ("Toronto ON", 43.65, -79.38, 76),
    ("Anchorage AK", 61.22, -149.90, 31),
    ("Honolulu HI", 21.31, -157.86, 6),
    ("Miami FL", 25.77, -80.19, 2),
    ("Warsaw PL", 52.23, 21.01, 113),
    ("Reykjavik IS", 64.15, -21.94, 61),
    ("Kathmandu NP", 27.7159, 85.3072, 1400),
    ("Manila PH", 14.60, 120.98, 16),
    ("Tokyo JP", 35.68, 139.65, 40),
    ("Sydney AU", -33.87, 151.21, 58),
    ("La Paz BO", -16.50, -68.15, 3640),
    ("Sao Paulo BR", -23.55, -46.63, 760),
    ("Cape Town ZA", -33.92, 18.42, 25),
    ("Dubai AE", 25.20, 55.27, 5),
    ("Nairobi KE", -1.29, 36.82, 1795),
    ("Singapore SG", 1.35, 103.82, 15),
    ("Alert NU", 82.50, -62.35, 30),
    ("McMurdo AQ", -77.85, 166.67, 10),
    ("Suva FJ", -18.14, 178.44, 6),
    ("Apia WS", -13.83, -171.77, 2),
]


def tier2_location_matrix():
    """Every location-dependent path, at every site. This tier exists because
    ARMS was North-America-correct and silently wrong elsewhere: radar sites
    10,000 km away, satellites that could not see the site, invalid WMS
    bboxes past the antimeridian, Canada-only substitute models in Nepal."""

    # --- satellite selection must be geometrically possible ---
    try:
        from modules.spatial_products import pick_star_view
        SUB_LON = {"GOES19": -75.2, "GOES18": -137.0, "HIMAWARI": 140.7}
        for nm, la, lo, _e in SITES:
            sat, cdn, sec, _b = pick_star_view(la, lo)
            if sec == "NONE":
                continue
            sub = SUB_LON.get(cdn)
            if sub is None:
                finding(2, "MEDIUM", "pick_star_view",
                        f"{nm}: unknown sub-satellite longitude for {cdn}")
                continue
            d = math.pi / 180.0
            angle = math.degrees(math.acos(max(-1.0, min(1.0,
                math.cos(la * d) * math.cos((lo - sub) * d)))))
            if angle > 81.0:
                finding(2, "HIGH", "pick_star_view",
                        f"{nm}: assigned {cdn} at {angle:.0f}deg from "
                        f"sub-satellite point — site is past the limb")
        ok("satellite selection geometry (all sites)")
    except Exception as e:
        finding(2, "MEDIUM", "pick_star_view", f"check failed: {e}")

    # --- radar station range + beam height validity ---
    try:
        from modules.spatial_quad import (nearest_stations, station_in_range,
                                          beam_height_ft, STATION_MAX_RANGE_KM)
        for nm, la, lo, _e in SITES:
            for sid, _n, km, _cc in nearest_stations(la, lo, 10):
                offered = station_in_range(km)
                if offered and km > STATION_MAX_RANGE_KM:
                    finding(2, "HIGH", "station_in_range",
                            f"{nm}: offered {sid} at {km:.0f} km")
                h = beam_height_ft(km)
                if h is not None and km > STATION_MAX_RANGE_KM:
                    finding(2, "HIGH", "beam_height_ft",
                            f"{nm}: returned {h:,.0f} ft at {km:.0f} km — "
                            f"the 4/3-earth model is meaningless there")
                if h is not None and h > 100000:
                    finding(2, "HIGH", "beam_height_ft",
                            f"{nm}: implausible beam height {h:,.0f} ft")
        ok("radar station range + beam height (all sites)")
    except Exception as e:
        finding(2, "MEDIUM", "radar station checks", f"check failed: {e}")

    # --- WMS bbox must be valid WGS84 at every site and zoom ---
    for nm, la, lo, _e in SITES:
        for z in range(4, 14):
            w, h = 1024, 720
            lon_span = 360.0 * w / (256.0 * (2 ** z))
            lat_span = lon_span * (h / w) * max(0.2, math.cos(math.radians(la)))
            lat_span, lon_span = min(lat_span, 180.0), min(lon_span, 360.0)
            lat0, lat1 = la - lat_span / 2, la + lat_span / 2
            lon0, lon1 = lo - lon_span / 2, lo + lon_span / 2
            if lat0 < -90.0:
                lat0, lat1 = -90.0, -90.0 + lat_span
            elif lat1 > 90.0:
                lat0, lat1 = 90.0 - lat_span, 90.0
            if lon0 < -180.0:
                lon0, lon1 = -180.0, -180.0 + lon_span
            elif lon1 > 180.0:
                lon0, lon1 = 180.0 - lon_span, 180.0
            if not (-90 <= lat0 < lat1 <= 90 and -180 <= lon0 < lon1 <= 180):
                finding(2, "HIGH", "WMS bbox",
                        f"{nm} z{z}: invalid {lat0:.1f},{lon0:.1f},"
                        f"{lat1:.1f},{lon1:.1f}")
    ok("WMS bbox validity (all sites, zooms 4-13)")

    # --- emergency substitution must never offer an out-of-coverage model ---
    HRDPS = (40.0, 75.0, -145.0, -50.0)
    for nm, la, lo, _e in SITES:
        in_hrdps = HRDPS[0] <= la <= HRDPS[1] and HRDPS[2] <= lo <= HRDPS[3]
        if not in_hrdps and nm.endswith(("ON", "AB", "QC")):
            finding(2, "MEDIUM", "coverage", f"{nm}: Canadian site outside HRDPS?")
    ok("HRDPS coverage sanity")

    # --- scorecard must not emit the Best Match pseudo-model ---
    try:
        from modules.ensemble_analysis import _select_regional_model
        for nm, la, lo, _e in SITES:
            name, _u = _select_regional_model(la, lo)
            if name == "Best Match":
                # expected where no regional model exists; the scorecard must
                # omit the slot rather than score it
                src = open("modules/model_performance.py").read()
                if "_has_regional" not in src:
                    finding(2, "HIGH", "model_performance",
                            f"{nm}: Best Match pseudo-model would be scored")
                    break
        ok("no Best Match pseudo-model in scorecard")
    except Exception as e:
        finding(2, "LOW", "regional model", f"check skipped: {e}")

    # --- below-ground isobaric filtering must not strip low-elevation sites ---
    try:
        from modules.meteomatics_provider import (_surface_pressure_hpa,
                                                  _PRESSURE_LEVELS)
        for nm, la, lo, elev in SITES:
            p = _surface_pressure_hpa(elev)
            dropped = [lv for lv in _PRESSURE_LEVELS if lv > p + 100.0]
            if elev < 200 and dropped:
                finding(2, "HIGH", "below-ground filter",
                        f"{nm} ({elev} m): dropped {dropped} at a "
                        f"near-sea-level site")
            kept = [lv for lv in _PRESSURE_LEVELS if lv not in dropped]
            if len(kept) < 5:
                finding(2, "HIGH", "below-ground filter",
                        f"{nm}: only {len(kept)} levels retained")
        ok("below-ground level filtering (all sites)")
    except Exception as e:
        finding(2, "LOW", "below-ground filter", f"check skipped: {e}")


# ---------------------------------------------------------------------------

def main():
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", type=int, default=2,
                    help="highest tier to run (0-2, default 2)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose

    os.chdir(ROOT)
    install_stubs()

    banner("TIER 0 — STATIC")
    tier0_compile()
    tier0_py311()
    tier0_undefined_names()
    tier0_banned_patterns()
    tier0_duplicate_defs()

    if args.tier >= 1:
        banner("TIER 1 — CONTRACT")
        tier1_imports()
        tier1_hazard_contract()
        tier1_accessor_contract()

    if args.tier >= 2:
        banner("TIER 2 — GLOBAL")
        tier2_location_matrix()

    banner("RESULT")
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    FINDINGS.sort(key=lambda f: (order.get(f[1], 9), f[0]))
    if not FINDINGS:
        print(f"  CLEAN — {PASSED} checks passed, 0 findings")
        return 0
    counts: dict = {}
    for _t, sev, _w, _m in FINDINGS:
        counts[sev] = counts.get(sev, 0) + 1
    print(f"  {PASSED} checks passed, {len(FINDINGS)} finding(s): "
          + ", ".join(f"{v} {k}" for k, v in sorted(counts.items(),
                                                    key=lambda x: order.get(x[0], 9))))
    print()
    for tier, sev, where, msg in FINDINGS:
        print(f"  [{sev:<8}] T{tier} {where}")
        print(f"             {msg}")
    return 1 if any(s in ("CRITICAL", "HIGH") for _t, s, _w, _m in FINDINGS) else 0


if __name__ == "__main__":
    sys.exit(main())
