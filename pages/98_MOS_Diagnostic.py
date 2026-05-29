"""
MOS + station-obs endpoint diagnostic.

Drop this in pages/ to identify which Meteomatics endpoint is returning 404.
Tests each new endpoint individually with detailed error reporting.
"""

import base64
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import streamlit as st

st.set_page_config(page_title="MOS Diagnostic", layout="wide")
st.title("MOS + Station-Obs Diagnostic")
st.caption("Pinpoints which Meteomatics endpoint is returning 404.")

if not st.session_state.get("password_correct"):
    st.error("Please log in to ARMS first.")
    st.stop()

try:
    MM_USER = st.secrets["meteomatics"]["user"]
    MM_PASS = st.secrets["meteomatics"]["password"]
except (KeyError, FileNotFoundError):
    st.error("No [meteomatics] section in secrets.toml")
    st.stop()


def fetch_mm(url: str, timeout: int = 15) -> tuple:
    """Returns (status, body, elapsed_ms, response_headers)."""
    auth = base64.b64encode(f"{MM_USER}:{MM_PASS}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {auth}",
        "User-Agent": "VCAG-ARMS-MOS-Diag/1.0",
        "Accept": "application/json",
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), int((time.time() - t0) * 1000), dict(resp.headers)
    except urllib.error.HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        return e.code, body, int((time.time() - t0) * 1000), dict(e.headers or {})
    except Exception as e:
        return None, str(e).encode(), int((time.time() - t0) * 1000), {}


PRIMARY_LAT, PRIMARY_LON = 44.16, -77.38
PRIMARY_ICAO = "CYBN"  # Belleville (Trenton actually — let's also try CYTR)

st.divider()
run = st.button("Run MOS diagnostic", type="primary")
if not run:
    st.info("Click to run. Tests each MOS/station-obs endpoint variant.")
    st.stop()

out_lines: list[str] = []
def emit(line: str = "") -> None:
    out_lines.append(line)
def banner(text: str) -> None:
    emit("")
    emit("=" * 72)
    emit(f"  {text}")
    emit("=" * 72)


emit(f"Time: {datetime.now(timezone.utc).isoformat()}")
emit(f"User: {MM_USER[:3]}***{MM_USER[-2:]}")

now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
end = now + timedelta(hours=24)
validdate_fwd = f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')}--{end.strftime('%Y-%m-%dT%H:%M:%SZ')}:PT1H"

start_past = now - timedelta(hours=24)
validdate_back = f"{start_past.strftime('%Y-%m-%dT%H:%M:%SZ')}--{now.strftime('%Y-%m-%dT%H:%M:%SZ')}:PT1H"


# -----------------------------------------------------------------------------
# TEST 1 — find_station (does our account have access to any MOS stations?)
# -----------------------------------------------------------------------------
banner("TEST 1 — find_station?source=mm-mos near Belleville")
url = f"https://api.meteomatics.com/find_station?location={PRIMARY_LAT},{PRIMARY_LON}&source=mm-mos"
emit(f"GET {url}")
status, body, elapsed, headers = fetch_mm(url)
emit(f"Status: {status}  Elapsed: {elapsed}ms")
if status == 200:
    # Response is CSV
    text = body.decode("utf-8", errors="replace")
    lines = text.split("\n")
    emit(f"Got {len(lines)} lines (header + stations):")
    for line in lines[:8]:
        emit(f"  {line}")
    if len(lines) > 8:
        emit(f"  ... ({len(lines)-8} more)")
elif status == 404:
    emit(f"404 — MOS not available on this subscription? Body:")
    emit(f"  {body[:300].decode(errors='replace')}")
elif status == 401:
    emit(f"401 — auth failed")
else:
    emit(f"Unexpected status. Body:")
    emit(f"  {body[:300].decode(errors='replace')}")


# -----------------------------------------------------------------------------
# TEST 2 — mm-mos with metar_CYBN
# -----------------------------------------------------------------------------
banner("TEST 2 — mm-mos for metar_CYBN (Belleville)")
url = (f"https://api.meteomatics.com/{validdate_fwd}/t_2m:C,wind_speed_10m:kn"
       f"/metar_CYBN/json?source=mm-mos")
emit(f"GET {url}")
status, body, elapsed, _ = fetch_mm(url)
emit(f"Status: {status}  Elapsed: {elapsed}ms")
if status == 200:
    p = json.loads(body)
    n = len(((p.get('data') or [{}])[0].get('coordinates') or [{}])[0].get('dates') or [])
    emit(f"PASS — got {n} hours of MOS data")
elif status == 404:
    emit(f"404 — metar_CYBN not in MOS station database, or MOS not licensed")
    emit(f"  Body: {body[:300].decode(errors='replace')}")
else:
    emit(f"Status {status}. Body: {body[:300].decode(errors='replace')}")


# -----------------------------------------------------------------------------
# TEST 3 — mm-mos by coordinates (let MM pick nearest station)
# -----------------------------------------------------------------------------
banner("TEST 3 — mm-mos by coordinates (MM picks nearest station)")
url = (f"https://api.meteomatics.com/{validdate_fwd}/t_2m:C,wind_speed_10m:kn"
       f"/{PRIMARY_LAT},{PRIMARY_LON}/json?source=mm-mos")
emit(f"GET {url}")
status, body, elapsed, _ = fetch_mm(url)
emit(f"Status: {status}  Elapsed: {elapsed}ms")
if status == 200:
    emit("PASS — coordinate-based MOS works")
elif status == 404:
    emit(f"404 by coords too — MOS likely not licensed on this account")
    emit(f"  Body: {body[:300].decode(errors='replace')}")
else:
    emit(f"Status {status}. Body: {body[:300].decode(errors='replace')}")


# -----------------------------------------------------------------------------
# TEST 4 — mix-obs for metar_CYBN
# -----------------------------------------------------------------------------
banner("TEST 4 — mix-obs (past 24h) for metar_CYBN")
url = (f"https://api.meteomatics.com/{validdate_back}/t_2m:C,wind_speed_10m:kn"
       f"/metar_CYBN/json?source=mix-obs&on_invalid=fill_with_invalid")
emit(f"GET {url}")
status, body, elapsed, _ = fetch_mm(url)
emit(f"Status: {status}  Elapsed: {elapsed}ms")
if status == 200:
    p = json.loads(body)
    n = len(((p.get('data') or [{}])[0].get('coordinates') or [{}])[0].get('dates') or [])
    emit(f"PASS — got {n} hours of station obs")
elif status == 404:
    emit(f"404 — station not found in mix-obs or feature not licensed")
else:
    emit(f"Status {status}. Body: {body[:300].decode(errors='replace')}")


# -----------------------------------------------------------------------------
# TEST 5 — re-test all Meteomatics models we use (proves checkpoint 6 still works)
# -----------------------------------------------------------------------------
banner("TEST 5 — Standard model fetches (regression check)")
models_to_test = [
    ("mix",         "Meteomatics MIX"),
    ("ecmwf-ifs",   "ECMWF IFS"),
    ("ecmwf-aifs",  "ECMWF AIFS"),
    ("ncep-gfs",    "NCEP GFS"),
    ("ncep-hrrr",   "NCEP HRRR"),
]
for model_id, label in models_to_test:
    url = (f"https://api.meteomatics.com/{validdate_fwd}/t_2m:C"
           f"/{PRIMARY_LAT},{PRIMARY_LON}/json?model={model_id}")
    status, body, elapsed, _ = fetch_mm(url)
    if status == 200:
        emit(f"  {label:18s} ({model_id:14s}) -> {status} OK ({elapsed}ms)")
    else:
        snippet = body[:120].decode(errors='replace').replace("\n", " ")
        emit(f"  {label:18s} ({model_id:14s}) -> {status} FAIL ({elapsed}ms): {snippet}")


# -----------------------------------------------------------------------------
# TEST 6 — user_stats (current quota usage)
# -----------------------------------------------------------------------------
banner("TEST 6 — Current quota usage")
url = "https://api.meteomatics.com/user_stats_json"
status, body, elapsed, _ = fetch_mm(url)
emit(f"Status: {status}  Elapsed: {elapsed}ms")
if status == 200:
    try:
        stats = json.loads(body)
        emit(f"Username: {stats.get('username')}")
        # Pretty-print the relevant subset
        for k, v in stats.items():
            if isinstance(v, (str, int, float, bool)):
                emit(f"  {k}: {v}")
        # Limits often nested
        limits = stats.get("limits") or stats.get("stats") or {}
        if isinstance(limits, dict):
            emit("  Limits/Stats subkeys:")
            for k, v in limits.items():
                emit(f"    {k}: {v}")
    except Exception as e:
        emit(f"Parse failed: {e}")
        emit(f"Raw body: {body[:500].decode(errors='replace')}")
else:
    emit(f"Body: {body[:500].decode(errors='replace')}")


# -----------------------------------------------------------------------------
# TEST 7 — Historical fetch (matches scorecard's exact query)
# -----------------------------------------------------------------------------
banner("TEST 7 — Historical scorecard fetch (the actual failing path)")
mm_params_8 = [
    "t_2m:C", "relative_humidity_2m:p",
    "wind_speed_10m:kn", "wind_dir_10m:d", "wind_gusts_10m_1h:kn",
    "sfc_pressure:hPa", "visibility:m", "weather_symbol_1h:idx",
]
param_str = ",".join(mm_params_8)
emit(f"8 params: {param_str}")
emit("")
for model_id, label in models_to_test:
    url = (f"https://api.meteomatics.com/{validdate_back}/{param_str}"
           f"/{PRIMARY_LAT},{PRIMARY_LON}/json?model={model_id}")
    status, body, elapsed, _ = fetch_mm(url)
    if status == 200:
        emit(f"  {label:18s} ({model_id:14s}) -> {status} OK ({elapsed}ms)")
    else:
        snippet = body[:200].decode(errors='replace').replace("\n", " ")
        emit(f"  {label:18s} ({model_id:14s}) -> {status} FAIL ({elapsed}ms)")
        emit(f"    {snippet}")


banner("DONE")
emit("")
emit("Diagnosis guide:")
emit("  - If TEST 1 returns 404 → MOS not licensed on this account → remove from routing")
emit("  - If TEST 2 returns 404 but TEST 1 OK → metar_CYBN not in MOS station db → use a closer ICAO or coords")
emit("  - If TEST 4 returns 404 → mix-obs not licensed → remove fallback path")
emit("  - If TEST 5 has any 404 → that specific model is not on this subscription")
emit("  - TEST 6 shows the actual quota numbers")
emit("  - TEST 7 = scorecard's exact historical params, per model")
emit("  - TEST 8 = full ARMS forecast params (95) against each model — finds blend-only params")


# -----------------------------------------------------------------------------
# TEST 8 — Full 95-param ARMS forecast request, per model. Identifies
# additional parameters not supported by each raw NWP model (e.g. HRRR may
# lack high-altitude pressure levels). Uses a bisection-style approach:
# request the full surface set, then add pressure levels one at a time.
# -----------------------------------------------------------------------------
banner("TEST 8 — Full ARMS param set against each raw model (find HRRR's gap)")

# Reuse the actual param list the provider builds. Easier to import than to
# duplicate the list here.
try:
    import sys
    sys.path.insert(0, "/app")
    from modules.meteomatics_provider import (
        _build_param_list, _filter_params_for_model, _MODEL_PARAM_BLOCKLIST,
    )
    pairs = _build_param_list()
    emit(f"Building full request: {len(pairs)} params (matches what fetch_meteomatics_forecast uses)")
except Exception as e:
    emit(f"Could not import provider module: {e}")
    pairs = []

if pairs:
    raw_models = [
        ("ecmwf-ifs",   "ECMWF IFS"),
        ("ecmwf-aifs",  "ECMWF AIFS"),
        ("ncep-gfs",    "NCEP GFS"),
        ("ncep-hrrr",   "NCEP HRRR"),
    ]
    for model_id, label in raw_models:
        # Apply the current blocklist (so we test exactly what the live code sends)
        filtered = _filter_params_for_model(pairs, model_id)
        mm_params = [p[1] for p in filtered]
        emit(f"\n{label} ({model_id}): testing with {len(mm_params)} params (after blocklist)")

        # Test in batches of 10 (matches METEOMATICS_BATCH_SIZE)
        batch_size = 10
        any_fail = False
        for i in range(0, len(mm_params), batch_size):
            batch = mm_params[i:i+batch_size]
            param_str = ",".join(batch)
            url = (f"https://api.meteomatics.com/{validdate_fwd}/{param_str}"
                   f"/{PRIMARY_LAT},{PRIMARY_LON}/json?model={model_id}")
            status, body, elapsed, _ = fetch_mm(url, timeout=10)
            if status != 200:
                snippet = body[:250].decode(errors="replace").replace("\n", " ")
                emit(f"  batch {i//batch_size + 1} ({len(batch)} params) -> {status} FAIL ({elapsed}ms)")
                emit(f"    Params: {batch}")
                emit(f"    Body: {snippet}")
                any_fail = True
            else:
                pass    # silent on success to keep output readable
        if not any_fail:
            emit(f"  All batches OK")


banner("END")

output_text = "\n".join(out_lines)
st.text_area("Output:", value=output_text, height=600)
st.download_button(
    "Download as .txt",
    data=output_text,
    file_name=f"mos_diagnostic_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.txt",
    mime="text/plain",
)
