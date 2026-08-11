"""Interactive HTML map: all candidates, click -> SVG horizon profile.

No drive-time/access modelling here — geometry only. Single self-contained
HTML file (Leaflet from CDN, horizon arrays embedded as JSON, profiles drawn
client-side as SVG). Each marker links to Google Maps directions.
"""
from __future__ import annotations

import csv
import json
import pathlib

import numpy as np

from .dem import DEMO_DIR
from .horizon import HorizonGrid, default_ranges, horizon_profile
from .solar import azimuth_sector, compute_eclipse_geometry
from .vegetation import Veg

OUTPUT_DIR = pathlib.Path(__file__).parent.parent / "output"

AZ0_PLOT, AZ1_PLOT = 150.0, 360.0


def build_webmap(name: str, verbose: bool = True):
    meta = json.loads((OUTPUT_DIR / f"{name}_meta.json").read_text())
    with open(OUTPUT_DIR / f"{name}_candidates.csv") as f:
        rows = list(csv.DictReader(f))

    mr = meta.get("max_range_km", 55.0)
    dem_path = DEMO_DIR / f"{name}_{meta['radius_km']:.0f}km_h{mr:.0f}km.tif"
    from .dem import Dem
    dem = Dem(dem_path)
    geo = compute_eclipse_geometry(meta["lat"], meta["lon"], meta["date"])
    az0, az1 = azimuth_sector(geo, 12.0)

    bbox = (dem.lon0, dem.lat0 - dem.h * dem.res, dem.lon0 + dem.w * dem.res, dem.lat0)
    veg = Veg.for_bbox(bbox, verbose=verbose)
    az_axis = np.arange(AZ0_PLOT, AZ1_PLOT + 0.5, 1.0)
    grid = HorizonGrid.build(dem, az_axis, default_ranges(mr), meta["lat"])

    sites = []
    for i, r in enumerate(rows, 1):
        lon, lat = float(r["lon"]), float(r["lat"])
        bare = horizon_profile(dem, lon, lat, grid)
        vegp = horizon_profile(dem, lon, lat, grid, veg=veg)
        sites.append(dict(
            rank=i, lon=lon, lat=lat, elev=round(float(r["elev"]), 1),
            min_clear=round(float(r["min_clearance"]), 2),
            veg_risk=round(float(r["veg_risk"]), 2),
            tc_obs=round(float(r["tc_obs"]), 1),
            score=round(float(r["score"]), 2),
            bare=[round(float(x), 2) for x in bare],
            veg=[round(float(x), 2) for x in vegp],
        ))
        if verbose:
            print(f"[webmap] {i}/{len(rows)} {lat:.4f},{lon:.4f}")

    track = []
    for j, s in enumerate(geo.useful_samples):
        track.append([round(s.az_deg, 2), round(s.alt_deg, 2),
                      s.t_utc.strftime("%H:%M") if j % 10 == 0 else ""])

    data = dict(
        name=name, date=meta["date"],
        origin=[meta["lat"], meta["lon"]],
        sector=[round(az0, 1), round(az1, 1)],
        az_axis=[round(float(a), 1) for a in az_axis],
        track=track, sites=sites,
        mag=round(geo.max_magnitude, 3),
        t_max=geo.contacts["maximum"].strftime("%H:%M"),
    )
    html = (_HTML.replace("__DATA__", json.dumps(data))
                 .replace("__NAME__", name))
    out = OUTPUT_DIR / f"{name}_webmap.html"
    out.write_text(html)
    print(f"wrote {out} ({out.stat().st_size/1e6:.2f} MB, {len(sites)} sites)")
    return out


_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Eclipse sites — __NAME__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
 html,body{margin:0;height:100%;font:13px/1.45 system-ui,sans-serif}
 #map{position:absolute;top:0;left:0;right:0;bottom:0}
 #panel{position:absolute;top:0;right:0;bottom:0;width:min(480px,92vw);
   background:#fff;box-shadow:-2px 0 8px rgba(0,0,0,.25);overflow-y:auto;
   transform:translateX(100%);transition:transform .18s;padding:14px 16px;box-sizing:border-box}
 #panel.open{transform:none}
 #panel h3{margin:.2em 0 .4em}
 #panel .stats{display:grid;grid-template-columns:auto auto;gap:2px 14px;margin:.5em 0}
 #panel .stats b{font-weight:600}
 a.btn{display:inline-block;margin:4px 6px 4px 0;padding:5px 10px;border-radius:6px;
   background:#1a73e8;color:#fff;text-decoration:none;font-weight:600}
 a.btn.alt{background:#5f6368}
 #close{float:right;cursor:pointer;font-size:16px;color:#888}
 .muted{color:#777}
 svg{max-width:100%}
 #legend{position:absolute;bottom:12px;left:12px;z-index:900;background:#fffc;
   padding:6px 10px;border-radius:6px;font-size:12px}
</style></head><body>
<div id="map"></div>
<div id="legend"></div>
<div id="panel"><span id="close" onclick="closePanel()">✕</span><div id="pbody"></div></div>
<script>
const D = __DATA__;
document.title = "Eclipse sites — " + D.name;

const map = L.map("map").setView(D.origin, 9);
const osm = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
  {maxZoom: 19, attribution: "© OpenStreetMap"});
const topo = L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
  {maxZoom: 17, attribution: "© OpenStreetMap, SRTM | © OpenTopoMap"});
topo.addTo(map);
L.control.layers({"OpenTopoMap": topo, "OSM": osm}).addTo(map);

// sun-sector wedge from origin
(function(){
  const [la0, lo0] = D.origin, R = 60000, pts = [[la0, lo0]];
  for (let a = D.sector[0]; a <= D.sector[1]; a += 2) {
    const dlat = R * Math.cos(a*Math.PI/180) / 111320;
    const dlon = R * Math.sin(a*Math.PI/180) / (111320 * Math.cos(la0*Math.PI/180));
    pts.push([la0 + dlat, lo0 + dlon]);
  }
  L.polygon(pts, {color: "#c8a400", weight: 1, fillColor: "#ffd75e",
    fillOpacity: .15, interactive: false}).addTo(map);
})();

L.marker(D.origin, {interactive: false}).addTo(map)
  .bindTooltip("origin", {permanent: true, direction: "right", className: "muted"});

const scores = D.sites.map(s => s.score);
const smin = Math.min(...scores), smax = Math.max(...scores);
function color(s){ const t = (s - smin) / (smax - smin || 1);
  return `hsl(${Math.round(140*t)}, 75%, ${Math.round(45 - 10*t)}%)`; }

D.sites.forEach(s => {
  L.circleMarker([s.lat, s.lon], {radius: 7, color: "#222", weight: 1,
    fillColor: color(s.score), fillOpacity: .9})
    .bindTooltip(`#${s.rank}  ${s.elev} m  clr ${s.min_clear}°`, {direction: "top"})
    .on("click", () => showPanel(s)).addTo(map);
});

document.getElementById("legend").innerHTML =
  `<b>${D.name}</b> — ${D.date}, max ${D.t_max} UTC (mag ${D.mag})<br>` +
  `marker colour: geometry score ${smin.toFixed(1)} → ${smax.toFixed(1)} (green = best); ` +
  `gold wedge = sun sector; click a marker`;

function showPanel(s){
  const gmap = `https://www.google.com/maps/dir/?api=1&destination=${s.lat},${s.lon}&travelmode=driving`;
  const osmL = `https://www.openstreetmap.org/?mlat=${s.lat}&mlon=${s.lon}#map=15/${s.lat}/${s.lon}`;
  document.getElementById("pbody").innerHTML = `
    <h3>#${s.rank} — ${s.elev} m</h3>
    <div class="stats">
      <span>Coordinates</span><b>${s.lat.toFixed(5)}, ${s.lon.toFixed(5)}</b>
      <span>Min sun clearance</span><b>${s.min_clear}°</b>
      <span>Vegetation risk</span><b>${s.veg_risk}°</b>
      <span>Local tree cover</span><b>${s.tc_obs}%</b>
      <span>Geometry score</span><b>${s.score}</b>
    </div>
    <a class="btn" target="_blank" href="${gmap}">Directions (Google Maps)</a>
    <a class="btn alt" target="_blank" href="${osmL}">OpenStreetMap</a>
    <div id="chart"></div>
    <p class="muted">Brown fill: bare-terrain horizon. Green line: horizon incl.
    modelled canopy. Orange: sun track during eclipse (labels UTC). Gold band:
    azimuth sector the sun occupies.</p>`;
  drawChart(s);
  document.getElementById("panel").classList.add("open");
}
function closePanel(){ document.getElementById("panel").classList.remove("open"); }

function drawChart(s){
  const W = 448, H = 250, mL = 34, mB = 24, mT = 8, mR = 6;
  const az = D.az_axis;
  const ymax = Math.max(25, ...s.bare, ...s.veg) + 3;
  const X = a => mL + (a - az[0]) / (az[az.length-1] - az[0]) * (W - mL - mR);
  const Y = v => mT + (ymax - v) / (ymax + 2) * (H - mT - mB);
  let el = "";
  // sun sector band
  el += `<rect x="${X(D.sector[0])}" y="${mT}" width="${X(D.sector[1])-X(D.sector[0])}"
         height="${H-mT-mB}" fill="#ffd75e" opacity=".18"/>`;
  // terrain fill
  let p = `M ${X(az[0])} ${Y(-2)}`;
  az.forEach((a,i)=> p += ` L ${X(a)} ${Y(s.bare[i])}`);
  p += ` L ${X(az[az.length-1])} ${Y(-2)} Z`;
  el += `<path d="${p}" fill="saddlebrown" opacity=".55"/>`;
  // veg line
  let pv = ""; az.forEach((a,i)=> pv += `${i?"L":"M"} ${X(a)} ${Y(s.veg[i])} `);
  el += `<path d="${pv}" fill="none" stroke="forestgreen" stroke-width="1.4"/>`;
  // sun track
  let ps = ""; D.track.forEach(t => ps += `${ps?"L":"M"} ${X(t[0])} ${Y(t[1])} `);
  el += `<path d="${ps}" fill="none" stroke="tab:orange" stroke-width="1.6"/>`;
  D.track.forEach(t => { if (t[2]) el +=
    `<circle cx="${X(t[0])}" cy="${Y(t[1])}" r="2.2" fill="tab:orange"/>
     <text x="${X(t[0])}" y="${Y(t[1])-5}" font-size="8" text-anchor="middle"
       fill="#666">${t[2]}</text>`; });
  // axes
  el += `<line x1="${mL}" y1="${Y(0)}" x2="${W-mR}" y2="${Y(0)}" stroke="#000" stroke-width=".7"/>`;
  for (let a = 180; a <= 360; a += 30)
    el += `<text x="${X(a)}" y="${H-8}" font-size="9" text-anchor="middle">${a}°</text>`;
  for (let v = 0; v <= ymax; v += 10)
    el += `<text x="${mL-4}" y="${Y(v)+3}" font-size="9" text-anchor="end">${v}°</text>`;
  el += `<text x="${(W)/2}" y="${H-0.5}" font-size="9" text-anchor="middle" fill="#666">azimuth</text>`;
  document.getElementById("chart").innerHTML =
    `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">${el}</svg>`;
}
</script></body></html>
"""
