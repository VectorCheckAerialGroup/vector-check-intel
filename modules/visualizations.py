import matplotlib.pyplot as plt
import numpy as np
import math

def calc_td(t, rh):
    if rh <= 0: return t
    a, b = 17.625, 243.04
    alpha = math.log(rh / 100.0) + ((a * t) / (b + t))
    return (b * alpha) / (a - alpha)

def plot_convective_profile(h, idx, t_temp, td, w_spd, sfc_dir, sfc_elevation):
    # 1. Parse High-Res Profile
    p_levels = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300, 250, 200, 150]
    
    alts = [sfc_elevation]
    temps = [t_temp]
    dewpts = [td]
    
    for p in p_levels:
        gh_list = h.get(f'geopotential_height_{p}hPa')
        t_list = h.get(f'temperature_{p}hPa')
        rh_list = h.get(f'relative_humidity_{p}hPa')
        
        if gh_list and t_list and rh_list and len(gh_list) > idx:
            if gh_list[idx] is not None and t_list[idx] is not None and rh_list[idx] is not None:
                h_ft = float(gh_list[idx]) * 3.28084
                t_c = float(t_list[idx])
                rh_val = int(rh_list[idx])
                td_c = calc_td(t_c, rh_val)
                
                # Ensure array strictly ascends for plotting
                if h_ft > alts[-1]: 
                    alts.append(h_ft)
                    temps.append(t_c)
                    dewpts.append(td_c)
    
    if len(alts) < 3: 
        return None
    
    # 2. Setup Plot
    fig, ax = plt.subplots(figsize=(8, 10), facecolor='#1B1E23')
    ax.set_facecolor('#1B1E23')
    
    # Custom Skew Transformation (+1 degree shift per 1000 ft)
    skew_factor = 1.0 / 1000.0
    
    sk_temps = [t + (a * skew_factor) for t, a in zip(temps, alts)]
    sk_dewpts = [d + (a * skew_factor) for d, a in zip(dewpts, alts)]
    
    # 3. Draw Isotherms (Vertical temperature lines)
    for t_line in range(-80, 50, 10):
        iso_x = [t_line + (a * skew_factor) for a in alts]
        ax.plot(iso_x, alts, color='#3E444E', linestyle='-', linewidth=0.5, zorder=1)
        
    # Highlight the Freezing Line (0C)
    freeze_x = [0 + (a * skew_factor) for a in alts]
    ax.plot(freeze_x, alts, color='#3498DB', linestyle='--', linewidth=1.5, zorder=2)

    # 4. Draw Dry Adiabats (Cooling 3C per 1000ft. Skew corrects it to -2 slope visually)
    for base_t in range(-40, 90, 10):
        adiabat_x = [base_t - (a * 2.0 / 1000.0) for a in alts]
        ax.plot(adiabat_x, alts, color='#5C6370', linestyle='--', linewidth=0.8, zorder=1)
        
    # 5. Draw Pseudo-Moist Adiabats (Cooling ~1.5C per 1000ft. Skew corrects to -0.5 slope visually)
    for base_t in range(-20, 60, 10):
        m_adiabat_x = [base_t - (a * 0.5 / 1000.0) for a in alts]
        ax.plot(m_adiabat_x, alts, color='#4A505A', linestyle=':', linewidth=1.2, zorder=1)
    
    # 6. Smooth the arrays for high-fidelity shading
    fine_alts = np.linspace(min(alts), max(alts), 1000)
    fine_temps = np.interp(fine_alts, alts, temps)
    fine_dewpts = np.interp(fine_alts, alts, dewpts)
    
    fine_sk_temps = fine_temps + (fine_alts * skew_factor)
    fine_sk_dewpts = fine_dewpts + (fine_alts * skew_factor)
    spread = fine_temps - fine_dewpts
    
    # 7. Execute the <= 2 Degree Cloud Saturation Shading
    ax.fill_betweenx(fine_alts, fine_sk_dewpts, fine_sk_temps, where=(spread <= 2.0), facecolor='#E0E0E0', alpha=0.35, zorder=3, label='Cloud Saturation')
    
    # 8. Plot Main Thermal Profiles
    ax.plot(sk_dewpts, alts, color='#2abf2a', linewidth=2.5, zorder=4, label='Dewpoint')
    ax.plot(sk_temps, alts, color='#ff4b4b', linewidth=2.5, zorder=4, label='Temperature')
    
    # Formating
    ax.set_ylim(0, 35000)
    ax.set_xlim(-40, 60)
    ax.set_title("Tactical Skew-T (Dry/Moist Adiabats & Saturation Shading)", color="#D1D5DB", pad=20, fontsize=12, fontweight='bold')
    ax.tick_params(axis='y', colors='#8E949E')
    ax.tick_params(axis='x', colors='#8E949E')
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['bottom'].set_color('#3E444E')
    ax.spines['left'].set_color('#3E444E')
    ax.set_ylabel("Altitude (ft ASL)", color='#A0A4AB')
    ax.set_xlabel("Temperature (°C)", color='#A0A4AB')
    
    return fig
