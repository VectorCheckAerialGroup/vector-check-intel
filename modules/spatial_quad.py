"""
VECTOR CHECK AERIAL GROUP INC. — Spatial Quad (v5)

One self-contained Leaflet HTML component: four synced panes (radar,
satellite, elevation, MIX precip) with a single Loop control that animates
radar + satellite together. Replaces the fragile per-pane folium/JS approach —
frames are managed by first-party JS inside the component, so animation and
visibility cannot silently fail the way injected folium scripts could.

Pane sync: move/zoom on any pane mirrors to all others (guarded, no loops).
Loop: one button steps radar frames and satellite frames in lockstep.
Canadian stations: selectable; geometry (marker, rings, beam height) renders
identically, imagery falls back to the ECCC 1 km composite because Canada
does not publish public single-site tiles.
"""

from __future__ import annotations

import json
import math

# US NEXRAD (single-site RIDGE tiles available) — id: (name, lat, lon, "us")
# Canadian radars (composite imagery, geometry-only) — "ca"
STATIONS = {
    "KTYX": ("Fort Drum NY", 43.756, -75.680, "us"),
    "KBUF": ("Buffalo NY", 42.949, -78.737, "us"),
    "KBGM": ("Binghamton NY", 42.200, -75.985, "us"),
    "KENX": ("Albany NY", 42.586, -74.064, "us"),
    "KCXX": ("Burlington VT", 44.511, -73.166, "us"),
    "KCBW": ("Caribou ME", 46.039, -67.806, "us"),
    "KGYX": ("Portland ME", 43.891, -70.256, "us"),
    "KDTX": ("Detroit MI", 42.700, -83.472, "us"),
    "KAPX": ("Gaylord MI", 44.906, -84.720, "us"),
    "KMQT": ("Marquette MI", 46.531, -87.548, "us"),
    "KDLH": ("Duluth MN", 46.837, -92.210, "us"),
    "KMVX": ("Grand Forks ND", 47.528, -97.325, "us"),
    "KMBX": ("Minot ND", 48.393, -100.865, "us"),
    "KGGW": ("Glasgow MT", 48.206, -106.625, "us"),
    "KTFX": ("Great Falls MT", 47.460, -111.385, "us"),
    "KOTX": ("Spokane WA", 47.680, -117.627, "us"),
    "CASKR": ("King City ON (Toronto)", 43.964, -79.574, "ca"),
    "CASFT": ("Franktown ON (Ottawa)", 45.043, -76.356, "ca"),
    "CASET": ("Exeter ON", 43.370, -81.383, "ca"),
    "CASBV": ("Blainville QC (Montreal)", 45.707, -73.858, "ca"),
    "CASVD": ("Val-d'Or QC", 48.055, -77.803, "ca"),
    "CASBE": ("Bethune SK (Regina)", 50.571, -105.183, "ca"),
    "CASSM": ("Strathmore AB (Calgary)", 51.206, -113.399, "ca"),
    "CASCV": ("Carvel AB (Edmonton)", 53.560, -114.145, "ca"),
    "CASRA": ("Radisson SK (Saskatoon)", 52.520, -107.443, "ca"),
    "CASDR": ("Dryden ON", 49.858, -92.796, "ca"),
}

SAT_PRODUCTS = {
    "Vis Red (Band 2)": ("GOES-East_ABI_Band2_Red_Visible_1km", 8),
    "IR Clean (Band 13)": ("GOES-East_ABI_Band13_Clean_Infrared", 7),
}


def nearest_stations(lat: float, lon: float, n: int = 10) -> list:
    out = []
    for sid, (nm, slat, slon, cc) in STATIONS.items():
        dlat = math.radians(slat - lat)
        dlon = math.radians(slon - lon)
        a = (math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat))
             * math.cos(math.radians(slat)) * math.sin(dlon / 2) ** 2)
        km = 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))
        out.append((sid, nm, km, cc))
    out.sort(key=lambda x: x[2])
    return out[:n]


def beam_height_ft(dist_km: float, elev_deg: float = 0.5) -> float:
    r_m = dist_km * 1000.0
    re_eff = (4.0 / 3.0) * 6.371e6
    return (r_m * math.sin(math.radians(elev_deg))
            + (r_m ** 2) / (2.0 * re_eff)) * 3.28084


def build_quad_html(lat, lon, zoom, radar_opacity, sat_product,
                    station_id=None, station_product="N0Q",
                    rv_catalog=None,
                    mix_uris=None, mix_times=None,
                    mix_bounds=None, pane_h=380):
    """Returns the full HTML for the synced 2x2 quad."""
    sat_layer, sat_maxz = SAT_PRODUCTS.get(
        sat_product, list(SAT_PRODUCTS.values())[0])
    sta = None
    if station_id and station_id in STATIONS:
        nm, slat, slon, cc = STATIONS[station_id]
        sta = {"id": station_id, "name": nm, "lat": slat, "lon": slon,
               "cc": cc, "product": station_product}
    cfg = {
        "lat": lat, "lon": lon, "zoom": int(zoom),
        "radarOp": radar_opacity,
        "satLayer": sat_layer, "satMaxZ": sat_maxz,
        "rvRadar": (rv_catalog or {}).get("radar", []),
        "rvSat": (rv_catalog or {}).get("sat", []),
        "station": sta,
        "mixUris": mix_uris or [], "mixTimes": mix_times or [],
        "mixBounds": mix_bounds,
        "paneH": pane_h,
    }
    return _TEMPLATE.replace("__CFG__", json.dumps(cfg))


_TEMPLATE = r"""
<!DOCTYPE html><html><head>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>
 body{margin:0;background:transparent;font-family:system-ui,sans-serif;}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;}
 .cell{position:relative;border-radius:10px;overflow:hidden;background:#0b0e12;}
 .map{width:100%;height:380px;}
 .lbl{position:absolute;top:8px;left:10px;z-index:1000;font-size:10px;
      letter-spacing:1px;text-transform:uppercase;color:#cbd5e1;
      background:rgba(10,12,16,0.72);padding:3px 9px;border-radius:5px;
      font-weight:600;pointer-events:none;}
 #loopbtn{position:absolute;top:8px;right:10px;z-index:1100;font-size:11px;
      color:#e5e7eb;background:rgba(10,12,16,0.85);border:1px solid #374151;
      padding:4px 12px;border-radius:6px;cursor:pointer;font-weight:600;}
 #loopbtn.on{color:#4ade80;border-color:#4ade80;}
 .tstamp{position:absolute;bottom:8px;left:10px;z-index:1000;font-size:10px;
      color:#9ca3af;background:rgba(10,12,16,0.72);padding:2px 8px;
      border-radius:4px;pointer-events:none;}
 .leaflet-control-attribution{display:none;}
</style></head><body>
<div class="grid" id="grid"></div>
<script>
const CFG = __CFG__;
const DARK = "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png";
const GIBS = (lyr,t,mz)=>`https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/${lyr}/default/${t}/GoogleMapsCompatible_Level${mz}/{z}/{y}/{x}.png`;
const GEOMET='https://geo.weather.gc.ca/geomet';

function cell(id,label){
  const g=document.getElementById('grid');
  const d=document.createElement('div');d.className='cell';
  d.innerHTML=`<div class="lbl">${label}</div><div id="${id}" class="map"></div><div class="tstamp" id="${id}_t"></div>`;
  g.appendChild(d);return d;
}
function mkmap(id,zoom,maxZoom){
  const m=L.map(id,{zoomControl:false,attributionControl:false,
                    center:[CFG.lat,CFG.lon],zoom:zoom,maxZoom:maxZoom||18});
  L.tileLayer(DARK,{subdomains:'abcd',maxZoom:20}).addTo(m);
  L.circleMarker([CFG.lat,CFG.lon],{radius:7,color:'#E58E26',weight:2,
                 fillOpacity:0.15}).addTo(m);
  return m;
}
const sta=CFG.station;
let radarLabel='RADAR \u00b7 COMPOSITE';
if(sta){radarLabel=`RADAR \u00b7 ${sta.id}`+(sta.cc==='ca'?' \u00b7 ECCC':` \u00b7 ${sta.product} 0.5\u00b0`);}
cell('m1',radarLabel);
cell('m2',`SATELLITE \u00b7 ${CFG.satLayer.includes('Band2')?'VIS RED':'IR CLEAN'}`);
cell('m3','ELEVATION \u00b7 HYPSOMETRIC');
cell('m4', (CFG.mixUris&&CFG.mixUris.length)?'METEOMATICS MIX \u00b7 1H PRECIP':'MODEL PRECIP \u00b7 HRDPS (MIX OFFLINE)');

const m1=mkmap('m1',CFG.zoom);
const m2=mkmap('m2',Math.min(CFG.zoom,CFG.satMaxZ),CFG.satMaxZ);
const m3=mkmap('m3',Math.max(CFG.zoom,9),12);
const m4=mkmap('m4',CFG.zoom);
const maps=[m1,m2,m3,m4];

// ---------- RADAR frames (no static underlay — loop owns the pane) ----------
let radarFrames=[],radarTs=[],satTs=[];
if(sta && sta.cc==='us'){
  // RIDGE tile cache serves only the latest scan (frame 0); historical
  // frame indices are not cached — show latest, loop skips this pane.
  L.tileLayer(
    `https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/ridge::${sta.id}-${sta.product}-0/{z}/{x}/{y}.png`,
    {opacity:CFG.radarOp,maxZoom:12}).addTo(m1);
}else{
  // Composite: RainViewer catalogued frames — every frame is one the
  // service says exists, with its true timestamp. Global composite fuses
  // NEXRAD + Canadian + European radars (RadarScope-style smoothing, scheme 4).
  radarFrames=(CFG.rvRadar||[]).map(f=>L.tileLayer(
    `https://tilecache.rainviewer.com${f.path}/512/{z}/{x}/{y}/4/1_1.png`,
    {opacity:0,maxZoom:12}).addTo(m1));
  radarTs=(CFG.rvRadar||[]).map(f=>f.ts);
}
if(sta){
  L.circleMarker([sta.lat,sta.lon],{radius:5,color:'#4ade80',weight:2,
    fillOpacity:0.9}).addTo(m1).bindTooltip(`${sta.id} ${sta.name}`);
  [60,120,180,230].forEach(rk=>{
    L.circle([sta.lat,sta.lon],{radius:rk*1000,color:'#4ade80',weight:1,
      opacity:0.35,fill:false,dashArray:'4 6'}).addTo(m1);
  });
  m1.setView([(CFG.lat+sta.lat)/2,(CFG.lon+sta.lon)/2],CFG.zoom);
}
// ---------- SATELLITE: catalogued GOES IR frames (loop) ----------
// Vis Red has no public frame catalog -> shown as static latest (GIBS).
// IR loops from the RainViewer satellite catalog with exact timestamps.
let satFrames=[];
const wantVis=CFG.satLayer.includes('Band2');
if(wantVis){
  L.tileLayer(GIBS('GOES-East_ABI_GeoColor','default',7),
    {opacity:1.0,maxZoom:CFG.satMaxZ}).addTo(m2);
}else{
  satFrames=(CFG.rvSat||[]).map(f=>L.tileLayer(
    `https://tilecache.rainviewer.com${f.path}/512/{z}/{x}/{y}/0/0_0.png`,
    {opacity:0,maxZoom:CFG.satMaxZ}).addTo(m2));
  satTs=(CFG.rvSat||[]).map(f=>f.ts);
  if(!satFrames.length){
    L.tileLayer(GIBS('GOES-East_ABI_GeoColor','default',7),
      {opacity:1.0,maxZoom:CFG.satMaxZ}).addTo(m2);
  }
}
// ---------- ELEVATION ----------
L.tileLayer(GIBS('ASTER_GDEM_Color_Shaded_Relief','default',12),{maxZoom:12}).addTo(m3);
L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Elevation/World_Hillshade/MapServer/tile/{z}/{y}/{x}',
  {opacity:0.45,maxZoom:16}).addTo(m3);
// ---------- MIX frames ----------
let mixFrames=[];
if(CFG.mixUris && CFG.mixUris.length && CFG.mixBounds){
  mixFrames=CFG.mixUris.map(u=>L.imageOverlay(u,CFG.mixBounds,{opacity:0}).addTo(m4));
}else{
  L.tileLayer.wms(GEOMET,{layers:'HRDPS.CONTINENTAL_PR',format:'image/png',
    transparent:true,opacity:0.7,version:'1.3.0'}).addTo(m4);
}
// ---------- stopped state: latest everywhere ----------
function show(frames,i,op){frames.forEach((l,j)=>l.setOpacity(j===i?op:0));}
function tlabel(ts){const d=new Date(ts*1000);
  return String(d.getUTCHours()).padStart(2,'0')+String(d.getUTCMinutes()).padStart(2,'0')+'Z';}
function stoppedState(){
  if(radarFrames.length)show(radarFrames,radarFrames.length-1,CFG.radarOp);
  if(satFrames.length)show(satFrames,satFrames.length-1,1.0);
  if(mixFrames.length)show(mixFrames,mixFrames.length-1,0.75);
  if(radarTs.length)document.getElementById('m1_t').textContent=tlabel(radarTs[radarTs.length-1])+' (latest)';
  if(satTs.length)document.getElementById('m2_t').textContent=tlabel(satTs[satTs.length-1])+' (latest)';
}
stoppedState();
if(sta&&sta.cc==='us')document.getElementById('m1_t').textContent='latest scan (no site archive)';
if(mixFrames.length&&CFG.mixTimes)document.getElementById('m4_t').textContent=
  CFG.mixTimes[0].slice(11,16)+'Z\u2192'+CFG.mixTimes[CFG.mixTimes.length-1].slice(11,16)+'Z';
// ---------- ONE loop: radar + satellite + MIX in lockstep ----------
const btn=document.createElement('button');btn.id='loopbtn';btn.textContent='\u25b6 LOOP';
document.querySelector('.cell').appendChild(btn);
let timer=null,idx=0;
btn.onclick=function(){
  if(timer){clearInterval(timer);timer=null;btn.classList.remove('on');
    btn.textContent='\u25b6 LOOP';stoppedState();return;}
  btn.classList.add('on');btn.textContent='\u25a0 LOOPING';idx=0;
  timer=setInterval(function(){
    if(radarFrames.length){const i=idx%radarFrames.length;
      show(radarFrames,i,CFG.radarOp);
      if(radarTs[i])document.getElementById('m1_t').textContent=tlabel(radarTs[i]);}
    if(satFrames.length){const i=idx%satFrames.length;
      show(satFrames,i,1.0);
      if(satTs[i])document.getElementById('m2_t').textContent=tlabel(satTs[i]);}
    if(mixFrames.length)show(mixFrames,idx%mixFrames.length,0.75);
    idx++;
  },800);
};
// ---------- sync ----------
let syncing=false;
maps.forEach(src=>{
  src.on('move zoom',function(){
    if(syncing)return;syncing=true;
    const c=src.getCenter(),z=src.getZoom();
    maps.forEach(dst=>{if(dst!==src){dst.setView(c,Math.min(z,dst.getMaxZoom()),{animate:false});}});
    syncing=false;
  });
});
</script></body></html>
"""
