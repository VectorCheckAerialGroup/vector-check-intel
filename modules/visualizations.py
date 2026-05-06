import matplotlib.pyplot as plt
import numpy as np

# calc_td is the authoritative source in physics.py.
# Importing from there eliminates the duplicate definition and ensures
# any future calibration changes propagate to both the Skew-T and the
# main dashboard automatically.
from modules.physics import calc_td


# Mandatory pressure levels Open-Meteo serves
_P_LEVELS = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300, 250, 200, 150]


def _extract_profile(h, idx, sfc_temp, sfc_td, sfc_pressure):
    """Extract a profile for one time index.

    Returns a dict with arrays for pressure, temperature, dewpoint, height (m),
    wind speed (kt), and wind direction (deg). All arrays are sorted by
    descending pressure (surface -> top). Returns None if there aren't enough
    levels to draw anything meaningful.

    Wind / height fields may contain NaN entries when a model didn't return
    that field at a given level — the caller decides how to render those.
    """
    pressures = [sfc_pressure]
    temps = [sfc_temp]
    dewpts = [sfc_td]
    heights_m = [np.nan]   # surface height filled in below if available
    wind_kt = [np.nan]
    wind_dir = [np.nan]

    for p in _P_LEVELS:
        if p >= sfc_pressure:
            continue
        t_list = h.get(f'temperature_{p}hPa')
        rh_list = h.get(f'relative_humidity_{p}hPa')
        if not t_list or not rh_list or len(t_list) <= idx:
            continue
        t_v = t_list[idx]
        rh_v = rh_list[idx]
        if t_v is None or rh_v is None:
            continue
        try:
            t_c = float(t_v)
            rh_pct = int(rh_v)
        except (TypeError, ValueError):
            continue

        td_c = calc_td(t_c, rh_pct)
        pressures.append(p)
        temps.append(t_c)
        dewpts.append(td_c)

        # Geopotential height (m) — used for ASL altitude axis
        gh_list = h.get(f'geopotential_height_{p}hPa')
        if gh_list and len(gh_list) > idx and gh_list[idx] is not None:
            try:
                heights_m.append(float(gh_list[idx]))
            except (TypeError, ValueError):
                heights_m.append(np.nan)
        else:
            heights_m.append(np.nan)

        # Wind speed (km/h from Open-Meteo) and direction (deg)
        ws_list = h.get(f'wind_speed_{p}hPa')
        if ws_list and len(ws_list) > idx and ws_list[idx] is not None:
            try:
                # convert km/h -> kt
                wind_kt.append(float(ws_list[idx]) * 0.539957)
            except (TypeError, ValueError):
                wind_kt.append(np.nan)
        else:
            wind_kt.append(np.nan)

        wd_list = h.get(f'wind_direction_{p}hPa')
        if wd_list and len(wd_list) > idx and wd_list[idx] is not None:
            try:
                wind_dir.append(float(wd_list[idx]))
            except (TypeError, ValueError):
                wind_dir.append(np.nan)
        else:
            wind_dir.append(np.nan)

    if len(pressures) < 3:
        return None

    return {
        "pressures": pressures,
        "temps": temps,
        "dewpts": dewpts,
        "heights_m": heights_m,
        "wind_kt": wind_kt,
        "wind_dir": wind_dir,
    }


def plot_compact_sounding(h, idx, sfc_temp, sfc_td, sfc_pressure,
                          title="", panel_color="#D1D5DB",
                          sfc_wind_kt=None, sfc_wind_dir=None,
                          sfc_elevation_ft=None):
    """Draws a small Skew-T-style sounding for a single timestamp.

    Designed to be one of three side-by-side panels (past / current / future).
    Uses a proper log-pressure y-axis with skewed isotherms, dry and moist
    adiabats for convective analysis, saturation shading where T-Td <= 2°C,
    pressure on the left and ASL altitude (ft) on the right, and wind barbs
    on the right margin at each pressure level.

    Args:
        h:                    hourly forecast dict
        idx:                  time index into h
        sfc_temp:             surface temperature (°C)
        sfc_td:               surface dewpoint (°C)
        sfc_pressure:         surface pressure (hPa)
        title:                title rendered above the panel
        panel_color:          accent color for the title (e.g. amber for current)
        sfc_wind_kt:          surface wind speed in kt (for surface barb)
        sfc_wind_dir:         surface wind direction in deg
        sfc_elevation_ft:     site elevation in ft (used to anchor ASL altitudes)

    Returns:
        matplotlib Figure or None if insufficient data.
    """
    profile = _extract_profile(h, idx, sfc_temp, sfc_td, sfc_pressure)
    if profile is None:
        return None
    pressures = profile["pressures"]
    temps = profile["temps"]
    dewpts = profile["dewpts"]
    heights_m = list(profile["heights_m"])
    wind_kt = list(profile["wind_kt"])
    wind_dir = list(profile["wind_dir"])

    # Inject the surface wind into the profile if provided
    if sfc_wind_kt is not None:
        wind_kt[0] = sfc_wind_kt
    if sfc_wind_dir is not None:
        wind_dir[0] = sfc_wind_dir

    # Surface height anchor (ft) — used for the right ASL altitude axis
    if sfc_elevation_ft is not None:
        heights_m[0] = sfc_elevation_ft / 3.28084

    # --- Figure setup, slightly wider to accommodate barbs and right axis ---
    fig, ax = plt.subplots(figsize=(4.0, 4.8), facecolor='#1B1E23')
    ax.set_facecolor('#1B1E23')

    # --- Skew-T construction ---
    P_BOTTOM = 1050.0
    P_TOP = 200.0
    SKEW_C_PER_DECADE = 35.0

    ax.set_yscale('log')
    ax.set_ylim(P_BOTTOM, P_TOP)
    ax.set_xlim(-30, 30)

    def _skew_x(temp, pressure):
        return temp - SKEW_C_PER_DECADE * np.log10(pressure / P_BOTTOM)

    pressures_arr = np.array(pressures, dtype=float)
    temps_arr = np.array(temps, dtype=float)
    dewpts_arr = np.array(dewpts, dtype=float)

    # --- Background: skewed isotherms ---
    p_grid_dense = np.geomspace(P_BOTTOM, P_TOP, 50)
    for t_iso in range(-100, 50, 10):
        iso_x = _skew_x(t_iso, p_grid_dense)
        ax.plot(iso_x, p_grid_dense,
                color='#3E444E', linewidth=0.5, zorder=1)

    # 0 °C freezing isotherm highlight
    iso_x_freeze = _skew_x(0, p_grid_dense)
    ax.plot(iso_x_freeze, p_grid_dense,
            color='#3498DB', linewidth=1.2, linestyle='--', zorder=2)

    # --- Dry adiabats (lines of constant potential temperature) ---
    # theta = T * (1000/P)^0.2854 (T in K). Spacing every 10 K.
    theta_K_values = np.arange(260, 410, 10)
    for theta_K in theta_K_values:
        t_K_at_p = theta_K * (p_grid_dense / 1000.0) ** 0.2854
        t_C_at_p = t_K_at_p - 273.15
        adiabat_x = _skew_x(t_C_at_p, p_grid_dense)
        ax.plot(adiabat_x, p_grid_dense,
                color='#8a6d3b', linewidth=0.6, alpha=0.55, zorder=1)

    # --- Moist (saturation pseudo-) adiabats ---
    # Approximation: integrate dT/dz = -g * (1 + Lr/(Rd*T)) / (cp + L^2 * r / (Rv*T^2))
    # where r is the saturation mixing ratio. This is the Bolton 1980 form.
    # For visual reference at small panel size we use a simpler tabulated
    # approximation — slope ~6.5°C/km at 850 hPa decreasing aloft.
    def _moist_adiabat_curve(t_start_C, p_start_hPa, p_grid):
        """Integrate a moist adiabat downward from p_start, t_start."""
        # Walk pressure from start to top of grid in small steps using a
        # simple iterative form. Result is T(°C) at each pressure in p_grid.
        # We compute on a dense grid then interpolate to p_grid.
        Rd = 287.05
        Rv = 461.5
        cp = 1005.0
        L = 2.501e6
        eps = Rd / Rv

        def _es(T_C):
            # Bolton 1980 saturation vapor pressure (hPa)
            return 6.112 * np.exp(17.67 * T_C / (T_C + 243.5))

        # Walk in fine pressure steps
        p_walk = np.geomspace(p_start_hPa, p_grid[-1], 200)
        T = np.zeros_like(p_walk)
        T[0] = t_start_C
        for k in range(1, len(p_walk)):
            T_K = T[k-1] + 273.15
            es = _es(T[k-1])
            r = eps * es / max(0.1, p_walk[k-1] - es)
            # Saturation pseudo-adiabatic dT/dlnP form
            num = Rd * T_K + L * r
            den = cp + (L**2 * r * eps) / (Rd * T_K**2)
            dT_dlnP = num / den
            dlnP = np.log(p_walk[k] / p_walk[k-1])
            T[k] = T[k-1] + dT_dlnP * dlnP

        # Interpolate down onto the requested pressure grid (descending P)
        # np.interp wants ascending xp, so reverse
        return np.interp(p_grid[::-1], p_walk[::-1], T[::-1])[::-1]

    # Draw moist adiabats anchored at 1000 hPa for several starting temps
    for t_start in range(-20, 35, 5):
        try:
            ma_T = _moist_adiabat_curve(t_start, 1000.0, p_grid_dense)
            ax.plot(_skew_x(ma_T, p_grid_dense), p_grid_dense,
                    color='#3a8c63', linewidth=0.6, alpha=0.45,
                    linestyle=':', zorder=1)
        except Exception:
            continue

    # Pressure gridlines
    for p_grid in [1000, 850, 700, 500, 300]:
        ax.axhline(p_grid, color='#2A3038', linewidth=0.5, zorder=1)

    # --- Saturation shading (T - Td <= 2°C) ---
    # Densify the profile so the shading band matches the actual model
    # vertical resolution. fill_betweenx handles non-monotone arrays poorly,
    # so we sort ascending by pressure-decreasing -> ascending log-P, then
    # use the where mask.
    try:
        # Interpolate temp and dewpoint onto a dense pressure grid that lies
        # within the actual profile range. Above the highest sampled level we
        # don't shade (no data).
        p_sat_grid = np.geomspace(max(pressures_arr), min(pressures_arr), 80)
        # np.interp needs ascending xp. Pressures are descending, so flip.
        t_interp = np.interp(p_sat_grid[::-1],
                             pressures_arr[::-1], temps_arr[::-1])[::-1]
        td_interp = np.interp(p_sat_grid[::-1],
                              pressures_arr[::-1], dewpts_arr[::-1])[::-1]
        spread = t_interp - td_interp
        sat_mask = spread <= 2.0

        # In skewed coordinates the band is bounded by skewed-Td (left) and
        # skewed-T (right) at each pressure
        sk_t = _skew_x(t_interp, p_sat_grid)
        sk_td = _skew_x(td_interp, p_sat_grid)
        ax.fill_betweenx(
            p_sat_grid, sk_td, sk_t,
            where=sat_mask,
            facecolor='#cbd5e1', alpha=0.40, zorder=3,
            interpolate=True,
        )
    except Exception:
        pass

    # --- Plot the actual profile (skewed) ---
    sk_temps_profile = _skew_x(temps_arr, pressures_arr)
    sk_dewpts_profile = _skew_x(dewpts_arr, pressures_arr)

    ax.plot(sk_dewpts_profile, pressures_arr,
            color='#2abf2a', linewidth=1.8, zorder=4)
    ax.plot(sk_temps_profile, pressures_arr,
            color='#ff4b4b', linewidth=1.8, zorder=4)

    # --- Cosmetics: pressure axis (left) ---
    from matplotlib.ticker import FixedLocator, FixedFormatter, NullLocator
    ax.tick_params(axis='y', colors='#8E949E', labelsize=7)
    ax.tick_params(axis='x', colors='#8E949E', labelsize=7)

    _p_ticks = [1000, 850, 700, 500, 300]
    ax.yaxis.set_major_locator(FixedLocator(_p_ticks))
    ax.yaxis.set_major_formatter(FixedFormatter([str(p) for p in _p_ticks]))
    ax.yaxis.set_minor_locator(NullLocator())
    ax.set_xticks([-30, -20, -10, 0, 10, 20, 30])

    for spine_name, spine in ax.spines.items():
        if spine_name in ('top',):
            spine.set_visible(False)
        else:
            spine.set_color('#3E444E')

    ax.set_ylabel("hPa", color='#A0A4AB', fontsize=8)
    ax.set_xlabel("°C",  color='#A0A4AB', fontsize=8)

    # --- Right axis: ASL altitude (ft) using a standard atmosphere mapping ---
    # The y-axis is log-pressure. We map standard pressure->altitude using
    # the U.S. Standard Atmosphere hypsometric relation, then offset by the
    # site elevation if known so the surface tick aligns with the launch site.
    def _p_to_alt_ft_std(p_hpa):
        """U.S. Standard Atmosphere pressure-to-altitude (returns ft)."""
        p_hpa = np.asarray(p_hpa, dtype=float)
        # T0 = 288.15 K, lapse = 0.0065 K/m, P0 = 1013.25 hPa
        h_m = (288.15 / 0.0065) * (1.0 - (p_hpa / 1013.25) ** (1.0 / 5.25588))
        return h_m * 3.28084

    ax_alt = ax.twinx()
    ax_alt.set_yscale('log')
    ax_alt.set_ylim(P_BOTTOM, P_TOP)
    # Ticks at the same pressure levels but labeled with ASL altitude
    _alt_ticks_p = [1000, 850, 700, 500, 300]
    _alt_ticks_ft = _p_to_alt_ft_std(np.array(_alt_ticks_p))
    ax_alt.yaxis.set_major_locator(FixedLocator(_alt_ticks_p))
    ax_alt.yaxis.set_major_formatter(
        FixedFormatter([f"{int(round(ft, -2)):,}" for ft in _alt_ticks_ft])
    )
    ax_alt.yaxis.set_minor_locator(NullLocator())
    ax_alt.tick_params(axis='y', colors='#8E949E', labelsize=7,
                       direction='out', pad=2)
    ax_alt.spines['top'].set_visible(False)
    ax_alt.spines['right'].set_color('#3E444E')
    ax_alt.set_ylabel("ft ASL", color='#A0A4AB', fontsize=8, rotation=270, labelpad=10)

    # --- Wind barbs on the right side of the plot ---
    # Plot inside the data area at the right edge. The barbs sit between the
    # rightmost isotherm and the right spine so they're clear of the
    # temperature/dewpoint traces.
    barb_x_axes = 0.91   # axes fraction
    barb_lvls_p = []
    barb_us = []
    barb_vs = []
    for p_v, ws_v, wd_v in zip(pressures, wind_kt, wind_dir):
        if np.isnan(ws_v) or np.isnan(wd_v):
            continue
        if p_v not in (1000, 925, 850, 700, 500, 300) and p_v != pressures[0]:
            continue
        rad = np.deg2rad(wd_v)
        barb_lvls_p.append(p_v)
        barb_us.append(-ws_v * np.sin(rad))
        barb_vs.append(-ws_v * np.cos(rad))

    if barb_lvls_p:
        # Use a blended transform: x in axes coords, y in data coords
        from matplotlib.transforms import blended_transform_factory
        trans = blended_transform_factory(ax.transAxes, ax.transData)
        ax.barbs(
            np.full(len(barb_lvls_p), barb_x_axes),
            barb_lvls_p,
            barb_us, barb_vs,
            length=5,
            barbcolor='#94A3B8',
            flagcolor='#94A3B8',
            linewidth=0.7,
            transform=trans,
            zorder=5,
        )

    if title:
        ax.set_title(title, color=panel_color, fontsize=10,
                     fontweight='600', pad=6)

    fig.tight_layout()
    return fig


def plot_convective_profile(h, idx, t_temp, td, w_spd, sfc_dir, sfc_elevation):
    # 1. Parse High-Res Profile
    p_levels = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300, 250, 200, 150]

    alts  = [sfc_elevation]
    temps = [t_temp]
    dewpts = [td]

    for p in p_levels:
        gh_list  = h.get(f'geopotential_height_{p}hPa')
        t_list   = h.get(f'temperature_{p}hPa')
        rh_list  = h.get(f'relative_humidity_{p}hPa')

        if gh_list and t_list and rh_list and len(gh_list) > idx:
            if gh_list[idx] is not None and t_list[idx] is not None and rh_list[idx] is not None:
                h_ft  = float(gh_list[idx]) * 3.28084
                t_c   = float(t_list[idx])
                rh_val = int(rh_list[idx])
                td_c  = calc_td(t_c, rh_val)

                # Ensure array strictly ascends for plotting
                if h_ft > alts[-1]:
                    alts.append(h_ft)
                    temps.append(t_c)
                    dewpts.append(td_c)

    if len(alts) < 3:
        return None

    # 2. Setup Plot
    fig, ax = plt.subplots(figsize=(7.2, 9.0), facecolor='#1B1E23')
    ax.set_facecolor('#1B1E23')

    # Custom Skew Transformation (+1 degree shift per 1000 ft)
    skew_factor = 1.0 / 1000.0

    sk_temps  = [t + (a * skew_factor) for t, a in zip(temps, alts)]
    sk_dewpts = [d + (a * skew_factor) for d, a in zip(dewpts, alts)]

    # 3. Draw Isotherms (Vertical temperature lines)
    for t_line in range(-80, 50, 10):
        iso_x = [t_line + (a * skew_factor) for a in alts]
        ax.plot(iso_x, alts, color='#3E444E', linestyle='-', linewidth=0.5, zorder=1)

    # Highlight the Freezing Line (0°C)
    freeze_x = [0 + (a * skew_factor) for a in alts]
    ax.plot(freeze_x, alts, color='#3498DB', linestyle='--', linewidth=1.5, zorder=2)

    # 4. Draw Dry Adiabats (Cooling 3°C per 1000 ft; skew corrects to -2 slope visually)
    for base_t in range(-40, 90, 10):
        adiabat_x = [base_t - (a * 2.0 / 1000.0) for a in alts]
        ax.plot(adiabat_x, alts, color='#5C6370', linestyle='--', linewidth=0.8, zorder=1)

    # 5. Draw Pseudo-Moist Adiabats (Cooling ~1.5°C per 1000 ft; skew corrects to -0.5 slope visually)
    for base_t in range(-20, 60, 10):
        m_adiabat_x = [base_t - (a * 0.5 / 1000.0) for a in alts]
        ax.plot(m_adiabat_x, alts, color='#4A505A', linestyle=':', linewidth=1.2, zorder=1)

    # 6. Smooth the arrays for high-fidelity shading
    fine_alts  = np.linspace(min(alts), max(alts), 1000)
    fine_temps  = np.interp(fine_alts, alts, temps)
    fine_dewpts = np.interp(fine_alts, alts, dewpts)

    fine_sk_temps  = fine_temps  + (fine_alts * skew_factor)
    fine_sk_dewpts = fine_dewpts + (fine_alts * skew_factor)
    spread = fine_temps - fine_dewpts

    # 7. Execute the ≤ 2°C Cloud Saturation Shading
    ax.fill_betweenx(
        fine_alts, fine_sk_dewpts, fine_sk_temps,
        where=(spread <= 2.0),
        facecolor='#E0E0E0', alpha=0.35, zorder=3, label='Cloud Saturation'
    )

    # 8. Plot Main Thermal Profiles
    ax.plot(sk_dewpts, alts, color='#2abf2a', linewidth=2.5, zorder=4, label='Dewpoint')
    ax.plot(sk_temps,  alts, color='#ff4b4b', linewidth=2.5, zorder=4, label='Temperature')

    # Formatting — y-axis capped at 30,000 ft (maximises resolution in core UAS envelope)
    ax.set_ylim(0, 30000)
    ax.set_xlim(-40, 60)

    ax.tick_params(axis='y', colors='#8E949E')
    ax.tick_params(axis='x', colors='#8E949E')
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['bottom'].set_color('#3E444E')
    ax.spines['left'].set_color('#3E444E')
    ax.set_ylabel("Altitude (ft ASL)", color='#A0A4AB')
    ax.set_xlabel("Temperature (°C)",  color='#A0A4AB')

    return fig
