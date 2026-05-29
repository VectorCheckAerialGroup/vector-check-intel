"""
Meteomatics 403 Diagnostic — finds the dimension causing the rejection.

USAGE:
  Replace your existing pages/99_Meteomatics_Check.py with this file,
  redeploy, log in, click "Meteomatics Check" in the sidebar, click Run.
  Paste the output back.
"""

import base64
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import streamlit as st

st.set_page_config(page_title="Meteomatics Diagnostic", layout="wide")
st.title("Meteomatics 403 Diagnostic")
st.caption("Progressively shrinks the request until we find what your subscription allows.")

if not st.session_state.get("password_correct"):
    st.error("Please log in to ARMS first, then return here.")
    st.stop()

try:
    MM_USER = st.secrets["meteomatics"]["user"]
    MM_PASS = st.secrets["meteomatics"]["password"]
except (KeyError, FileNotFoundError):
    st.error("No `[meteomatics]` section in secrets.toml")
    st.stop()

_redacted = f"{MM_USER[:3]}***{MM_USER[-2:]}"
st.success(f"Credentials loaded for user `{_redacted}`")


def fetch_mm(url: str, timeout: int = 20) -> tuple:
    """Returns (status, body, elapsed_ms)."""
    auth = base64.b64encode(f"{MM_USER}:{MM_PASS}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {auth}",
        "User-Agent": "VCAG-ARMS-Diag/1.0",
        "Accept": "application/json",
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        return e.code, body, int((time.time() - t0) * 1000)
    except Exception as e:
        return None, str(e).encode(), int((time.time() - t0) * 1000)


PRIMARY_LAT, PRIMARY_LON = 44.16, -77.38

st.divider()
run = st.button("Run diagnostic", type="primary")
if not run:
    st.info("Click to run. Takes about 30 seconds.")
    st.stop()


out_lines: list[str] = []

def emit(line: str = "") -> None:
    out_lines.append(line)

def banner(text: str) -> None:
    emit("")
    emit("=" * 72)
    emit(f"  {text}")
    emit("=" * 72)


emit("=" * 72)
emit("  Meteomatics 403 Diagnostic")
emit("=" * 72)
emit(f"  Time: {datetime.now(timezone.utc).isoformat()}")
emit(f"  User: {_redacted}")

now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
progress = st.progress(0.0)


# TEST A
banner("A — Surface-only, 1 param, 1 time, MIX")
progress.progress(0.05, "A: bare minimum")
url = f"https://api.meteomatics.com/{now.strftime('%Y-%m-%dT%H:%M:%SZ')}/t_2m:C/{PRIMARY_LAT},{PRIMARY_LON}/json?model=mix"
status, body, elapsed = fetch_mm(url)
emit(f"GET {url}")
emit(f"Status: {status}  Elapsed: {elapsed}ms")
if status != 200:
    emit(f"Body: {body[:400].decode(errors='replace')}")


# TEST B
banner("B — 10 surface params, 6h, MIX (this passed in verification)")
progress.progress(0.15, "B: original verification set")
end = now + timedelta(hours=6)
validdate = f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')}--{end.strftime('%Y-%m-%dT%H:%M:%SZ')}:PT1H"
SURFACE_10 = "t_2m:C,relative_humidity_2m:p,wind_speed_10m:kn,wind_dir_10m:d,wind_gusts_10m_1h:kn,sfc_pressure:hPa,precip_1h:mm,weather_symbol_1h:idx,t_500hPa:C,gh_500hPa:m"
url = f"https://api.meteomatics.com/{validdate}/{SURFACE_10}/{PRIMARY_LAT},{PRIMARY_LON}/json?model=mix"
status, body, elapsed = fetch_mm(url)
emit(f"Params: 10  Hours: 6  Model: mix")
emit(f"Status: {status}  Elapsed: {elapsed}ms")
if status != 200:
    emit(f"Body: {body[:400].decode(errors='replace')}")


# TEST C
banner("C — Same 10 params, 72h horizon (production horizon)")
progress.progress(0.25, "C: 72h horizon")
end72 = now + timedelta(hours=72)
validdate72 = f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')}--{end72.strftime('%Y-%m-%dT%H:%M:%SZ')}:PT1H"
url = f"https://api.meteomatics.com/{validdate72}/{SURFACE_10}/{PRIMARY_LAT},{PRIMARY_LON}/json?model=mix"
status, body, elapsed = fetch_mm(url)
emit(f"Params: 10  Hours: 72  Model: mix")
emit(f"Status: {status}  Elapsed: {elapsed}ms")
if status != 200:
    emit(f"Body: {body[:400].decode(errors='replace')}")
    emit("--> If this fails but B passed: HORIZON limit at ~6h or somewhere between")


# TEST D
banner("D — All 20 ARMS surface params, 24h, MIX")
progress.progress(0.35, "D: full surface set")
end24 = now + timedelta(hours=24)
vd24 = f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')}--{end24.strftime('%Y-%m-%dT%H:%M:%SZ')}:PT1H"
SURFACE_FULL = ",".join([
    "t_2m:C", "relative_humidity_2m:p",
    "wind_speed_10m:kn", "wind_dir_10m:d", "wind_gusts_10m_1h:kn",
    "wind_speed_80m:kn", "wind_speed_120m:kn", "wind_speed_180m:kn",
    "wind_dir_80m:d", "wind_dir_120m:d", "wind_dir_180m:d",
    "weather_symbol_1h:idx", "visibility:m",
    "freezing_level:m", "prob_precip_1h:p", "precip_1h:mm",
    "cape:Jkg", "boundary_layer_height:m", "sfc_pressure:hPa",
    "snow_depth:m",
])
url = f"https://api.meteomatics.com/{vd24}/{SURFACE_FULL}/{PRIMARY_LAT},{PRIMARY_LON}/json?model=mix"
status, body, elapsed = fetch_mm(url)
emit(f"Params: 20  Hours: 24  Model: mix")
emit(f"Status: {status}  Elapsed: {elapsed}ms")
if status != 200:
    emit(f"Body: {body[:400].decode(errors='replace')}")
    emit("--> If this fails but C passed: one of the new surface params is restricted")


# TEST E
banner("E — Surface + 1 pressure level (500hPa, 5 vars), 6h, MIX")
progress.progress(0.45, "E: 1 pressure level")
PL_500 = "t_500hPa:C,relative_humidity_500hPa:p,gh_500hPa:m,wind_speed_500hPa:kn,wind_dir_500hPa:d"
url = f"https://api.meteomatics.com/{validdate}/{PL_500}/{PRIMARY_LAT},{PRIMARY_LON}/json?model=mix"
status, body, elapsed = fetch_mm(url)
emit(f"Params: 5 (one pressure level)  Hours: 6  Model: mix")
emit(f"Status: {status}  Elapsed: {elapsed}ms")
if status != 200:
    emit(f"Body: {body[:400].decode(errors='replace')}")


# TEST F
banner("F — Surface + 5 common pressure levels (25 PL params), 6h, MIX")
progress.progress(0.55, "F: 5 pressure levels")
PL_COMMON = ",".join([
    f"{v}_{p}hPa:{u}" for p in [1000, 850, 700, 500, 250]
    for v, u in [("t", "C"), ("relative_humidity", "p"), ("gh", "m"),
                  ("wind_speed", "kn"), ("wind_dir", "d")]
])
url = f"https://api.meteomatics.com/{validdate}/{PL_COMMON}/{PRIMARY_LAT},{PRIMARY_LON}/json?model=mix"
status, body, elapsed = fetch_mm(url)
emit(f"Params: 25 (5 pressure levels x 5 vars)  Hours: 6  Model: mix")
emit(f"Status: {status}  Elapsed: {elapsed}ms")
if status != 200:
    emit(f"Body: {body[:400].decode(errors='replace')}")


# TEST G
banner("G — Surface + 15 ARMS pressure levels (75 PL params), 6h, MIX")
progress.progress(0.65, "G: full pressure column")
ARMS_LEVELS = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300, 250, 200, 150]
PL_FULL = ",".join([
    f"{v}_{p}hPa:{u}" for p in ARMS_LEVELS
    for v, u in [("t", "C"), ("relative_humidity", "p"), ("gh", "m"),
                  ("wind_speed", "kn"), ("wind_dir", "d")]
])
url = f"https://api.meteomatics.com/{validdate}/{PL_FULL}/{PRIMARY_LAT},{PRIMARY_LON}/json?model=mix"
status, body, elapsed = fetch_mm(url)
emit(f"Params: 75 (15 pressure levels x 5 vars)  Hours: 6  Model: mix")
emit(f"Status: {status}  Elapsed: {elapsed}ms")
if status != 200:
    emit(f"Body: {body[:400].decode(errors='replace')}")
    emit("--> If this fails but F passed: one of the less-common pressure levels is restricted")


# TEST H
banner("H — Full ARMS production request (95 params, 72h, MIX)")
progress.progress(0.80, "H: full production")
FULL_PRODUCTION = SURFACE_FULL + "," + PL_FULL
url = f"https://api.meteomatics.com/{validdate72}/{FULL_PRODUCTION}/{PRIMARY_LAT},{PRIMARY_LON}/json?model=mix"
status, body, elapsed = fetch_mm(url)
emit(f"Params: 95  Hours: 72  Model: mix")
emit(f"Status: {status}  Elapsed: {elapsed}ms")
if status != 200:
    emit(f"Body: {body[:500].decode(errors='replace')}")
    emit("--> THIS is the production query that's hitting 403 in the dashboard")


# TEST I
banner("I — Repeat full production with different models")
progress.progress(0.90, "I: per-model production")
for model in ["mix", "ecmwf-ifs", "ncep-gfs"]:
    url = f"https://api.meteomatics.com/{validdate72}/{FULL_PRODUCTION}/{PRIMARY_LAT},{PRIMARY_LON}/json?model={model}"
    status, body, elapsed = fetch_mm(url, timeout=25)
    emit(f"  {model:14s} -> {status}  {elapsed}ms" + (
        f"  {body[:160].decode(errors='replace')}" if status != 200 else "  OK"
    ))


# TEST J
banner("J — Individual parameter test (find restricted ones)")
progress.progress(0.95, "J: per-parameter")
SUSPECT_PARAMS = [
    "boundary_layer_height:m",
    "cape:Jkg",
    "freezing_level:m",
    "snow_depth:m",
    "visibility:m",
    "wind_speed_180m:kn",
    "wind_speed_120m:kn",
    "weather_symbol_1h:idx",
    "t_975hPa:C",
    "t_925hPa:C",
    "t_150hPa:C",
    "t_200hPa:C",
]
single_time = now.strftime('%Y-%m-%dT%H:%M:%SZ')
for p in SUSPECT_PARAMS:
    url = f"https://api.meteomatics.com/{single_time}/{p}/{PRIMARY_LAT},{PRIMARY_LON}/json?model=mix"
    status, body, elapsed = fetch_mm(url, timeout=10)
    marker = "OK" if status == 200 else "NO"
    short_err = body[:80].decode(errors='replace') if status != 200 else ""
    emit(f"  [{marker}] {p:32s} -> {status}  {short_err}")


banner("DONE")
emit("Paste this entire output back. We'll identify which dimension causes the 403.")
progress.progress(1.0, "Complete")

output_text = "\n".join(out_lines)
st.text_area("Output — copy and paste back:", value=output_text, height=600)
st.download_button(
    "Download as .txt",
    data=output_text,
    file_name=f"meteomatics_diag_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.txt",
    mime="text/plain",
)
