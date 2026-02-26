import matplotlib.pyplot as plt
import math

def calc_td(t, rh):
    """Calculates dewpoint from temperature and relative humidity."""
    if rh <= 0: return t
    a = 17.625
    b = 243.04
    alpha = math.log(rh / 100.0) + ((a * t) / (b + t))
    return (b * alpha) / (a - alpha)

def plot_convective_profile(h, idx, t_temp, td, w_spd, sfc_dir, sfc_h):
    """
    Renders a high-resolution tactical Skew-T profile extending to ~35,000 ft.
    Uses universally supported WMO mandatory pressure levels to prevent chart collapse.
    """
    altitudes = [sfc_h]
    temps = [t_temp]
    dewpoints = [td]
    
    # 500hPa (~18,000ft), 250hPa (~34,000ft) - Supported by all regional/global models
    for p in [1000, 925, 850, 700, 500, 250]:
        gh_list = h.get(f'geopotential_height_{p}hPa')
        t_list = h.get(f'temperature_{p}hPa')
        rh_list = h.get(f'relative_humidity_{p}hPa')
        
        if gh_list and t_list and rh_list and len(gh_list) > idx:
            gh_val = gh_list[idx]
            t_val = t_list[idx]
            rh_val = rh_list[idx]
            
            if gh_val is not None and t_val is not None and rh_val is not None:
                alt_ft = float(gh_val) * 3.28084
                if alt_ft > altitudes[-1]: 
                    altitudes.append(alt_ft)
                    temps.append(float(t_val))
                    dewpoints.append(calc_td(float(t_val), int(rh_val)))
                    
    if len(altitudes) < 2:
        return None

    # --- SKEW-T MATHEMATICAL TRANSFORMATION ---
    skew_factor = 0.004  
    min_y = max(0, sfc_h - 200)
    
    # Hardcoded upper boundary to guarantee the chart displays the full troposphere
    # even if intermediate pressure levels drop out of the API payload.
    max_alt = max(35000, int(altitudes[-1]) + 1500)
    
    y_bg = list(range(0, max_alt + 1000, 500))
    
    # --- STYLE & SCALING ---
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(6, 4.5), dpi=200) 
    fig.patch.set_facecolor('#1B1E23')
    ax.set_facecolor('#1B1E23')
    
    # 1. DRAW BACKGROUND THERMODYNAMIC GRID
    for t_iso in range(-80, 80, 10):
        x_iso = [t_iso + skew_factor * a for a in y_bg]
        ax.plot(x_iso, y_bg, color='#3E444E', linewidth=0.8, zorder=1)

    for t_dry_sfc in range(-40, 120, 10):
        x_dry = [(t_dry_sfc - 0.003 * a) + skew_factor * a for a in y_bg]
        ax.plot(x_dry, y_bg, color='#A87C4F', linestyle='--', linewidth=0.6, alpha=0.7, zorder=1)
        
    for t_moist_sfc in range(-40, 80, 10):
        x_moist = [(t_moist_sfc - 0.0015 * a) + skew_factor * a for a in y_bg]
        ax.plot(x_moist, y_bg, color='#4A6B53', linestyle=':', linewidth=0.8, alpha=0.7, zorder=1)

    for a_line in range(0, max_alt + 1000, 5000):
        ax.axhline(a_line, color='#3E444E', linewidth=0.8, zorder=1)

    # 2. TRANSFORM & PLOT MISSION DATA
    skewed_temps = [t + skew_factor * a for t, a in zip(temps, altitudes)]
    skewed_dews = [d + skew_factor * a for d, a in zip(dewpoints, altitudes)]
    
    fill_cond = [(t - d) <= 3.0 for t, d in zip(temps, dewpoints)]
    ax.fill_betweenx(altitudes, skewed_dews, skewed_temps, where=fill_cond, color='#ffffff', alpha=0.2, zorder=2, label='Cloud / Moisture')

    ax.plot(skewed_temps, altitudes, color='#ff4b4b', label='Temp', linewidth=2, marker='o', markersize=3, zorder=3)
    ax.plot(skewed_dews, altitudes, color='#4b7Bff', label='Dewpt', linewidth=2, marker='o', markersize=3, zorder=3)
    
    # 3. DYNAMIC SCALING & CUSTOM X-AXIS
    min_x = min(skewed_dews) - 10
    max_x = max(skewed_temps) + 15
    ax.set_xlim(min_x, max_x)
    ax.set_ylim(min_y, max_alt)
    
    ax.set_xticks([])
    for t_iso in range(-80, 80, 10):
        bot_x = t_iso + skew_factor * min_y
        if min_x <= bot_x <= max_x:
            ax.text(bot_x, min_y - (max_alt - min_y)*0.03, f"{t_iso}°", color='#A0A4AB', ha='center', va='top', fontsize=7)

    ax.set_ylabel('Altitude (ft ASL)', color='#A0A4AB', fontsize=8)
    ax.tick_params(axis='y', colors='#A0A4AB', labelsize=7)
    
    for spine in ax.spines.values():
        spine.set_color('#3E444E')
        
    ax.legend(loc='upper right', facecolor='#1B1E23', edgecolor='#3E444E', fontsize=7)
    
    plt.tight_layout()
    return fig
