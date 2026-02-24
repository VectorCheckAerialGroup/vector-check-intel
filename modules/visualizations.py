import matplotlib.pyplot as plt
import numpy as np

def plot_convective_profile(h, idx, sfc_t, sfc_td, sfc_wspd, sfc_wdir, sfc_h):
    """Generates a vertical atmospheric sounding with adiabatic reference lines."""
    p_levels = [1000, 950, 925, 900, 850, 800, 700, 600]
    
    # Initialize arrays with surface data anchor points
    alts = [sfc_h]
    temps = [sfc_t]
    dps = [sfc_td]
    
    # Dynamically extract and calculate the upper-air profile
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
    
    # Determine graph top ceiling
    top_alt = max(alts) if alts else 15000

    # --- RENDER THE MATPLOTLIB GRAPH ---
    # RESIZED: Reduced figsize by ~30% from (6, 8) to (4.2, 5.6)
    fig, ax = plt.subplots(figsize=(4.2, 5.6))
    fig.patch.set_facecolor('#1B1E23')
    ax.set_facecolor('#1B1E23')
    
    # --- PLOT ADIABATIC REFERENCE LINES (BACKGROUND) ---
    ref_alts = np.linspace(sfc_h, top_alt, 50)
    
    # Dry Adiabats (DALR ~3°C/1000ft) - Solid Green, Faint
    for start_t in range(-40, 50, 10):
        dalr_temps = [start_t - ((a - sfc_h) / 1000.0) * 3.0 for a in ref_alts]
        ax.plot(dalr_temps, ref_alts, color='green', linewidth=0.5, linestyle='-', alpha=0.15)

    # Moist Adiabats (Linear Approx ~1.5°C/1000ft) - Dashed Cyan, Faint
    for start_t in range(-20, 40, 10):
         malr_temps = [start_t - ((a - sfc_h) / 1000.0) * 1.5 for a in ref_alts]
         ax.plot(malr_temps, ref_alts, color='cyan', linewidth=0.5, linestyle='--', alpha=0.15)

    # --- PLOT MAIN DATA ---
    # Environmental Temperature & Dewpoint
    ax.plot(temps, alts, color='red', linewidth=2, label='Env Temp (T)')
    ax.plot(dps, alts, color='#2196F3', linewidth=2, label='Env Dewpoint (Td)')
    
    # Theoretical Parcel Ascent (Using SALR approximation)
    parcel_temps = [sfc_t]
    for i in range(1, len(alts)):
        alt_diff_ft = alts[i] - alts[i-1]
        drop = (alt_diff_ft / 1000.0) * 1.5 
        parcel_temps.append(parcel_temps[i-1] - drop)
    ax.plot(parcel_temps, alts, color='yellow', linestyle='--', linewidth=2, label='Parcel Path')
    
    # Shade CAPE & Clouds
    ax.fill_betweenx(alts, temps, parcel_temps, where=np.array(parcel_temps) > np.array(temps), facecolor='red', alpha=0.3)
    ax.fill_betweenx(alts, dps, temps, where=(np.array(temps) - np.array(dps)) <= 2.0, facecolor='grey', alpha=0.5)
    
    # --- GRAPH FORMATTING ---
    ax.axvline(x=0, color='white', linestyle=':', linewidth=1)
    ax.set_title("Tactical Convective Profile", color='#D1D5DB', fontsize=12, pad=10)
    ax.set_xlabel("Temperature (°C)", color='#8E949E', fontsize=9)
    ax.set_ylabel("Altitude (ft ASL)", color='#8E949E', fontsize=9)
    ax.tick_params(colors='#8E949E', labelsize=8)
    
    ax.grid(color='#3E444E', linestyle='--', linewidth=0.5)
    for spine in ax.spines.values(): spine.set_color('#3E444E')
        
    ax.set_ylim(bottom=sfc_h, top=top_alt)
    # Adjust x-axis to ensure all lines are visible
    ax.set_xlim(min(min(temps), min(dps)) - 5, max(max(temps), sfc_t) + 5)
    
    # Legend placed outside to save space on smaller chart
    ax.legend(facecolor='#1B1E23', edgecolor='#3E444E', labelcolor='#D1D5DB', loc='upper center', bbox_to_anchor=(0.5, -0.15), ncol=3, fontsize=7)
    
    plt.tight_layout()
    return fig
