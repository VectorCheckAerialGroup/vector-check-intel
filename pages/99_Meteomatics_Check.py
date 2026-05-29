"""
Meteomatics verification page — drop-in for ARMS.

USAGE:
  1. Save this file as `pages/99_Meteomatics_Check.py` in your ARMS repo
     (create the `pages/` folder next to app.py if it doesn't exist)
  2. Deploy. Streamlit will auto-discover it and add it to the sidebar nav
  3. Log in to ARMS normally, click "Meteomatics Check" in the sidebar
  4. Click the Run button
  5. Copy the output box and paste it back to me in chat

NO TERMINAL REQUIRED. Uses the credentials from your existing secrets.toml.
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
st.caption("One-click check that the Meteomatics credentials and API are reachable from ARMS.")

# Sanity gate — make sure they're logged in (matches existing app.py auth pattern)
if not st.session_state.get("password_correct"):
    st.error("Please log in to ARMS first (open the main page), then return here.")
    st.stop()


# =============================================================================
# CREDENTIAL CHECK
# =============================================================================
try:
    MM_USER = st.secrets["meteomatics"]["user"]
    MM_PASS = st.secrets["meteomatics"]["password"]
except (KeyError, FileNotFoundError):
    st.error(
        "No `[meteomatics]` section found in secrets.toml — or the keys aren't "
        "`user` and `password`. Expected format:\n\n"
        "```toml\n[meteomatics]\nuser = \"your_username\"\npassword = \"your_password\"\n```"
    )
    st.stop()

if not MM_USER or not MM_PASS:
    st.error("Credentials are present but empty. Fill in real values in secrets.toml.")
    st.stop()

# Show that credentials are loaded, redacted
_redacted = f"{MM_USER[:3]}***{MM_USER[-2:]}" if len(MM_USER) > 5 else "***"
st.success(f"Credentials loaded for user `{_redacted}`")


# =============================================================================
# HTTP HELPER (stdlib only, no extra dependencies)
# =============================================================================
def fetch_meteomatics(url: str, timeout: int = 20) -> tuple:
    """Returns (status, body_bytes_or_str, elapsed_ms, headers_dict)."""
    auth = base64.b64encode(f"{MM_USER}:{MM_PASS}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {auth}",
        "User-Agent": "VCAG-ARMS-Verify/1.0",
        "Accept": "application/json",
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), int((time.time() - t0) * 1000), dict(resp.headers)
    except urllib.error.HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        return e.code, body, int((time.time() - t0) * 1000), dict(getattr(e, "headers", {}) or {})
    except Exception as e:
        return None, str(e).encode(), int((time.time() - t0) * 1000), {}


def fetch_open_meteo(lat: float, lon: float) -> dict:
    """Fetches a small surface set from Open-Meteo ECMWF for cross-check."""
    url = (
        f"https://api.open-meteo.com/v1/ecmwf?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m,relative_humidity_2m,wind_speed_10m,"
        f"wind_direction_10m,surface_pressure"
        f"&forecast_days=1&timezone=UTC&wind_speed_unit=kn"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "VCAG-ARMS-Verify/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


# =============================================================================
# SITES (VCAG detachments)
# =============================================================================
SITES = [
    ("HQ Belleville ON",      44.16,  -77.38),
    ("Vector1 Cold Lake AB",  54.40, -110.28),
    ("Vector2 Petawawa ON",   45.95,  -77.32),
    ("Vector3 Bagotville QC", 48.33,  -70.99),
    ("Vector4 Toronto ON",    43.65,  -79.38),
]
PRIMARY_NAME, PRIMARY_LAT, PRIMARY_LON = SITES[0]

MODELS_TO_TEST = ["mix", "ecmwf-ifs", "ecmwf-aifs", "ncep-gfs", "dwd-icon", "ncep-hrrr"]

TEST_PARAMS = [
    ("temperature_2m",             "t_2m:C"),
    ("relative_humidity_2m",       "relative_humidity_2m:p"),
    ("wind_speed_10m",             "wind_speed_10m:kn"),
    ("wind_direction_10m",         "wind_dir_10m:d"),
    ("wind_gusts_10m",             "wind_gusts_10m_1h:kn"),
    ("surface_pressure",           "sfc_pressure:hPa"),
    ("precipitation",              "precip_1h:mm"),
    ("weather_code",               "weather_symbol_1h:idx"),
    ("temperature_500hPa",         "t_500hPa:C"),
    ("geopotential_height_500hPa", "gh_500hPa:m"),
]


# =============================================================================
# UI
# =============================================================================
st.divider()
st.write(
    "Running this performs roughly **85 parameter-location API calls** against "
    "Meteomatics. That's a tiny fraction of any reasonable daily quota."
)

run = st.button("Run verification", type="primary")

if not run:
    st.info("Click the button to begin. The whole check takes about 15 seconds.")
    st.stop()


# =============================================================================
# RUN TESTS — accumulate output into a single text block
# =============================================================================
out_lines: list[str] = []

def emit(line: str = "") -> None:
    out_lines.append(line)

def banner(text: str) -> None:
    emit("")
    emit("=" * 72)
    emit(f"  {text}")
    emit("=" * 72)

emit("=" * 72)
emit("  VCAG ARMS — Meteomatics Integration Verification")
emit("=" * 72)
emit(f"  Time:               {datetime.now(timezone.utc).isoformat()}")
emit(f"  Operator:           {st.session_state.get('active_operator', 'UNKNOWN')}")
emit(f"  User (redacted):    {_redacted}")

progress = st.progress(0.0, text="Starting…")


# ---------- TEST 1: AUTH ----------
banner("TEST 1 — Authentication & minimal call")
progress.progress(0.05, text="Test 1: authenticating…")
now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
url1 = f"https://api.meteomatics.com/{now.strftime('%Y-%m-%dT%H:%M:%SZ')}/t_2m:C/{PRIMARY_LAT},{PRIMARY_LON}/json"
emit(f"GET {url1}")
status, body, elapsed, headers = fetch_meteomatics(url1)
emit(f"Status: {status}  Elapsed: {elapsed} ms")

auth_ok = (status == 200)
last_payload = None

if status == 401:
    emit("FAIL: Authentication rejected. Credentials may be invalid or expired.")
elif status == 402:
    emit("FAIL: Quota exceeded.")
elif status != 200:
    emit(f"FAIL: HTTP {status}")
    emit(f"Body (truncated): {body[:500].decode(errors='replace')}")
else:
    try:
        last_payload = json.loads(body)
        emit(f"Response status: {last_payload.get('status')}")
        emit(f"User echoed:     {last_payload.get('user')}")
        emit(f"dateGenerated:   {last_payload.get('dateGenerated')}")
        data_blocks = last_payload.get("data") or []
        emit(f"Data blocks:     {len(data_blocks)}")
        if data_blocks:
            first = data_blocks[0]
            emit(f"First block param: {first.get('parameter')}")
            coords = first.get("coordinates") or []
            if coords and coords[0].get("dates"):
                emit(f"Sample value:   {coords[0]['dates'][0]}")
        emit("PASS")
    except json.JSONDecodeError as e:
        emit(f"FAIL: response was not JSON: {e}")
        emit(f"Body: {body[:300].decode(errors='replace')}")
        auth_ok = False


# ---------- TEST 2: MULTI-PARAM ----------
if auth_ok:
    banner("TEST 2 — Multi-parameter request (MIX model)")
    progress.progress(0.25, text="Test 2: multi-parameter request…")
    end_time = now + timedelta(hours=6)
    validdate = f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')}--{end_time.strftime('%Y-%m-%dT%H:%M:%SZ')}:PT1H"
    param_str = ",".join(p[1] for p in TEST_PARAMS)
    url2 = (
        f"https://api.meteomatics.com/{validdate}/{param_str}/"
        f"{PRIMARY_LAT:.4f},{PRIMARY_LON:.4f}/json?model=mix"
    )
    emit(f"GET {url2[:120]}...")
    emit(f"Parameters: {len(TEST_PARAMS)}, hours: 6 → expected quota: ~{len(TEST_PARAMS) * 7}")
    status, body, elapsed, _ = fetch_meteomatics(url2)
    emit(f"Status: {status}  Elapsed: {elapsed} ms")

    multi_payload = None
    if status == 200:
        try:
            multi_payload = json.loads(body)
            data = multi_payload.get("data") or []
            emit(f"\nReturned {len(data)} parameter blocks (requested {len(TEST_PARAMS)})")
            returned = {b.get("parameter") for b in data}
            missing = [p[1] for p in TEST_PARAMS if p[1] not in returned]
            if missing:
                emit(f"WARN: missing parameters: {missing}")
            else:
                emit("All requested parameters returned.")
            emit(f"\nSample values at {now.strftime('%Y-%m-%dT%H:%M:%SZ')}:")
            for block in data:
                param = block.get("parameter")
                coords = block.get("coordinates") or []
                if coords and coords[0].get("dates"):
                    val = coords[0]["dates"][0].get("value")
                    emit(f"  {param:30s}  {val}")
                else:
                    emit(f"  {param:30s}  (empty)")
            emit("PASS")
        except json.JSONDecodeError as e:
            emit(f"FAIL: JSON parse: {e}")
    else:
        emit(f"FAIL: HTTP {status}")
        emit(f"Body: {body[:500].decode(errors='replace')}")


# ---------- TEST 3: CROSS-CHECK ----------
if auth_ok and multi_payload:
    banner("TEST 3 — Cross-check vs Open-Meteo ECMWF")
    progress.progress(0.45, text="Test 3: cross-check vs Open-Meteo…")
    emit("Note: MIX is Meteomatics' downscaled blend, OM-ECMWF is raw ECMWF.")
    emit("      Different models → values WILL differ; checking SENSIBILITY.")
    om = fetch_open_meteo(PRIMARY_LAT, PRIMARY_LON)
    if om.get("error"):
        emit(f"SKIP: Open-Meteo unreachable ({om['error']})")
    else:
        mm_first = {}
        for block in multi_payload.get("data") or []:
            coords = block.get("coordinates") or []
            if coords and coords[0].get("dates"):
                mm_first[block["parameter"]] = coords[0]["dates"][0].get("value")
        comparisons = [
            ("temperature_2m",       "t_2m:C",                 "°C", 3.0),
            ("relative_humidity_2m", "relative_humidity_2m:p", "%",  20.0),
            ("wind_speed_10m",       "wind_speed_10m:kn",      "kt", 5.0),
            ("wind_direction_10m",   "wind_dir_10m:d",         "°",  45.0),
            ("surface_pressure",     "sfc_pressure:hPa",       "hPa", 3.0),
        ]
        om_h = om.get("hourly") or {}
        emit(f"\n{'Variable':<24} {'OM-ECMWF':>10} {'MM-MIX':>10} {'Diff':>8}  Status")
        emit("-" * 70)
        for om_name, mm_name, unit, tol in comparisons:
            om_val = (om_h.get(om_name) or [None])[0]
            mm_val = mm_first.get(mm_name)
            if om_val is None or mm_val is None:
                emit(f"{om_name:<24} {'(none)':>10} {'(none)':>10} {'-':>8}  SKIP")
                continue
            diff = abs(om_val - mm_val)
            if om_name == "wind_direction_10m":
                diff = min(diff, 360 - diff)
            status_str = "OK" if diff <= tol else f"DRIFT (>{tol}{unit})"
            emit(f"{om_name:<24} {om_val:>10.2f} {mm_val:>10.2f} {diff:>8.2f}  {status_str}")


# ---------- TEST 4: RUN INFO ----------
if auth_ok and last_payload:
    banner("TEST 4 — Run info / dateGenerated parsing")
    progress.progress(0.55, text="Test 4: run info…")
    ts = last_payload.get("dateGenerated", "")
    if not ts:
        emit("FAIL: no dateGenerated field")
    else:
        try:
            iso = ts[:-1] if ts.endswith("Z") else ts
            run_dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
            age_min = int((datetime.now(timezone.utc) - run_dt).total_seconds() / 60)
            emit(f"dateGenerated: {ts}")
            emit(f"Parsed UTC:    {run_dt}")
            emit(f"Age:           {age_min} minutes ago")
            emit(f"Cycle label:   {run_dt.hour:02d}Z")
            emit("PASS")
        except Exception as e:
            emit(f"FAIL: parse error: {e}")


# ---------- TEST 5: PER-MODEL REACHABILITY ----------
if auth_ok:
    banner("TEST 5 — Per-model reachability")
    progress.progress(0.65, text="Test 5: per-model reachability…")
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    emit(f"Site: {PRIMARY_LAT}, {PRIMARY_LON}")
    emit(f"\n{'Model':<14} {'Status':>8} {'Elapsed':>10}  Result")
    emit("-" * 60)
    reachable_models = []
    for i, model in enumerate(MODELS_TO_TEST):
        progress.progress(0.65 + 0.15 * (i + 1) / len(MODELS_TO_TEST),
                          text=f"Test 5: checking {model}…")
        url_m = f"https://api.meteomatics.com/{timestamp}/t_2m:C/{PRIMARY_LAT:.4f},{PRIMARY_LON:.4f}/json?model={model}"
        st_m, body_m, elapsed_m, _ = fetch_meteomatics(url_m, timeout=15)
        if st_m == 200:
            try:
                p = json.loads(body_m)
                d = p.get("data") or []
                if d and d[0].get("coordinates"):
                    val = d[0]["coordinates"][0]["dates"][0]["value"]
                    emit(f"{model:<14} {st_m:>8} {elapsed_m:>8}ms  t_2m={val:.1f}°C ✓")
                    reachable_models.append(model)
                else:
                    emit(f"{model:<14} {st_m:>8} {elapsed_m:>8}ms  (empty data)")
            except Exception:
                emit(f"{model:<14} {st_m:>8} {elapsed_m:>8}ms  (parse fail)")
        elif st_m == 400:
            emit(f"{model:<14} {st_m:>8} {elapsed_m:>8}ms  NOT AVAILABLE in account")
        else:
            emit(f"{model:<14} {st_m:>8} {elapsed_m:>8}ms  FAIL")
    emit(f"\n{len(reachable_models)} of {len(MODELS_TO_TEST)} models reachable: {reachable_models}")


# ---------- TEST 6: ALL SITES ----------
if auth_ok:
    banner("TEST 6 — Coverage for all VCAG sites (MIX)")
    progress.progress(0.85, text="Test 6: all sites…")
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    emit(f"Model: mix  Timestamp: {timestamp}")
    emit(f"\n{'Detachment':<26} {'Lat':>8} {'Lon':>9} {'Status':>8}  Result")
    emit("-" * 75)
    for name, lat, lon in SITES:
        url_s = f"https://api.meteomatics.com/{timestamp}/t_2m:C/{lat:.4f},{lon:.4f}/json?model=mix"
        st_s, body_s, _, _ = fetch_meteomatics(url_s, timeout=15)
        if st_s == 200:
            try:
                p = json.loads(body_s)
                d = p.get("data") or []
                if d and d[0].get("coordinates"):
                    val = d[0]["coordinates"][0]["dates"][0]["value"]
                    emit(f"{name:<26} {lat:>8.2f} {lon:>9.2f} {st_s:>8}  t_2m={val:.1f}°C ✓")
                else:
                    emit(f"{name:<26} {lat:>8.2f} {lon:>9.2f} {st_s:>8}  (empty)")
            except Exception:
                emit(f"{name:<26} {lat:>8.2f} {lon:>9.2f} {st_s:>8}  (parse fail)")
        else:
            emit(f"{name:<26} {lat:>8.2f} {lon:>9.2f} {st_s:>8}  FAIL")


# ---------- QUOTA HEADERS ----------
if auth_ok:
    banner("Quota / rate-limit headers (if exposed)")
    progress.progress(0.95, text="Final: checking quota headers…")
    quota = [k for k in headers if any(t in k.lower() for t in ("limit", "quota", "remaining"))]
    if quota:
        for k in quota:
            emit(f"  {k}: {headers[k]}")
    else:
        emit("  (no quota headers found in response — limits may only be visible in customer portal)")


banner("DONE")
emit("Copy the box below and paste it back in chat to share results.")
progress.progress(1.0, text="Complete.")


# =============================================================================
# DISPLAY OUTPUT
# =============================================================================
output_text = "\n".join(out_lines)
st.text_area(
    "Verification output — copy this and paste it back in chat:",
    value=output_text,
    height=600,
)
st.download_button(
    "Download as .txt",
    data=output_text,
    file_name=f"meteomatics_verify_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.txt",
    mime="text/plain",
)
