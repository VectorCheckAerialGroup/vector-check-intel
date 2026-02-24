import matplotlib.pyplot as plt
import numpy as np

def plot_convective_profile(h, idx, sfc_t, sfc_td, sfc_wspd, sfc_wdir, sfc_h):
    """Generates a vertical atmospheric sounding (Convective Profile) using Matplotlib."""
    p_levels = [1000, 950, 925, 900, 850, 800, 700, 600]
    
    # Initialize arrays with the surface data anchor points
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
                # Only plot layers that exist above the physical ground level
                if alt_ft > sfc_h: 
                    # Calculate dewpoint natively to bypass API limitations
                    td_v = t_v - ((100 - rh_v) / 5.0)
                    alts.append(alt_ft)
                    temps.append(t_v)
                    dps.append(td_v)
                    
    if len(alts) < 3:
        return None # Failsafe: Not enough data points to plot a meaningful graph
        
    # --- RENDER THE MATPLOTLIB GRAPH ---
    fig, ax = plt.subplots(figsize=(6, 8))
    fig.patch.set_facecolor('#1B1E23')
    ax.set_facecolor('#1B1E23')
    
    # Plot Environmental Temperature & Dewpoint
    ax.plot(temps, alts, color='red', linewidth=2, label='Env Temp (T)')
    ax.plot(dps, alts, color='#2196F3', linewidth=2, label='Env Dewpoint (Td)')
    
    # Calculate Theoretical Parcel Ascent (Simplified SALR approximation: 1.5C drop per 1000ft)
    parcel_temps = [sfc_t]
    for i in range(1, len(alts)):
        alt_diff_ft = alts[i] - alts[i-1]
        drop = (alt_diff_ft / 1000.0) * 1.5 
        parcel_temps.append(parcel_temps[i-1] - drop)
        
    ax.plot(parcel_temps, alts, color='yellow', linestyle='--', linewidth=2, label='Parcel Trajectory (SALR)')
    
    # Shade CAPE (Convective Available Potential Energy)
    ax.fill_betweenx(alts, temps, parcel_temps, where=np.array(parcel_temps) > np.array(temps), facecolor='red', alpha=0.3, label='CAPE (Instability)')
    
    # Shade Saturation / Cloud Layers (T and Td within 2 degrees)
    ax.fill_betweenx(alts, dps, temps, where=(np.array(temps) - np.array(dps)) <= 2.0, facecolor='grey', alpha=0.5, label='Cloud/Saturation')
    
    # --- GRAPH FORMATTING & CSS INJECTION ---
    ax.axvline(x=0, color='white', linestyle=':', linewidth=1) # 0°C Freezing Isotherm
    ax.set_title("Tactical Convective Profile", color='#D1D5DB', fontsize=14, pad=15)
    ax.set_xlabel("Temperature (°C)", color='#8E949E')
    ax.set_ylabel("Altitude (ft ASL)", color='#8E949E')
    ax.tick_params(colors='#8E949E')
    
    ax.grid(color='#3E444E', linestyle='--', linewidth=0.5)
    for spine in ax.spines.values():
        spine.set_color('#3E444E')
        
    ax.set_ylim(bottom=sfc_h, top=max(alts) if alts else 15000)
    ax.legend(facecolor='#1B1E23', edgecolor='#3E444E', labelcolor='#D1D5DB', loc='upper right', fontsize=8)
    
    plt.tight_layout()
    return fig
