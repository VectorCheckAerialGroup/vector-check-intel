"""
Meteomatics verification page (post-batching).

Confirms credentials work AND that the production batched-fetch path now
succeeds. Replaces the earlier diagnostic.
"""

import base64
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import streamlit as st

st.set_page_config(page_title="Meteomatics Check", layout="wide")
st.title("Meteomatics Integration Verification")
st.caption("Confirms credentials, batched fetch, and production-equivalent request all work.")

if not st.session_state.get("password_correct"):
    st.error("Please log in to ARMS first, then return here.")
    st.stop()

try:
    MM_USER = st.secrets["meteomatics"]["user"]
    MM_PASS = st.secrets["meteomatics"]["password"]
except (KeyError, FileNotFoundError):
    st.error("No `[meteomatics]` section in secrets.toml")
    st.stop()

_redacted = f"{MM_USER[:3]}***{MM_USER[-2:]}" if len(MM_USER) > 5 else "***"
st.success(f"Credentials loaded for user `{_redacted}`")


def fetch_mm(url: str, timeout: int = 20) -> tuple:
    """Returns (status, body, elapsed_ms)."""
    auth = base64.b64encode(f"{MM_USER}:{MM_PASS}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {auth}",
        "User-Agent": "VCAG-ARMS-Verify/1.1",
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
st.markdown(
    "This page calls the ARMS provider module directly, exercising the "
    "production code path (batched 10-call fetch internally)."
)
run = st.button("Run verification", type="primary")
if not run:
    st.info("Click to run. Takes about 5 seconds.")
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
emit("  Meteomatics Integration Verification (post-batching)")
emit("=" * 72)
emit(f"  Time: {datetime.now(timezone.utc).isoformat()}")
emit(f"  User: {_redacted}")

progress = st.progress(0.0)


# TEST 1
banner("TEST 1 — Raw API authentication")
progress.progress(0.1, "Test 1")
now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
url = f"https://api.meteomatics.com/{now.strftime('%Y-%m-%dT%H:%M:%SZ')}/t_2m:C/{PRIMARY_LAT},{PRIMARY_LON}/json"
status, body, elapsed = fetch_mm(url)
emit(f"Status: {status}  Elapsed: {elapsed}ms")
if status == 200:
    p = json.loads(body)
    emit(f"User echoed: {p.get('user')}")
    emit(f"dateGenerated: {p.get('dateGenerated')}")
    emit("PASS")
else:
    emit(f"FAIL: {body[:200].decode(errors='replace')}")


# TEST 2 — the real test
banner("TEST 2 — ARMS provider batched fetch (95 params via 10 batches)")
progress.progress(0.3, "Test 2: batched fetch")
try:
    from modules.meteomatics_provider import fetch_meteomatics_forecast
    t0 = time.time()
    result = fetch_meteomatics_forecast(PRIMARY_LAT, PRIMARY_LON, model="mix", hours_ahead=72)
    elapsed = int((time.time() - t0) * 1000)
    emit(f"Elapsed: {elapsed}ms")
    if result.get("error"):
        emit(f"FAIL: {result.get('message')}")
    else:
        batches_info = result.get("_batches") or {}
        emit(f"Batches: {batches_info.get('count')}  Batch fetch elapsed: {batches_info.get('elapsed_ms')}ms")
        hourly = result.get("hourly") or {}
        times = hourly.get("time") or []
        emit(f"Time grid length: {len(times)} hours (expect ~73)")
        emit(f"Provider: {result.get('_provider')}  Model: {result.get('_model')}")
        emit(f"Run info: {result.get('_run_info')}")
        for var in ["temperature_2m", "wind_speed_10m", "surface_pressure",
                    "temperature_500hPa", "geopotential_height_500hPa",
                    "cape", "weather_code", "boundary_layer_height", "snow_depth"]:
            arr = hourly.get(var)
            if arr is None:
                emit(f"  {var:32s}  MISSING from response")
            else:
                n_non_null = sum(1 for v in arr if v is not None)
                first_val = next((v for v in arr if v is not None), None)
                emit(f"  {var:32s}  len={len(arr)}  non-null={n_non_null}  first={first_val}")
        critical = ["temperature_2m", "wind_speed_10m", "surface_pressure"]
        all_present = all(hourly.get(v) and any(x is not None for x in hourly[v]) for v in critical)
        emit("PASS" if all_present else "FAIL (critical surface vars missing)")
except Exception as e:
    import traceback
    emit(f"FAIL: {type(e).__name__}: {e}")
    emit(traceback.format_exc()[:500])


# TEST 3 — cross-check
banner("TEST 3 — Cross-check vs Open-Meteo ECMWF")
progress.progress(0.7, "Test 3: cross-check")
try:
    from modules.meteomatics_provider import fetch_meteomatics_forecast
    mm_data = fetch_meteomatics_forecast(PRIMARY_LAT, PRIMARY_LON, model="mix", hours_ahead=24)

    om_url = (
        f"https://api.open-meteo.com/v1/ecmwf?latitude={PRIMARY_LAT}&longitude={PRIMARY_LON}"
        f"&hourly=temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,surface_pressure"
        f"&forecast_days=1&timezone=UTC&wind_speed_unit=kn"
    )
    om_req = urllib.request.Request(om_url, headers={"User-Agent": "VCAG-ARMS/1.0"})
    with urllib.request.urlopen(om_req, timeout=15) as resp:
        om_data = json.loads(resp.read())
    om_h = om_data.get("hourly") or {}
    mm_h = mm_data.get("hourly") or {} if not mm_data.get("error") else {}

    if not mm_h:
        emit("SKIP: MM fetch failed (see Test 2)")
    else:
        emit(f"{'Variable':<24} {'OM-ECMWF':>10} {'MM-MIX':>10} {'Diff':>8}  Status")
        emit("-" * 70)
        for var, tol in [
            ("temperature_2m", 3.0),
            ("relative_humidity_2m", 20.0),
            ("wind_speed_10m", 5.0),
            ("wind_direction_10m", 45.0),
            ("surface_pressure", 3.0),
        ]:
            om_v = (om_h.get(var) or [None])[0]
            mm_v = (mm_h.get(var) or [None])[0]
            if om_v is None or mm_v is None:
                emit(f"{var:<24} {'(none)':>10} {'(none)':>10} {'-':>8}  SKIP")
                continue
            diff = abs(om_v - mm_v)
            if var == "wind_direction_10m":
                diff = min(diff, 360 - diff)
            status_str = "OK" if diff <= tol else "DRIFT"
            emit(f"{var:<24} {om_v:>10.2f} {mm_v:>10.2f} {diff:>8.2f}  {status_str}")
except Exception as e:
    emit(f"FAIL: {type(e).__name__}: {e}")


# TEST 4 — all sites
banner("TEST 4 — All 5 VCAG sites")
progress.progress(0.9, "Test 4: all sites")
SITES = [
    ("HQ Belleville ON",      44.16,  -77.38),
    ("Vector1 Cold Lake AB",  54.40, -110.28),
    ("Vector2 Petawawa ON",   45.95,  -77.32),
    ("Vector3 Bagotville QC", 48.33,  -70.99),
    ("Vector4 Toronto ON",    43.65,  -79.38),
]
from modules.meteomatics_provider import fetch_meteomatics_forecast
emit(f"{'Detachment':<26} {'Lat':>8} {'Lon':>9}  Result")
emit("-" * 75)
for name, lat, lon in SITES:
    try:
        r = fetch_meteomatics_forecast(lat, lon, model="mix", hours_ahead=6)
        if r.get("error"):
            emit(f"{name:<26} {lat:>8.2f} {lon:>9.2f}  FAIL: {r.get('message','')[:60]}")
        else:
            t2m = (r.get("hourly", {}).get("temperature_2m") or [None])[0]
            batches = r.get("_batches", {}).get("count", "?")
            emit(f"{name:<26} {lat:>8.2f} {lon:>9.2f}  t_2m={t2m}°C  batches={batches}")
    except Exception as e:
        emit(f"{name:<26} {lat:>8.2f} {lon:>9.2f}  EXCEPTION: {e}")


banner("DONE")
emit("If Test 2 PASSED, the production dashboard should now work with Meteomatics MIX.")
progress.progress(1.0, "Complete")

output_text = "\n".join(out_lines)
st.text_area("Output:", value=output_text, height=600)
st.download_button(
    "Download as .txt",
    data=output_text,
    file_name=f"meteomatics_verify_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.txt",
    mime="text/plain",
)
