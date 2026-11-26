"""
VECTOR CHECK AERIAL GROUP INC. — Shared Atmospheric Computation Engine

This module extracts the atmospheric physics pipeline that was previously
duplicated between compute_impact_matrix() and the main dashboard block in
app.py. Every function here is the SINGLE AUTHORITATIVE implementation.

Functions:
    build_thermal_profile()   — Constructs the 15-layer thermodynamic column
    evaluate_thermal_phase()  — Thermal Phase Gate (drizzle→precip type veto)
    evaluate_cloud_base()     — Cloud base cascade (OVC→BKN→SCT→FEW→CLR)
    evaluate_blsn()           — Blowing/Drifting snow kinetic gate
    compute_gusts()           — Surface gust model with floor enforcement
    compute_hour_state()      — Full single-hour atmospheric state (master)
"""

import math
from modules.physics import (
    calc_td,
    SNOWPACK_BLSN_THRESHOLD_M,
    CONVECTIVE_CCL_MULTIPLIER,
    ALL_P_LEVELS,
    METERS_TO_FEET,
    attenuate_gust_delta,
)
from modules.hazard_logic import (
    calculate_icing_profile,
    get_turb_ice,
    get_weather_element,
)


def build_thermal_profile(
    h_data: dict,
    idx: int,
    sfc_elevation: float,
    t_temp: float,
    td: float,
    sfc_spread: float,
    rh: int,
) -> list[dict]:
    """Constructs the 15-layer thermodynamic column from NWP pressure-level data.

    Returns a list of dicts sorted ascending by geopotential height:
        [{'h': ft_msl, 't': °C, 'td': °C, 'spread': °C, 'rh': %}]
    """
    profile = [{'h': sfc_elevation, 't': t_temp, 'td': td, 'spread': sfc_spread, 'rh': rh}]

    for p in ALL_P_LEVELS:
        gh_list = h_data.get(f'geopotential_height_{p}hPa')
        t_list = h_data.get(f'temperature_{p}hPa')
        rh_list = h_data.get(f'relative_humidity_{p}hPa')

        if gh_list and t_list and rh_list and len(gh_list) > idx:
            if gh_list[idx] is not None and t_list[idx] is not None and rh_list[idx] is not None:
                p_gh = float(gh_list[idx]) * METERS_TO_FEET
                p_t = float(t_list[idx])
                p_rh = int(rh_list[idx])
                p_td = calc_td(p_t, p_rh)

                if p_gh > profile[-1]['h']:
                    profile.append({
                        'h': p_gh,
                        't': p_t,
                        'td': p_td,
                        'spread': p_t - p_td,
                        'rh': p_rh,
                    })

    return profile


def evaluate_thermal_phase(
    wx: int,
    t_temp: float,
    c_depth: float,
    precip: float,
    is_convective: bool,
    thermal_profile: list[dict],
    frz_agl: float,
) -> int:
    """Thermal Phase Gate — reclassifies ambiguous WMO drizzle codes (50-59)
    into operationally correct precipitation types based on the thermal column.

    Returns the corrected weather code.
    """
    if not (50 <= wx <= 59):
        return wx
    if not (c_depth >= 2500 or precip >= 0.5 or is_convective):
        return wx

    warm_nose = any(layer['t'] > 0 for layer in thermal_profile[1:])
    is_heavy = wx in [54, 55, 57]

    if t_temp <= 0:
        return (67 if is_heavy else 66) if warm_nose else (73 if is_heavy else 71)
    elif 0 < t_temp <= 2.5 and frz_agl < 1500:
        return 69 if is_heavy else 68
    else:
        return 63 if is_heavy else 61


def evaluate_cloud_base(
    thermal_profile: list[dict],
    sfc_elevation: float,
    sfc_spread: float,
    vis_sm: float,
    wx: int,
    is_convective: bool,
) -> tuple[int, str]:
    """Cloud base cascade — determines cloud base AGL and coverage category.

    Scans the thermal profile for dew-point spread thresholds:
        ≤3.0°C → OVC (≤1.0) / BKN
        ≤5.0°C → SCT
        ≤7.0°C → FEW

    Returns (cloud_base_agl_ft, cloud_amount_str).
    """
    c_base_agl = 99999
    c_amt = "CLR"

    search_profile = thermal_profile[1:] if len(thermal_profile) > 1 else thermal_profile

    # Pass 1: BKN/OVC (spread ≤ 3.0)
    for layer in search_profile:
        h_agl = max(0, layer['h'] - sfc_elevation)
        if layer['spread'] <= 3.0:
            if h_agl < 1000 and sfc_spread <= 3.0 and vis_sm >= 1.5 and wx < 50:
                continue
            c_base_agl = int(round(h_agl, -2))
            c_amt = "OVC" if layer['spread'] <= 1.0 else "BKN"
            if c_base_agl == 0:
                if vis_sm > 0.62 and wx not in [45, 48]:
                    c_base_agl = 100
                else:
                    c_amt = "VV"
            break

    # Pass 2: SCT (spread ≤ 5.0)
    if c_amt == "CLR":
        for layer in search_profile:
            h_agl = max(0, layer['h'] - sfc_elevation)
            if layer['spread'] <= 5.0:
                if h_agl < 1000 and sfc_spread <= 3.0 and vis_sm >= 1.5 and wx < 50:
                    continue
                c_base_agl = int(round(h_agl, -2))
                c_amt = "SCT"
                if c_base_agl == 0 and vis_sm > 0.62 and wx not in [45, 48]:
                    c_base_agl = 100
                break

    # Pass 3: FEW (spread ≤ 7.0)
    if c_amt == "CLR":
        for layer in search_profile:
            h_agl = max(0, layer['h'] - sfc_elevation)
            if layer['spread'] <= 7.0:
                if h_agl < 1000 and sfc_spread <= 3.0 and vis_sm >= 1.5 and wx < 50:
                    continue
                c_base_agl = int(round(h_agl, -2))
                c_amt = "FEW"
                if c_base_agl == 0 and vis_sm > 0.62 and wx not in [45, 48]:
                    c_base_agl = 100
                break

    # Convective fallback: CCL estimation
    if is_convective and c_amt == "CLR":
        ccl_base = int(round(max(0, sfc_spread * CONVECTIVE_CCL_MULTIPLIER), -2))
        if ccl_base < 10000:
            c_base_agl = ccl_base
            c_amt = "BKN" if wx >= 80 else "SCT"

    return c_base_agl, c_amt


def compute_gusts(
    w_spd: float,
    raw_gust_list: list | None,
    idx: int,
    k_conv: float,
) -> tuple[float, float]:
    """Computes gust speed and gust delta from NWP gust data.

    Enforces a 1.25× floor when the model reports gusts ≤ sustained.

    Returns (gust_speed_kt, gust_delta_kt).
    """
    if raw_gust_list and len(raw_gust_list) > idx and raw_gust_list[idx] is not None:
        raw_gst = float(raw_gust_list[idx]) * k_conv
    else:
        raw_gst = w_spd

    gst = (w_spd * 1.25) if raw_gst <= w_spd else raw_gst
    gust_delta = max(0, gst - w_spd)
    return gst, gust_delta


def evaluate_blsn(
    wx: int,
    t_temp: float,
    w_spd: float,
    gst: float,
    sn_depth: float,
    vis_sm: float,
) -> tuple[bool, bool, float]:
    """Evaluates Blowing Snow (BLSN) and Drifting Snow (DRSN) conditions.

    Returns (blsn_trigger, drsn_trigger, adjusted_vis_sm).
    """
    is_snowing = wx in [71, 73, 75, 77, 85, 86, 68, 69]
    is_cold_snow = t_temp <= -5.0
    has_snowpack = sn_depth >= SNOWPACK_BLSN_THRESHOLD_M

    blsn_trigger = False
    drsn_trigger = False

    if is_cold_snow:
        if is_snowing:
            if w_spd >= 20.0 or gst >= 30.0:
                blsn_trigger = True
        elif has_snowpack:
            if w_spd >= 25.0 or gst >= 35.0:
                blsn_trigger = True
            elif w_spd >= 15.0 or gst >= 20.0:
                drsn_trigger = True

    adjusted_vis = vis_sm
    if blsn_trigger and vis_sm is not None and vis_sm > 4.0:   # None = model sent no visibility
        adjusted_vis = max(1.5, vis_sm * 0.5)

    return blsn_trigger, drsn_trigger, adjusted_vis


def cloud_depth_scan(thermal_profile: list[dict]) -> float:
    """Scans the profile for cloud depth (vertical extent of ≤4°C spread layers).

    Returns cloud depth in feet (0 if no cloud detected).
    """
    cb_v = ct_v = None
    for layer in thermal_profile:
        if layer['spread'] <= 4.0:
            if cb_v is None:
                cb_v = layer['h']
            ct_v = layer['h']
    return (ct_v - cb_v) if cb_v and ct_v else 0


def get_interp_thermals(alt_msl: float, profile: list[dict]) -> tuple[float, int]:
    """Interpolates temperature and RH at an arbitrary MSL altitude within the profile."""
    if not profile:
        return 0.0, 0
    if alt_msl <= profile[0]['h']:
        return profile[0]['t'], profile[0]['rh']
    if alt_msl >= profile[-1]['h']:
        return profile[-1]['t'], profile[-1]['rh']

    for k in range(len(profile) - 1):
        if profile[k]['h'] <= alt_msl <= profile[k + 1]['h']:
            lower = profile[k]
            upper = profile[k + 1]
            frac = (alt_msl - lower['h']) / (upper['h'] - lower['h']) if upper['h'] != lower['h'] else 0
            i_t = lower['t'] + frac * (upper['t'] - lower['t'])
            i_rh = lower['rh'] + frac * (upper['rh'] - lower['rh'])
            return i_t, int(i_rh)

    return profile[0]['t'], profile[0]['rh']
