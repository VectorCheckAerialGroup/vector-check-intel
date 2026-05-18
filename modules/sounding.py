"""
VECTOR CHECK AERIAL GROUP INC. — Interactive Sounding Module

Renders a high-resolution Skew-T-log-p sounding with:
  - Every available Open-Meteo pressure level (1000 down to 100 hPa)
  - Wind barb at every level
  - Dry / moist adiabats and isohumes as background reference
  - Saturation shading where T - Td <= 2 °C
  - ASL altitude axis on the right
  - Slider-driven parcel ascent with CAPE / CIN shading and diagnostics

PHYSICS (standard):
  - Poisson's relation for dry adiabat (potential temperature conservation)
  - Bolton (1980) for vapor pressure and pseudo-adiabatic ascent
  - LCL via Espy's approximation refined with iterative dewpoint convergence
  - CAPE / CIN by trapezoidal integration of Rd * (T_parcel - T_env) * d(ln(P))
"""

import math
import numpy as np
import plotly.graph_objects as go
from typing import Optional

from modules.physics import calc_td


# =============================================================================
# CONSTANTS
# =============================================================================

# Thermodynamic constants (SI)
RD = 287.04           # Specific gas constant for dry air, J/(kg·K)
RV = 461.5            # Specific gas constant for water vapor, J/(kg·K)
CP = 1005.7           # Specific heat of dry air at constant pressure, J/(kg·K)
G = 9.80665           # Gravitational acceleration, m/s²
EPSILON = RD / RV     # ≈ 0.622
KAPPA = RD / CP       # ≈ 0.2854 (Poisson's exponent)
LV = 2.501e6          # Latent heat of vaporization at 0°C, J/kg
KMH_TO_KT = 0.539957

# Open-Meteo pressure levels — all available levels in the troposphere
_OM_PRESSURES = [1000, 975, 950, 925, 900, 875, 850, 825, 800, 775, 750,
                 725, 700, 675, 650, 625, 600, 575, 550, 525, 500,
                 475, 450, 425, 400, 375, 350, 325, 300, 275, 250,
                 225, 200, 175, 150, 125, 100]


# =============================================================================
# THERMODYNAMIC FUNCTIONS
# =============================================================================

def _saturation_vapor_pressure(T_C: float) -> float:
    """Saturation vapor pressure in hPa using Bolton 1980."""
    return 6.112 * math.exp((17.67 * T_C) / (T_C + 243.5))


def _saturation_mixing_ratio(T_C: float, P_hPa: float) -> float:
    """Saturation mixing ratio in kg/kg."""
    es = _saturation_vapor_pressure(T_C)
    if es >= P_hPa:
        return 0.0
    return EPSILON * es / (P_hPa - es)


def _dry_adiabat_temp(theta_K: float, P_hPa: float) -> float:
    """Temperature in °C on dry adiabat θ at pressure P."""
    return theta_K * (P_hPa / 1000.0) ** KAPPA - 273.15


def _potential_temperature(T_C: float, P_hPa: float) -> float:
    """Potential temperature θ (K) from T (°C) at pressure P (hPa)."""
    return (T_C + 273.15) * (1000.0 / P_hPa) ** KAPPA


def _moist_adiabat_step(T_C: float, P_hPa: float, dP_hPa: float) -> float:
    """One step along a moist (pseudo) adiabat using the explicit dT/dlnP form.

    The saturated pseudo-adiabatic temperature change with pressure is:

        dT/dlnP = (Rd/Cp) * (T_K + Lv*rs/Rd)
                  / (1 + Lv^2 * rs * EPSILON / (Cp * Rd * T_K^2))

    (AMS Glossary / Bohren & Albrecht form.) This gives roughly 0.3-0.5 °C of
    cooling per 5 hPa in the lower troposphere, asymptoting toward the dry rate
    aloft as the saturation mixing ratio rs -> 0.

    Returns dT in °C for a pressure change of dP (negative when ascending).
    """
    T_K = T_C + 273.15
    rs = _saturation_mixing_ratio(T_C, P_hPa)
    numer = T_K + (LV * rs / RD)
    denom = 1.0 + (LV * LV * rs * EPSILON) / (CP * RD * T_K * T_K)
    dT_dlnP = (RD / CP) * (numer / denom)
    # dlnP for this pressure step
    dlnP = dP_hPa / P_hPa
    return dT_dlnP * dlnP


def _lcl_pressure(T_C: float, Td_C: float, P_hPa: float) -> tuple:
    """Iterates the dry adiabat down (parcel up) until saturation.

    Returns (P_lcl_hPa, T_lcl_C).
    """
    if Td_C >= T_C:
        return (P_hPa, T_C)  # already saturated

    theta = _potential_temperature(T_C, P_hPa)
    # Initial Td mixing ratio (conserved during dry ascent)
    e_d = _saturation_vapor_pressure(Td_C)
    w_parcel = EPSILON * e_d / (P_hPa - e_d)

    # March upward in 5 hPa steps until saturation
    p = P_hPa
    while p > 100.0:
        p -= 5.0
        T_dry = _dry_adiabat_temp(theta, p)
        w_sat = _saturation_mixing_ratio(T_dry, p)
        if w_sat <= w_parcel:
            return (p, T_dry)
    return (100.0, _dry_adiabat_temp(theta, 100.0))


def lift_parcel(P0_hPa: float, T0_C: float, Td0_C: float,
                P_top_hPa: float = 100.0,
                dP_hPa: float = 5.0) -> dict:
    """Lifts a parcel from (P0, T0, Td0) to P_top.

    Returns dict with arrays of pressures, parcel temperatures, and the
    diagnostic levels.
    """
    # Stage 1: dry adiabat from P0 up to LCL
    P_lcl, T_lcl = _lcl_pressure(T0_C, Td0_C, P0_hPa)
    theta = _potential_temperature(T0_C, P0_hPa)

    pressures = []
    parcel_T = []

    # Build dense pressure grid from P0 down to P_top
    # Stage 1: dry adiabat from P0 up to (but not past) the LCL
    p = P0_hPa
    while p >= P_top_hPa:
        if p >= P_lcl:
            # Dry adiabat segment — append pressure and temperature together
            pressures.append(p)
            parcel_T.append(_dry_adiabat_temp(theta, p))
        else:
            # We have crossed the LCL — stop the dry segment here
            break
        p -= dP_hPa

    # Stage 2: moist adiabat from LCL down to P_top
    # Insert the exact LCL point for a clean kink (only if not already there)
    if not pressures or pressures[-1] > P_lcl:
        pressures.append(P_lcl)
        parcel_T.append(T_lcl)

    T_current = T_lcl
    p_current = P_lcl
    while p_current > P_top_hPa:
        dp = -dP_hPa
        dT = _moist_adiabat_step(T_current, p_current, dp)
        T_current = T_current + dT
        p_current = p_current + dp
        if p_current < P_top_hPa:
            break
        pressures.append(p_current)
        parcel_T.append(T_current)

    return {
        "pressures": np.array(pressures),
        "parcel_T": np.array(parcel_T),
        "P_lcl": P_lcl,
        "T_lcl": T_lcl,
        "P_start": P0_hPa,
        "T_start": T0_C,
        "Td_start": Td0_C,
    }


def compute_cape_cin(parcel: dict, env_pressures: np.ndarray,
                     env_temps: np.ndarray) -> dict:
    """Computes CAPE and CIN from a lifted parcel against an environment.

    Both integrations use trapezoidal rule in (ln P, T) space:
        CAPE = Rd * integral over positive-buoyancy region of (T_p - T_e) d(lnP)
        CIN  = Rd * integral over negative-buoyancy region of (T_p - T_e) d(lnP)
    Units: J/kg (positive CAPE, negative CIN).

    Also identifies LFC (Level of Free Convection) and EL (Equilibrium Level).
    """
    p_parcel = parcel["pressures"]
    T_parcel = parcel["parcel_T"]

    # Interpolate environmental T to the parcel's pressure grid (in log-P space).
    # env arrays are in descending pressure order (surface first).
    env_p_sorted = np.array(env_pressures)
    env_t_sorted = np.array(env_temps)
    order = np.argsort(-env_p_sorted)  # high P first
    env_p_sorted = env_p_sorted[order]
    env_t_sorted = env_t_sorted[order]

    # The valid environmental pressure range. We must NOT extrapolate the
    # environment beyond its top observed level — np.interp clamps to the edge
    # value, which would hold the environment at a fixed cold temperature while
    # the parcel keeps cooling slowly, producing physically impossible CAPE.
    env_p_min = float(env_p_sorted.min())   # top of observed environment
    env_p_max = float(env_p_sorted.max())   # bottom (surface)

    # Interp uses ascending x — so reverse
    log_env_p = np.log(env_p_sorted[::-1])
    env_t_ascending = env_t_sorted[::-1]
    log_p_parcel = np.log(p_parcel)
    T_env_interp = np.interp(log_p_parcel, log_env_p, env_t_ascending)

    # Mask: which parcel levels fall within the real environmental data range
    valid_env = (p_parcel >= env_p_min) & (p_parcel <= env_p_max)

    # Buoyancy in K (T parcel - T env)
    dT_K = T_parcel - T_env_interp

    # ----- Pass 1: locate LFC and EL from the buoyancy profile -----
    # LFC = first level at/above the LCL where buoyancy turns positive.
    # EL  = first level above the LFC where buoyancy returns to negative.
    lfc = None
    el = None
    P_lcl = parcel["P_lcl"]

    # Index of the first parcel level at or above the LCL
    lcl_idx = None
    for i in range(len(p_parcel)):
        if p_parcel[i] <= P_lcl and valid_env[i]:
            lcl_idx = i
            break

    # If the parcel is already positively buoyant at the LCL itself, the LFC
    # is the LCL (common for hot/humid surface-based parcels).
    if lcl_idx is not None and dT_K[lcl_idx] > 0:
        lfc = float(p_parcel[lcl_idx])

    for i in range(1, len(p_parcel)):
        # Skip steps that fall outside the real environmental data range
        if not (valid_env[i] and valid_env[i - 1]):
            continue

        # LFC: first negative -> positive crossing at/above the LCL
        if lfc is None and p_parcel[i] <= P_lcl:
            if dT_K[i - 1] <= 0 and dT_K[i] > 0:
                denom = (dT_K[i] - dT_K[i - 1])
                frac = (-dT_K[i - 1]) / denom if denom != 0 else 0.5
                lnP_cross = (math.log(p_parcel[i - 1])
                             + frac * (math.log(p_parcel[i]) - math.log(p_parcel[i - 1])))
                lfc = math.exp(lnP_cross)

        # EL: first positive -> negative crossing above the LFC
        if lfc is not None and el is None and p_parcel[i] < lfc:
            if dT_K[i - 1] >= 0 and dT_K[i] < 0:
                denom = (dT_K[i - 1] - dT_K[i])
                frac = dT_K[i - 1] / denom if denom != 0 else 0.5
                lnP_cross = (math.log(p_parcel[i - 1])
                             + frac * (math.log(p_parcel[i]) - math.log(p_parcel[i - 1])))
                el = math.exp(lnP_cross)

    # ----- Pass 2: integrate CAPE (LFC->EL) and CIN (start->LFC) -----
    # Trapezoidal integration of Rd * dT * d(lnP). lnP decreases as the parcel
    # rises, so we sign-correct with -dlnP to keep CAPE positive.
    cape = 0.0
    cin = 0.0

    if lfc is not None:
        el_bound = el if el is not None else env_p_min
        for i in range(1, len(p_parcel)):
            if not (valid_env[i] and valid_env[i - 1]):
                continue
            p_lo = p_parcel[i - 1]
            p_hi = p_parcel[i]
            dT_mean = 0.5 * (dT_K[i - 1] + dT_K[i])
            dlnP = math.log(p_hi) - math.log(p_lo)        # negative (rising)
            increment = RD * dT_mean * (-dlnP)

            mid_p = 0.5 * (p_lo + p_hi)
            if mid_p > lfc:
                # Below the LFC — negative buoyancy contributes to CIN
                if dT_mean < 0:
                    cin += increment
            elif lfc >= mid_p >= el_bound:
                # Between LFC and EL — positive buoyancy contributes to CAPE
                if dT_mean > 0:
                    cape += increment
            # Above EL: ignored (parcel is no longer convectively relevant)
    else:
        # No LFC — the parcel never becomes freely convective from this level.
        # CAPE is zero. CIN is only physically meaningful as the barrier up to
        # where convection *would* initiate; with no LFC there is no barrier to
        # quote, so we report a bounded value: the negative area from the start
        # level up to the LCL plus a short distance above (the layer a parcel
        # would realistically be forced through). Integrating to the tropopause
        # would produce a meaningless multi-thousand J/kg figure.
        cin_top = P_lcl - 100.0  # ~100 hPa above the LCL
        for i in range(1, len(p_parcel)):
            if not (valid_env[i] and valid_env[i - 1]):
                continue
            if p_parcel[i] < cin_top:
                break
            dT_mean = 0.5 * (dT_K[i - 1] + dT_K[i])
            dlnP = math.log(p_parcel[i]) - math.log(p_parcel[i - 1])
            if dT_mean < 0:
                cin += RD * dT_mean * (-dlnP)
            dlnP = math.log(p_parcel[i]) - math.log(p_parcel[i - 1])
            if dT_mean < 0:
                cin += RD * dT_mean * (-dlnP)

    return {
        "cape": round(cape, 0),
        "cin":  round(cin, 0),
        "lfc":  round(lfc, 0) if lfc else None,
        "el":   round(el, 0) if el else None,
        "T_env_at_parcel_p": T_env_interp,
        "dT_K": dT_K,
    }


# =============================================================================
# PROFILE EXTRACTION
# =============================================================================

def extract_high_res_profile(h: dict, idx: int,
                             sfc_T: float, sfc_Td: float,
                             sfc_P: float) -> Optional[dict]:
    """Pulls all available pressure-level data plus surface.

    Returns dict with arrays sorted by descending pressure.
    Returns None if there's not enough data.
    """
    pressures = [sfc_P]
    temps = [sfc_T]
    dewpts = [sfc_Td]
    heights = [0.0]
    wind_kt = [None]   # surface wind filled in by caller
    wind_dir = [None]

    for p in _OM_PRESSURES:
        if p >= sfc_P:
            continue
        t_list = h.get(f'temperature_{p}hPa')
        rh_list = h.get(f'relative_humidity_{p}hPa')
        z_list = h.get(f'geopotential_height_{p}hPa')
        ws_list = h.get(f'wind_speed_{p}hPa')
        wd_list = h.get(f'wind_direction_{p}hPa')

        if not t_list or not rh_list or len(t_list) <= idx:
            continue
        t_v = t_list[idx]
        rh_v = rh_list[idx]
        if t_v is None or rh_v is None:
            continue
        try:
            t_c = float(t_v)
            rh_pct = max(0.0, min(100.0, float(rh_v)))
        except (TypeError, ValueError):
            continue

        td_c = calc_td(t_c, rh_pct)

        # Height in metres (None if unavailable)
        z_m = None
        if z_list and len(z_list) > idx and z_list[idx] is not None:
            try:
                z_m = float(z_list[idx])
            except (TypeError, ValueError):
                z_m = None

        # Wind at this level
        ws_v = None
        wd_v = None
        if ws_list and len(ws_list) > idx and ws_list[idx] is not None:
            try:
                # Open-Meteo serves wind in km/h on this endpoint family
                ws_v = float(ws_list[idx]) * KMH_TO_KT
            except (TypeError, ValueError):
                ws_v = None
        if wd_list and len(wd_list) > idx and wd_list[idx] is not None:
            try:
                wd_v = float(wd_list[idx])
            except (TypeError, ValueError):
                wd_v = None

        pressures.append(p)
        temps.append(t_c)
        dewpts.append(td_c)
        heights.append(z_m if z_m is not None else float('nan'))
        wind_kt.append(ws_v)
        wind_dir.append(wd_v)

    if len(pressures) < 5:
        return None

    return {
        "pressures": np.array(pressures),
        "temps": np.array(temps),
        "dewpts": np.array(dewpts),
        "heights_m": np.array(heights),
        "wind_kt": wind_kt,        # list (None-allowed)
        "wind_dir": wind_dir,      # list (None-allowed)
    }


# =============================================================================
# SKEW-T COORDINATE TRANSFORM
# =============================================================================

P_BOTTOM = 1050.0
P_TOP = 100.0
SKEW_C_PER_DECADE = 35.0


def skew_x(temp_C, pressure_hPa):
    """Skew transform: shift x by skew rate × log10(P / P_BOTTOM)."""
    pressure_hPa = np.asarray(pressure_hPa, dtype=float)
    return temp_C - SKEW_C_PER_DECADE * np.log10(pressure_hPa / P_BOTTOM)


# =============================================================================
# PLOTLY SOUNDING RENDERER
# =============================================================================

def render_sounding_plotly(profile: dict, parcel_lift_p: float,
                            title: str = "", panel_color: str = "#D1D5DB",
                            sfc_elevation_ft: float = 0.0,
                            x_range: tuple = (-40, 40)) -> tuple:
    """Renders the interactive Skew-T as a Plotly figure.

    Args:
        profile:           output of extract_high_res_profile
        parcel_lift_p:     pressure level (hPa) from which to lift the parcel
        title:             panel title text
        panel_color:       title color (amber for current hour)
        sfc_elevation_ft:  station elevation (ft) for right-axis labels
        x_range:           (min, max) temperature axis range

    Returns:
        (figure, diagnostics_dict)
    """
    pressures = profile["pressures"]
    temps = profile["temps"]
    dewpts = profile["dewpts"]
    heights_m = profile["heights_m"]
    wind_kt_list = profile["wind_kt"]
    wind_dir_list = profile["wind_dir"]

    # ----- Compute the parcel from the requested level -----
    # Find environmental T, Td at the requested lift pressure (interpolated)
    order = np.argsort(-pressures)
    p_sorted = pressures[order]
    t_sorted = temps[order]
    td_sorted = dewpts[order]
    # Interp expects ascending x — reverse
    log_p_asc = np.log(p_sorted[::-1])
    t_asc = t_sorted[::-1]
    td_asc = td_sorted[::-1]

    target_lnp = math.log(parcel_lift_p)
    T_lift = float(np.interp(target_lnp, log_p_asc, t_asc))
    Td_lift = float(np.interp(target_lnp, log_p_asc, td_asc))

    parcel = lift_parcel(parcel_lift_p, T_lift, Td_lift, P_top_hPa=P_TOP)
    cape_cin = compute_cape_cin(parcel, pressures, temps)

    # ----- Build figure -----
    fig = go.Figure()

    p_grid_dense = np.geomspace(P_BOTTOM, P_TOP, 60)

    # Background isotherms every 10°C
    for t_iso in range(-100, 51, 10):
        iso_x = skew_x(t_iso, p_grid_dense)
        fig.add_trace(go.Scatter(
            x=iso_x, y=p_grid_dense,
            mode="lines", line=dict(color="#3E444E", width=0.6),
            hoverinfo="skip", showlegend=False,
        ))

    # Freezing isotherm
    fig.add_trace(go.Scatter(
        x=skew_x(0, p_grid_dense), y=p_grid_dense,
        mode="lines", line=dict(color="#3498DB", width=1.2, dash="dash"),
        hoverinfo="skip", showlegend=False, name="0°C",
    ))

    # Dry adiabats every 10 K
    for theta_K in range(260, 411, 10):
        t_C = theta_K * (p_grid_dense / 1000.0) ** KAPPA - 273.15
        fig.add_trace(go.Scatter(
            x=skew_x(t_C, p_grid_dense), y=p_grid_dense,
            mode="lines", line=dict(color="#8a6d3b", width=0.6),
            hoverinfo="skip", showlegend=False, opacity=0.45,
        ))

    # Moist adiabats every 5°C surface
    for T0 in range(-20, 41, 5):
        ma_p = []
        ma_T = []
        T_cur = T0
        p_cur = 1000.0
        ma_p.append(p_cur); ma_T.append(T_cur)
        while p_cur > P_TOP:
            dp = -5.0
            dT = _moist_adiabat_step(T_cur, p_cur, dp)
            T_cur += dT
            p_cur += dp
            ma_p.append(p_cur); ma_T.append(T_cur)
        ma_p = np.array(ma_p)
        ma_T = np.array(ma_T)
        fig.add_trace(go.Scatter(
            x=skew_x(ma_T, ma_p), y=ma_p,
            mode="lines",
            line=dict(color="#3a8c63", width=0.6, dash="dot"),
            hoverinfo="skip", showlegend=False, opacity=0.4,
        ))

    # Saturation shading (T - Td <= 2°C)
    # Densify the profile and shade where spread is small
    p_sat_grid = np.geomspace(pressures.max(), pressures.min(), 80)
    log_p_asc_sat = np.log(p_sorted[::-1])
    t_dense = np.interp(np.log(p_sat_grid), log_p_asc_sat, t_asc)
    td_dense = np.interp(np.log(p_sat_grid), log_p_asc_sat, td_asc)
    sk_t_dense = skew_x(t_dense, p_sat_grid)
    sk_td_dense = skew_x(td_dense, p_sat_grid)
    sat_mask = (t_dense - td_dense) <= 2.0

    # Build shaded segments
    in_seg = False
    seg_start = 0
    for i, m in enumerate(sat_mask):
        if m and not in_seg:
            seg_start = i; in_seg = True
        elif not m and in_seg:
            # Render the segment
            xs = np.concatenate([sk_t_dense[seg_start:i], sk_td_dense[seg_start:i][::-1]])
            ys = np.concatenate([p_sat_grid[seg_start:i], p_sat_grid[seg_start:i][::-1]])
            fig.add_trace(go.Scatter(
                x=xs, y=ys, fill="toself",
                fillcolor="rgba(203, 213, 225, 0.35)",
                line=dict(width=0), hoverinfo="skip",
                showlegend=False,
            ))
            in_seg = False
    if in_seg:
        xs = np.concatenate([sk_t_dense[seg_start:], sk_td_dense[seg_start:][::-1]])
        ys = np.concatenate([p_sat_grid[seg_start:], p_sat_grid[seg_start:][::-1]])
        fig.add_trace(go.Scatter(
            x=xs, y=ys, fill="toself",
            fillcolor="rgba(203, 213, 225, 0.35)",
            line=dict(width=0), hoverinfo="skip", showlegend=False,
        ))

    # CAPE / CIN shading along the parcel trace
    parcel_p = parcel["pressures"]
    parcel_T = parcel["parcel_T"]
    T_env_at_p = cape_cin["T_env_at_parcel_p"]
    dT = cape_cin["dT_K"]

    cape_x = skew_x(parcel_T, parcel_p)
    env_x = skew_x(T_env_at_p, parcel_p)

    # Walk segments where dT > 0 (CAPE) and dT < 0 (CIN) and build polygons
    def _shade_buoyancy(positive: bool):
        target = (dT > 0) if positive else (dT < 0)
        color = ("rgba(239, 68, 68, 0.25)" if positive
                 else "rgba(59, 130, 246, 0.20)")
        in_seg = False
        seg_start = 0
        for i, m in enumerate(target):
            if m and not in_seg:
                seg_start = i; in_seg = True
            elif not m and in_seg:
                xs = np.concatenate([cape_x[seg_start:i], env_x[seg_start:i][::-1]])
                ys = np.concatenate([parcel_p[seg_start:i], parcel_p[seg_start:i][::-1]])
                fig.add_trace(go.Scatter(
                    x=xs, y=ys, fill="toself", fillcolor=color,
                    line=dict(width=0), hoverinfo="skip", showlegend=False,
                ))
                in_seg = False
        if in_seg:
            xs = np.concatenate([cape_x[seg_start:], env_x[seg_start:][::-1]])
            ys = np.concatenate([parcel_p[seg_start:], parcel_p[seg_start:][::-1]])
            fig.add_trace(go.Scatter(
                x=xs, y=ys, fill="toself", fillcolor=color,
                line=dict(width=0), hoverinfo="skip", showlegend=False,
            ))

    _shade_buoyancy(True)
    _shade_buoyancy(False)

    # Environmental T (red) — with hover showing each data point
    hover_text = []
    for i, p in enumerate(pressures):
        h_m = heights_m[i] if not np.isnan(heights_m[i]) else None
        h_ft = round(h_m * 3.28084) if h_m else None
        h_part = f"<br>{h_ft:,} ft ASL" if h_ft else ""
        hover_text.append(
            f"<b>{int(p)} hPa</b>{h_part}<br>"
            f"T = {temps[i]:.1f}°C<br>"
            f"Td = {dewpts[i]:.1f}°C<br>"
            f"Spread = {temps[i] - dewpts[i]:.1f}°C"
        )
    fig.add_trace(go.Scatter(
        x=skew_x(temps, pressures), y=pressures,
        mode="lines+markers", name="Temperature",
        line=dict(color="#ff4b4b", width=2.2),
        marker=dict(size=5, color="#ff4b4b"),
        hovertext=hover_text, hoverinfo="text",
        showlegend=False,
    ))

    # Environmental Td (green)
    fig.add_trace(go.Scatter(
        x=skew_x(dewpts, pressures), y=pressures,
        mode="lines+markers", name="Dewpoint",
        line=dict(color="#2abf2a", width=2.2),
        marker=dict(size=5, color="#2abf2a"),
        hoverinfo="skip", showlegend=False,
    ))

    # Lifted parcel trace (red dashed)
    fig.add_trace(go.Scatter(
        x=skew_x(parcel["parcel_T"], parcel["pressures"]),
        y=parcel["pressures"],
        mode="lines", name="Parcel",
        line=dict(color="#ef4444", width=2.0, dash="dash"),
        hovertemplate=("<b>Lifted Parcel</b><br>"
                       "%{y:.0f} hPa<br>"
                       "T = %{customdata:.1f}°C<extra></extra>"),
        customdata=parcel["parcel_T"],
        showlegend=False,
    ))

    # LCL marker
    lcl_x = skew_x(parcel["T_lcl"], parcel["P_lcl"])
    fig.add_trace(go.Scatter(
        x=[lcl_x], y=[parcel["P_lcl"]],
        mode="markers+text",
        marker=dict(size=10, color="#fbbf24", symbol="circle-open",
                    line=dict(width=2)),
        text=["LCL"], textposition="middle right",
        textfont=dict(color="#fbbf24", size=10),
        hovertemplate=f"<b>LCL</b><br>{parcel['P_lcl']:.0f} hPa<br>{parcel['T_lcl']:.1f}°C<extra></extra>",
        showlegend=False,
    ))

    # LFC and EL markers if present
    if cape_cin["lfc"]:
        # Get T_parcel at LFC pressure
        T_lfc = float(np.interp(math.log(cape_cin["lfc"]),
                                np.log(parcel["pressures"][::-1]),
                                parcel["parcel_T"][::-1]))
        fig.add_trace(go.Scatter(
            x=[skew_x(T_lfc, cape_cin["lfc"])], y=[cape_cin["lfc"]],
            mode="markers+text",
            marker=dict(size=10, color="#a78bfa", symbol="diamond-open",
                        line=dict(width=2)),
            text=["LFC"], textposition="middle right",
            textfont=dict(color="#a78bfa", size=10),
            hovertemplate=f"<b>LFC</b><br>{cape_cin['lfc']:.0f} hPa<extra></extra>",
            showlegend=False,
        ))

    if cape_cin["el"]:
        T_el = float(np.interp(math.log(cape_cin["el"]),
                               np.log(parcel["pressures"][::-1]),
                               parcel["parcel_T"][::-1]))
        fig.add_trace(go.Scatter(
            x=[skew_x(T_el, cape_cin["el"])], y=[cape_cin["el"]],
            mode="markers+text",
            marker=dict(size=10, color="#06b6d4", symbol="diamond-open",
                        line=dict(width=2)),
            text=["EL"], textposition="middle right",
            textfont=dict(color="#06b6d4", size=10),
            hovertemplate=f"<b>EL</b><br>{cape_cin['el']:.0f} hPa<extra></extra>",
            showlegend=False,
        ))

    # Wind barbs on the right side of the data area. Drawn as short directional
    # shafts (pointing the direction the wind is coming FROM) with the speed in
    # knots as a small label. To avoid clutter with ~30 levels, we draw a barb
    # at most every ~40 hPa.
    barb_x_data = x_range[1] - 3
    _last_barb_p = None
    for p, ws, wd in zip(pressures, wind_kt_list, wind_dir_list):
        if ws is None or wd is None:
            continue
        # Declutter: skip if within 40 hPa of the previous barb (except always
        # draw the surface barb, which is the first entry)
        if _last_barb_p is not None and abs(_last_barb_p - p) < 40 and p != pressures[0]:
            continue
        _last_barb_p = p

        # Shaft points toward where the wind comes FROM. In skewed chart space
        # we only use the shaft for a directional cue; magnitude is the label.
        rad = math.radians(wd)
        shaft_len = 2.6
        tail_dx = math.sin(rad) * shaft_len
        # Color-code the shaft by speed band for quick reading
        if ws >= 35:
            _barb_color = "#ff6b4a"
        elif ws >= 20:
            _barb_color = "#E58E26"
        else:
            _barb_color = "#94a3b8"

        fig.add_annotation(
            x=barb_x_data, y=p,
            ax=barb_x_data + tail_dx, ay=p,
            xref="x", yref="y", axref="x", ayref="y",
            showarrow=True, arrowhead=2, arrowsize=1, arrowwidth=1.3,
            arrowcolor=_barb_color,
            text="", standoff=0,
        )
        # Speed label just right of the barb
        fig.add_annotation(
            x=barb_x_data + 3.2, y=p,
            xref="x", yref="y",
            showarrow=False,
            text=f"{ws:.0f}",
            font=dict(size=8, color=_barb_color),
            xanchor="left", yanchor="middle",
        )

    # Layout
    # Compute ASL ticks from pressure ticks using US Standard Atmosphere
    p_ticks = [1000, 850, 700, 500, 300, 200]
    def _p_to_ft_asl(p_hpa):
        return (288.15 / 0.0065) * (1 - (p_hpa / 1013.25) ** (1/5.25588)) * 3.28084
    alt_labels = [f"{int(_p_to_ft_asl(p)):,} ft" for p in p_ticks]

    fig.update_layout(
        title=dict(text=title, font=dict(color=panel_color, size=13)),
        height=540,
        margin=dict(l=42, r=70, t=40, b=36),
        plot_bgcolor="#1B1E23",
        paper_bgcolor="#1B1E23",
        xaxis=dict(
            range=list(x_range),
            tickfont=dict(color="#8E949E", size=9),
            title=dict(text="°C", font=dict(color="#A0A4AB", size=9)),
            showgrid=False, zeroline=False, fixedrange=True,
        ),
        yaxis=dict(
            type="log",
            range=[math.log10(P_BOTTOM), math.log10(P_TOP)],
            tickvals=p_ticks,
            ticktext=[str(p) for p in p_ticks],
            tickfont=dict(color="#8E949E", size=9),
            title=dict(text="hPa", font=dict(color="#A0A4AB", size=9)),
            showgrid=False, zeroline=False, fixedrange=True,
            autorange="reversed",
        ),
        showlegend=False,
        hovermode="closest",
        dragmode=False,
    )

    # Secondary y-axis (ASL feet) on the right
    fig.update_layout(
        yaxis2=dict(
            type="log",
            range=[math.log10(P_BOTTOM), math.log10(P_TOP)],
            tickvals=p_ticks,
            ticktext=alt_labels,
            tickfont=dict(color="#8E949E", size=8),
            overlaying="y",
            side="right",
            showgrid=False,
            autorange="reversed",
        ),
    )

    # Plotly only renders an overlaying axis if at least one trace references
    # it. Attach a fully transparent anchor trace spanning the pressure range.
    fig.add_trace(go.Scatter(
        x=[x_range[0], x_range[0]],
        y=[P_BOTTOM, P_TOP],
        mode="markers",
        marker=dict(size=0.1, color="rgba(0,0,0,0)"),
        yaxis="y2",
        hoverinfo="skip",
        showlegend=False,
    ))

    diagnostics = {
        "cape": cape_cin["cape"],
        "cin": cape_cin["cin"],
        "lcl_hpa": parcel["P_lcl"],
        "lcl_ft": _p_to_ft_asl(parcel["P_lcl"]),
        "lfc_hpa": cape_cin["lfc"],
        "el_hpa": cape_cin["el"],
        "lift_from_p": parcel_lift_p,
        "lift_from_t": T_lift,
        "lift_from_td": Td_lift,
    }
    return fig, diagnostics
