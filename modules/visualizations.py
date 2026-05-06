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
    """Extract a (pressure, temperature, dewpoint) profile for one time index.

    Returns three lists sorted by descending pressure (surface -> top), or
    None if there aren't enough levels to draw anything meaningful.
    """
    pressures = [sfc_pressure]
    temps = [sfc_temp]
    dewpts = [sfc_td]

    for p in _P_LEVELS:
        # Skip levels at or above the surface pressure
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

    if len(pressures) < 3:
        return None
    return pressures, temps, dewpts


def plot_compact_sounding(h, idx, sfc_temp, sfc_td, sfc_pressure,
                          title="", panel_color="#D1D5DB"):
    """Draws a small Skew-T-style sounding for a single timestamp.

    Designed to be rendered as one of three side-by-side panels showing
    past / current / future. Uses a proper log-pressure y-axis and a 45°
    Affine2D skew on the x-axis — the textbook Skew-T construction.

    Args:
        h:             hourly forecast dict from data_ingest
        idx:           time index into h
        sfc_temp:      surface temperature (°C)
        sfc_td:        surface dewpoint (°C)
        sfc_pressure:  surface pressure (hPa)
        title:         label rendered above the panel
        panel_color:   accent color for the title (e.g. amber for current hour)

    Returns:
        matplotlib Figure or None if insufficient data.
    """
    profile = _extract_profile(h, idx, sfc_temp, sfc_td, sfc_pressure)
    if profile is None:
        return None
    pressures, temps, dewpts = profile

    # --- Figure setup, sized to fit one of three side-by-side columns ---
    fig, ax = plt.subplots(figsize=(3.6, 4.6), facecolor='#1B1E23')
    ax.set_facecolor('#1B1E23')

    # --- Skew-T construction ---
    # Standard Skew-T: temperature on x-axis, log-pressure on y-axis (inverted).
    # The skew is applied as a shear so that isotherms slant up and to the right.
    # Convention: ~30°C of horizontal shift per pressure decade gives the
    # canonical 45° isotherm appearance on a square plot.
    P_BOTTOM = 1050.0
    P_TOP = 200.0
    SKEW_C_PER_DECADE = 35.0   # degrees C of x-shift per log10(P) decade

    ax.set_yscale('log')
    ax.set_ylim(P_BOTTOM, P_TOP)            # Inverted: high P at bottom
    ax.set_xlim(-30, 30)

    # Compute skew offset: shift x by -SKEW_C_PER_DECADE * log10(P / P_BOTTOM).
    # At P=P_BOTTOM the shift is zero; at higher altitudes (lower P) the
    # shift is positive (to the right).
    def _skew_x(temp, pressure):
        return temp - SKEW_C_PER_DECADE * np.log10(pressure / P_BOTTOM)

    pressures_arr = np.array(pressures)
    temps_arr = np.array(temps)
    dewpts_arr = np.array(dewpts)

    # --- Background reference: skewed isotherms ---
    # Each isotherm is drawn between P_BOTTOM and P_TOP at its skewed x-position.
    p_grid_dense = np.geomspace(P_BOTTOM, P_TOP, 30)
    for t_iso in range(-100, 50, 10):
        iso_x = _skew_x(t_iso, p_grid_dense)
        ax.plot(iso_x, p_grid_dense,
                color='#3E444E', linewidth=0.5, zorder=1)

    # Highlight the 0 °C freezing isotherm
    iso_x_freeze = _skew_x(0, p_grid_dense)
    ax.plot(iso_x_freeze, p_grid_dense,
            color='#3498DB', linewidth=1.2, linestyle='--', zorder=2)

    # Pressure gridlines
    for p_grid in [1000, 850, 700, 500, 300]:
        ax.axhline(p_grid, color='#2A3038', linewidth=0.5, zorder=1)

    # --- Plot the actual profile (skewed) ---
    sk_temps_profile = _skew_x(temps_arr, pressures_arr)
    sk_dewpts_profile = _skew_x(dewpts_arr, pressures_arr)

    ax.plot(sk_temps_profile, pressures_arr,
            color='#ff4b4b', linewidth=1.8, zorder=4)
    ax.plot(sk_dewpts_profile, pressures_arr,
            color='#2abf2a', linewidth=1.8, zorder=4)

    # --- Cosmetics ---
    from matplotlib.ticker import FixedLocator, FixedFormatter, NullLocator
    ax.tick_params(axis='y', colors='#8E949E', labelsize=7)
    ax.tick_params(axis='x', colors='#8E949E', labelsize=7)

    # Force fixed pressure tick labels (matplotlib's default log formatter
    # produces 2*10^2 style labels which look broken in a small panel).
    _p_ticks = [1000, 850, 700, 500, 300]
    ax.yaxis.set_major_locator(FixedLocator(_p_ticks))
    ax.yaxis.set_major_formatter(FixedFormatter([str(p) for p in _p_ticks]))
    ax.yaxis.set_minor_locator(NullLocator())
    ax.set_xticks([-30, -20, -10, 0, 10, 20, 30])

    for spine_name, spine in ax.spines.items():
        if spine_name in ('top', 'right'):
            spine.set_visible(False)
        else:
            spine.set_color('#3E444E')

    ax.set_ylabel("hPa", color='#A0A4AB', fontsize=8)
    ax.set_xlabel("°C",  color='#A0A4AB', fontsize=8)
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
