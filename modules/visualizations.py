import matplotlib.pyplot as plt
import numpy as np

def plot_convective_profile(h, idx, t, td, w_spd, sfc_dir, sfc_h):
    p_levs_plot = [1000, 950, 925, 900, 850, 800, 700, 600, 500, 400]
    
    valid_p = []
    t_plot_valid, td_plot_valid, h_plot_valid, ws_plot_valid, wd_plot_valid = [], [], [], [], []
    for p in p_levs_plot:
        t_val = h.get(f'temperature_{p}hPa')[idx]
        td_val = h.get(f'dewpoint_{p}hPa')[idx]
        h_val = h.get(f'geopotential_height_{p}hPa')[idx]
        ws_val = h.get(f'wind_speed_{p}hPa')[idx]
        wd_val = h.get(f'wind_direction_{p}hPa')[idx]
        
        if t_val is not None and td_val is not None and h_val is not None:
            valid_p.append(p)
            t_plot_valid.append(t_val)
            td_plot_valid.append(td_val)
            h_plot_valid.append(h_val * 3.28084)
            ws_plot_valid.append(ws_val)
            wd_plot_valid.append(wd_val)

    if len(valid_p) < 3:
        return None

    if sfc_h < h_plot_valid[0]:
        h_plot_valid.insert(0, sfc_h)
        t_plot_valid.insert(0, t)
        td_plot_valid.insert(0, td)
        ws_plot_valid.insert(0, w_spd)
        wd_plot_valid.insert(0, sfc_dir)

    fig, ax = plt.subplots(figsize=(10, 8))
    fig.patch.set_facecolor('#1B1E23')
    ax.set_facecolor('#1B1E23')

    ax.plot(t_plot_valid, h_plot_valid, color='#e74c3c', linewidth=3, label='Env Temp (°C)', zorder=5)
    ax.plot(td_plot_valid, h_plot_valid, color='#3498db', linewidth=3, label='Dewpoint (°C)', zorder=5)
    ax.fill_betweenx(h_plot_valid, t_plot_valid, td_plot_valid, where=(np.array(t_plot_valid) - np.array(td_plot_valid) <= 3.0), color='#8E949E', alpha=0.3, label='Saturated / Cloud')

    max_h = max(h_plot_valid)
    h_grid = np.linspace(0, max_h, 50)
    
    for t_base in range(-40, 60, 10):
        ax.plot(t_base - (3.0 * (h_grid / 1000.0)), h_grid, color='#e67e22', linestyle='dashed', alpha=0.2, linewidth=1)
        t_moist = np.zeros_like(h_grid)
        t_moist[0] = t_base
        for i in range(1, len(h_grid)):
            salr = 1.2 + 1.8 * (1.0 / (1.0 + np.exp((t_moist[i-1] + 15)/10)))
            t_moist[i] = t_moist[i-1] - (salr * ((h_grid[i] - h_grid[i-1]) / 1000.0))
        ax.plot(t_moist, h_grid, color='#27ae60', linestyle='dotted', alpha=0.3, linewidth=1)

    lcl_h = sfc_h + 400 * (t - td)
    p_t_plot = []
    for hv in h_plot_valid:
        if hv <= lcl_h:
            pt = t - 3.0 * ((hv - sfc_h) / 1000.0)
        else:
            t_lcl = t - 3.0 * ((lcl_h - sfc_h) / 1000.0)
            salr = 1.2 + 1.8 * (1.0 / (1.0 + np.exp((t_lcl + 15)/10)))
            pt = t_lcl - salr * ((hv - lcl_h) / 1000.0)
        p_t_plot.append(pt)
        
    ax.plot(p_t_plot, h_plot_valid, color='#f1c40f', linewidth=2, linestyle='-.', label='Surface Parcel Trajectory', zorder=6)
    ax.fill_betweenx(h_plot_valid, t_plot_valid, p_t_plot, where=(np.array(p_t_plot) > np.array(t_plot_valid)), color='#ff4b4b', alpha=0.4, label='CAPE (Instability Risk)')
    ax.axvline(0, color='#B976AC', linestyle='--', linewidth=2, alpha=0.8, label='0°C Isotherm')

    ax.set_ylabel('Altitude (ft MSL)', color='#A0A4AB', fontsize=12, fontweight='bold')
    ax.set_xlabel('Temperature (°C)', color='#A0A4AB', fontsize=12, fontweight='bold')
    ax.tick_params(axis='both', colors='#D1D5DB', labelsize=10)
    ax.grid(color='#2D3139', linestyle='-', linewidth=1)
    for spine in ax.spines.values():
        spine.set_color('#3E444E')
        
    min_x = min(td_plot_valid) - 5
    max_x = max(t_plot_valid) + 15
    ax.set_xlim(min_x, max_x)
    ax.set_ylim(min(h_plot_valid), max(h_plot_valid))

    u_p, v_p = [], []
    for ws_v, wd_v in zip(ws_plot_valid, wd_plot_valid):
        if ws_v is not None and wd_v is not None:
            u_p.append(-ws_v * np.sin(np.radians(wd_v)))
            v_p.append(-ws_v * np.cos(np.radians(wd_v)))
        else:
            u_p.append(0)
            v_p.append(0)
            
    ax.barbs([max_x - 5] * len(h_plot_valid), h_plot_valid, u_p, v_p, color='#D1D5DB', length=6)
    ax.legend(loc='upper left', facecolor='#1B1E23', edgecolor='#3E444E', labelcolor='#D1D5DB', prop={'size': 9})
    plt.figtext(0.14, 0.88, f"Surface Elev: {int(sfc_h)} ft", color='#A0A4AB', fontsize=10, backgroundcolor='#1B1E23')

    return fig
