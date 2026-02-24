import matplotlib.pyplot as plt
import numpy as np

def plot_convective_profile(h, idx, sfc_t, sfc_td, sfc_wspd, sfc_wdir, sfc_h):
    """Generates an ultra-compact vertical atmospheric sounding (thumbnail scale)."""
    p_levels = [1000, 950, 925, 900, 850, 800, 700, 600]
    
    alts = [sfc_h]
    temps = [sfc_t]
    dps = [sfc_td]
    
    for p in p_levels:
        h_val = h.get(f'geopotential_height_{p}hPa')
        t_val = h.get(f'temperature_{p}hPa')
        rh_val = h.get(f'relative_humidity_{p}hPa')
        
        if h_val and t_val and rh_val:
            h_v = h_val[idx]
            t_v = t_val[idx]
            rh_v = rh_val[idx]
            
            if h_v is not None and t_v is not None and rh_v is not None:
                alt_ft = h_v * 3.28084
                if alt_ft > sfc_h: 
                    td_v = t_v - ((100 - rh_v) / 5.0)
                    alts.append(alt_ft)
                    temps.append(t_v)
                    dps.append(td_v)
                    
    if len(alts) < 3: return None
    
    top_alt = max(alts) if alts else 15000

    # --- RENDER THE MATPLOTLIB GRAPH ---
    # RESIZED: Shrunk by exactly 50% linearly. New ultra-compact dimensions.
    fig, ax = plt.subplots(figsize=(1.5, 2.0))
    fig.patch.set_facecolor('#1B1E23')
    ax.set_facecolor('#1B1E23')
    
    ref_alts = np.linspace(sfc_h, top_alt, 50)
    
    for start_t in range(-40, 50, 10):
        dalr_temps = [start_t - ((a - sfc_h) / 1000.0) * 3.0 for a in ref_alts]
        ax.plot(dalr_temps, ref_alts, color='green', linewidth=0.3, linestyle='-', alpha=0.15)

    for start_t in range(-20, 40, 10):
         malr_temps = [start_t - ((a - sfc_h) / 1000.0) * 1.5 for a in ref_alts]
         ax.plot(malr_temps, ref_alts, color='cyan', linewidth=0.3, linestyle='--', alpha=0.15)

    # Plot lines
    ax.plot(temps, alts, color='red', linewidth=1.0, label='T')
    ax.plot(dps, alts, color='#2196F3', linewidth=1.0, label='Td')
    
    parcel_temps = [sfc_t]
    for i in range(1, len(alts)):
        alt_diff_ft = alts[i] - alts[i-1]
        drop = (alt_diff_ft / 1000.0) * 1.5 
        parcel_temps.append(parcel_temps[i-1] - drop)
    ax.plot(parcel_temps, alts, color='yellow', linestyle='--', linewidth=1.0, label='Parcel')
    
    ax.fill_betweenx(alts, temps, parcel_temps, where=np.array(parcel_temps) > np.array(temps), facecolor='red', alpha=0.3)
    ax.fill_betweenx(alts, dps, temps, where=(np.array(temps) - np.array(dps)) <= 2.0, facecolor='grey', alpha=0.5)
    
    # --- ULTRA-COMPACT GRAPH FORMATTING ---
    ax.axvline(x=0, color='white', linestyle=':', linewidth=0.5)
    
    # Fonts downscaled to micro-sizes to fit the tiny frame
    ax.set_title("Tactical Convective Profile", color='#D1D5DB', fontsize=5, pad=4)
    ax.set_xlabel("Temp (°C)", color='#8E949E', fontsize=4)
    ax.set_ylabel("Alt (ft)", color='#8E949E', fontsize=4)
    ax.tick_params(colors='#8E949E', labelsize=4, length=2, pad=1)
    
    ax.grid(color='#3E444E', linestyle='--', linewidth=0.3)
    for spine in ax.spines.values(): 
        spine.set_color('#3E444E')
        spine.set_linewidth(0.5)
        
    ax.set_ylim(bottom=sfc_h, top=top_alt)
    ax.set_xlim(min(min(temps), min(dps)) - 5, max(max(temps), sfc_t) + 5)
    
    ax.legend(facecolor='#1B1E23', edgecolor='#3E444E', labelcolor='#D1D5DB', loc='upper center', bbox_to_anchor=(0.5, -0.15), ncol=3, fontsize=3, handlelength=1.0, columnspacing=0.5)
    
    plt.tight_layout(pad=0.2)
    return fig
