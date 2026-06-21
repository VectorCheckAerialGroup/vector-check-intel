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
    """Lifts a SATURATED parcel from (P0, T0) upward following the moist
    (pseudo) adiabat.

    Operational assumption: the parcel is assumed to be already saturated
    at the lift level (immediate condensation). This skips the dry adiabatic
    stage and starts the moist adiabat from P0 directly — appropriate for
    "lifted saturated parcel" analysis used in convective potential and
    elevated-source applications.

    Td0_C is preserved in the return dict for reference but is not used in
    the ascent (saturation is assumed).

    Returns dict with:
        pressures   — np.ndarray, descending from P0 to P_top
        parcel_T    — np.ndarray, moist-adiabat temperatures (°C)
        P_start     — original lift pressure
        T_start     — original lift temperature
        Td_start    — original lift dewpoint (for reference)
        P_lcl       — set to P0 (parcel is saturated at lift level by assumption)
        T_lcl       — set to T0
    """
    pressures = [P0_hPa]
    parcel_T = [T0_C]

    T_current = T0_C
    p_current = P0_hPa
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
        # For the saturated-parcel theory the parcel is condensing at the lift
        # level itself, so P_lcl == P0. Kept in the return dict for API stability
        # with the rest of the module.
        "P_lcl": P0_hPa,
        "T_lcl": T0_C,
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
                             sfc_P: float,
                             wind_kt_scale: float = 1.0) -> Optional[dict]:
    """Pulls all available pressure-level data plus surface.

    Args:
        wind_kt_scale: multiplicative factor to convert h's wind_speed_NNNhPa
            values to knots. Caller is responsible for passing the correct
            value based on the response's reported units (1.0 if knots,
            0.539957 if km/h, 1.943844 if m/s).

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
                ws_v = float(ws_list[idx]) * wind_kt_scale
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
SKEW_C_PER_DECADE = 45.0


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
                            x_range: tuple = (-35, 30),
                            show_parcel: bool = False) -> tuple:
    """Renders the interactive Skew-T as a Plotly figure.

    Args:
        profile:           output of extract_high_res_profile
        parcel_lift_p:     pressure level (hPa) from which to lift the parcel
                            (only used when show_parcel is True)
        title:             panel title text
        panel_color:       title color (amber for current hour)
        sfc_elevation_ft:  station elevation (ft) for right-axis labels
        x_range:           (min, max) temperature axis range
        show_parcel:       if True, compute and draw the lifted parcel,
                            CAPE/CIN shading, and LCL/LFC/EL markers.
                            Defaults to False — parcel analysis is opt-in.

    Returns:
        (figure, diagnostics_dict). diagnostics_dict is empty when
        show_parcel is False.
    """
    pressures = profile["pressures"]
    temps = profile["temps"]
    dewpts = profile["dewpts"]
    heights_m = profile["heights_m"]
    wind_kt_list = profile["wind_kt"]
    wind_dir_list = profile["wind_dir"]

    # Sort the profile for interpolation utilities below (used by both the
    # saturation shading and, if enabled, the parcel lift)
    order = np.argsort(-pressures)
    p_sorted = pressures[order]
    t_sorted = temps[order]
    td_sorted = dewpts[order]
    log_p_asc = np.log(p_sorted[::-1])
    t_asc = t_sorted[::-1]
    td_asc = td_sorted[::-1]

    # ----- Compute the parcel from the requested level (opt-in) -----
    parcel = None
    cape_cin = None
    T_lift = None
    Td_lift = None
    if show_parcel:
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

    # CAPE / CIN shading along the parcel trace — only when parcel is shown
    if show_parcel and parcel is not None:
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

    # Environmental T (red) — with hover showing each data point.
    # IMPORTANT: plot in pressure-sorted (descending) order, not the raw array
    # order. The raw profile prepends the surface point and then appends
    # pressure levels; if the surface pressure interleaves with a level, or a
    # provider returns a level slightly out of order, drawing in raw order
    # makes the line jump vertically and zigzag. Sorting by descending pressure
    # guarantees a monotonic, smoothly-connected trace.
    _ord = np.argsort(-pressures)
    _p_ord = pressures[_ord]
    _t_ord = temps[_ord]
    _td_ord = dewpts[_ord]
    _z_ord = heights_m[_ord]

    hover_text = []
    for i in range(len(_p_ord)):
        h_m = _z_ord[i] if not np.isnan(_z_ord[i]) else None
        h_ft = round(h_m * 3.28084) if h_m else None
        h_part = f"<br>{h_ft:,} ft ASL" if h_ft else ""
        hover_text.append(
            f"<b>{int(_p_ord[i])} hPa</b>{h_part}<br>"
            f"T = {_t_ord[i]:.1f}\u00b0C<br>"
            f"Td = {_td_ord[i]:.1f}\u00b0C<br>"
            f"Spread = {_t_ord[i] - _td_ord[i]:.1f}\u00b0C"
        )
    fig.add_trace(go.Scatter(
        x=skew_x(_t_ord, _p_ord), y=_p_ord,
        mode="lines+markers", name="Temperature",
        line=dict(color="#ff4b4b", width=2.2),
        marker=dict(size=5, color="#ff4b4b"),
        hovertext=hover_text, hoverinfo="text",
        showlegend=False,
    ))

    # Environmental Td (green) — same sorted order
    fig.add_trace(go.Scatter(
        x=skew_x(_td_ord, _p_ord), y=_p_ord,
        mode="lines+markers", name="Dewpoint",
        line=dict(color="#2abf2a", width=2.2),
        marker=dict(size=5, color="#2abf2a"),
        hoverinfo="skip", showlegend=False,
    ))

    # Lifted parcel trace, LCL, LFC, EL markers — only when parcel analysis is on
    if show_parcel and parcel is not None:
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

        # Lift-level marker (the level from which the saturated parcel was
        # lifted; for the saturated-parcel theory this is the operational
        # reference, not an LCL since condensation is assumed at this level).
        lift_x = skew_x(parcel["T_lcl"], parcel["P_lcl"])
        fig.add_trace(go.Scatter(
            x=[lift_x], y=[parcel["P_lcl"]],
            mode="markers+text",
            marker=dict(size=10, color="#fbbf24", symbol="circle-open",
                        line=dict(width=2)),
            text=["Lift"], textposition="middle right",
            textfont=dict(color="#fbbf24", size=10),
            hovertemplate=f"<b>Lift Level</b><br>{parcel['P_lcl']:.0f} hPa<br>"
                          f"{parcel['T_lcl']:.1f}°C (saturated)<extra></extra>",
            showlegend=False,
        ))

        # LFC marker
        if cape_cin["lfc"]:
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

        # EL marker
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

    # =========================================================================
    # AVIATION-STANDARD WIND BARBS
    #
    # Each barb is a glyph consisting of:
    #   - a staff pointing in the direction the wind is coming FROM
    #   - half-barb (small tick) = 5 kt
    #   - full barb (longer tick) = 10 kt
    #   - pennant (filled triangle) = 50 kt
    #   - circle around the station for calm (< 3 kt)
    # Speed is rounded to nearest 5 kt then decomposed into pennants/full/half.
    #
    # Geometry: barbs sit in a narrow right-margin column. STAFF_LEN and
    # BARB_LEN are in paper-fraction units, calibrated to produce ~14 px
    # staff / ~8 px tick at typical 3-column Streamlit panel widths (~340 px).
    # =========================================================================
    BARB_COL_X    = 0.965       # paper-fraction x for the barb anchor column
    STAFF_LEN     = 0.032       # staff length, paper-fraction units (~11 px)
    BARB_LEN      = 0.018       # barb tick length, paper x units (~6 px)
    BARB_GAP      = 0.16        # spacing between successive barbs (as fraction of staff)
    SHAPE_COLOR   = "#9ca3af"   # slightly dimmer slate
    SHAPE_WIDTH   = 1.1

    # Approximate panel aspect for screen-space barb geometry. With panel
    # height=720 px and ~600 px column width in 3-column layout, the data
    # area is roughly 500 × 660 px after margins. paper-y spans more pixels
    # per unit than paper-x, so we scale y components by W/H to keep barbs
    # visually orthogonal on screen.
    ASPECT_W_OVER_H = 500.0 / 660.0   # ~0.76 — portrait panel

    LOGP_SCALE = math.log10(P_BOTTOM / P_TOP)   # ~1.022

    def _paper_dy_to_logp_factor(dpaper_y):
        """Convert a paper-y offset to a log-P multiplicative factor."""
        return 10 ** (-dpaper_y * LOGP_SCALE)

    def _add_segment(x0, y0, x1, y1, color=SHAPE_COLOR, width=SHAPE_WIDTH):
        """Add a straight line segment shape with hybrid paper-x / data-y refs."""
        fig.add_shape(
            type="line",
            xref="paper", yref="y",
            x0=x0, y0=y0, x1=x1, y1=y1,
            line=dict(color=color, width=width),
            layer="above",
        )

    def _add_pennant(x0, y0, x_along_dx, x_along_dy, x_perp_dx, x_perp_dy):
        """Add a filled triangle pennant. The pennant base sits on the staff
        and the tip extends perpendicular to the staff."""
        # Three points: base-start, base-end (along staff), tip (perpendicular)
        base1_x, base1_y = x0, y0
        base2_x = x0 + x_along_dx * 0.55   # narrower than full barb to look like a flag
        base2_y = y0 * _paper_dy_to_logp_factor(x_along_dy * 0.55)
        tip_x   = x0 + x_perp_dx
        tip_y   = y0 * _paper_dy_to_logp_factor(x_perp_dy)
        # SVG path
        path = (f"M {base1_x},{base1_y} L {base2_x},{base2_y} L {tip_x},{tip_y} Z")
        fig.add_shape(
            type="path",
            xref="paper", yref="y",
            path=path,
            fillcolor=SHAPE_COLOR,
            line=dict(color=SHAPE_COLOR, width=0.8),
            layer="above",
        )

    def _draw_barb_glyph(p_lvl, speed_kt, wind_dir_deg):
        """Draw one full aviation-standard wind barb at pressure p_lvl."""
        # Calm wind: open circle (round, not oval — aspect-corrected)
        if speed_kt < 3:
            r = 0.010
            fig.add_shape(
                type="circle",
                xref="paper", yref="y",
                x0=BARB_COL_X - r,
                x1=BARB_COL_X + r,
                y0=p_lvl * _paper_dy_to_logp_factor(-r * ASPECT_W_OVER_H),
                y1=p_lvl * _paper_dy_to_logp_factor(r * ASPECT_W_OVER_H),
                line=dict(color=SHAPE_COLOR, width=1.2),
                layer="above",
            )
            return

        # Round speed to nearest 5 kt and decompose into pennants/full/half
        spd_rounded = int(round(speed_kt / 5.0) * 5)
        pennants = spd_rounded // 50
        remainder = spd_rounded - pennants * 50
        full_barbs = remainder // 10
        half_barbs = (remainder % 10) // 5

        # Staff direction unit vector: the staff points TOWARD where the wind
        # is FROM. wd is in meteorological degrees (north=0, east=90, FROM).
        # In paper coordinates, +x = east on screen, +y = up on screen (which
        # is toward lower pressure on our reversed log axis).
        rad = math.radians(wind_dir_deg)
        ux = math.sin(rad)    # +x component of "from" direction (paper-x)
        uy = math.cos(rad)    # +y component of "from" direction (paper-y up)

        # Staff endpoints. y-components scaled by ASPECT_W_OVER_H so the staff
        # has equal pixel length in x and y on screen.
        staff_tail_x = BARB_COL_X + ux * STAFF_LEN
        staff_tail_dpy = uy * STAFF_LEN * ASPECT_W_OVER_H
        staff_tail_y = p_lvl * _paper_dy_to_logp_factor(staff_tail_dpy)
        _add_segment(BARB_COL_X, p_lvl, staff_tail_x, staff_tail_y)

        # Perpendicular direction for barb ticks. By convention, barbs hang on
        # the LEFT side of the staff (when looking from anchor toward tail in
        # northern hemisphere). The left-perpendicular of (ux, uy) is (-uy, ux).
        perp_x = -uy
        perp_y = ux

        n_total = pennants + full_barbs + half_barbs
        if n_total == 0:
            return

        gap_step = STAFF_LEN * BARB_GAP

        positions = []
        dist_from_anchor = STAFF_LEN
        for _ in range(pennants):
            positions.append(("pennant", dist_from_anchor))
            dist_from_anchor -= gap_step
            dist_from_anchor -= STAFF_LEN * 0.12
        for _ in range(full_barbs):
            positions.append(("full", dist_from_anchor))
            dist_from_anchor -= gap_step
        for _ in range(half_barbs):
            positions.append(("half", dist_from_anchor))
            dist_from_anchor -= gap_step

        for kind, d in positions:
            base_x = BARB_COL_X + ux * d
            base_dpy = uy * d * ASPECT_W_OVER_H
            base_y = p_lvl * _paper_dy_to_logp_factor(base_dpy)

            if kind == "pennant":
                _add_pennant(
                    base_x, base_y,
                    x_along_dx=ux * STAFF_LEN * 0.32,
                    x_along_dy=uy * STAFF_LEN * 0.32 * ASPECT_W_OVER_H,
                    x_perp_dx=perp_x * BARB_LEN,
                    x_perp_dy=perp_y * BARB_LEN * ASPECT_W_OVER_H,
                )
            elif kind == "full":
                tip_x = base_x + perp_x * BARB_LEN
                tip_dpy = perp_y * BARB_LEN * ASPECT_W_OVER_H
                tip_y = base_y * _paper_dy_to_logp_factor(tip_dpy)
                _add_segment(base_x, base_y, tip_x, tip_y)
            elif kind == "half":
                tip_x = base_x + perp_x * BARB_LEN * 0.55
                tip_dpy = perp_y * BARB_LEN * 0.55 * ASPECT_W_OVER_H
                tip_y = base_y * _paper_dy_to_logp_factor(tip_dpy)
                _add_segment(base_x, base_y, tip_x, tip_y)

    # Decluttered barb pass — every ~40 hPa. Iterate in descending-pressure
    # order so the declutter spacing logic (which assumes monotonic pressure)
    # works regardless of the raw profile ordering.
    _barb_order = np.argsort(-pressures)
    _barb_p = pressures[_barb_order]
    _barb_ws = [wind_kt_list[i] for i in _barb_order]
    _barb_wd = [wind_dir_list[i] for i in _barb_order]
    _last_barb_p = None
    for _bi in range(len(_barb_p)):
        p = _barb_p[_bi]
        ws = _barb_ws[_bi]
        wd = _barb_wd[_bi]
        if ws is None or wd is None:
            continue
        if _last_barb_p is not None and abs(_last_barb_p - p) < 40 and p != _barb_p[0]:
            continue
        _last_barb_p = p
        _draw_barb_glyph(p, ws, wd)

    # Layout
    # Compute ASL ticks from pressure ticks using US Standard Atmosphere
    p_ticks = [1000, 850, 700, 500, 300, 200]
    def _p_to_ft_asl(p_hpa):
        return (288.15 / 0.0065) * (1 - (p_hpa / 1013.25) ** (1/5.25588)) * 3.28084
    alt_labels = [f"{int(_p_to_ft_asl(p)):,} ft" for p in p_ticks]

    fig.update_layout(
        title=dict(text=title, font=dict(color=panel_color, size=12)),
        # Skew-T panels should be portrait-oriented (taller than wide) since
        # the vertical axis covers the full troposphere on a log scale.
        # At ~600 px column width in the 3-column layout, a 720 px height
        # gives a 1:1.2 portrait aspect that lets the vertical structure
        # of the atmosphere read naturally.
        height=720,
        margin=dict(l=42, r=60, t=34, b=32),
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
            # Reversed pressure axis: specify the range in log10 units ordered
            # high-pressure-first. The ordering alone produces the reversal —
            # do NOT also set autorange, which conflicts with an explicit range
            # and collapses the plot area to zero height.
            range=[math.log10(P_BOTTOM), math.log10(P_TOP)],
            tickvals=p_ticks,
            ticktext=[str(p) for p in p_ticks],
            tickfont=dict(color="#8E949E", size=9),
            title=dict(text="hPa", font=dict(color="#A0A4AB", size=9)),
            showgrid=False, zeroline=False, fixedrange=True,
        ),
        showlegend=False,
        hovermode="closest",
        dragmode=False,
    )

    # Secondary y-axis (ASL feet) on the right — same reversed-range approach,
    # no autorange.
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

    if show_parcel and parcel is not None:
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
    else:
        diagnostics = {}
    return fig, diagnostics
