"""Interactive HTML map with in-browser horizon computation.

Click anywhere: the horizon (bare + canopy-corrected) is computed client-side
from a bundled two-ring raster (30 m terrain+canopy within ~15 km of the
origin, ~90 m beyond, out to 55 km). Single self-contained HTML file:
Leaflet from CDN, raster embedded as base64 gzip.

Buildings/hedges are NOT modelled yet (stage 2).
"""
from __future__ import annotations

import base64
import csv
import gzip
import json

import pathlib
import struct

import numpy as np

from .dem import DEMO_DIR, Dem
from .horizon import _meters_per_deg
from .solar import azimuth_sector, compute_eclipse_geometry
from .vegetation import Veg
from .buildings import B_RES_M, extract_vectors, rasterize

OUTPUT_DIR = pathlib.Path(__file__).parent.parent / "output"
INNER_RADIUS_KM = 15.0


# ---------------------------------------------------------------- raster ----
def _inner_window(dem: Dem, lon_o: float, lat_o: float):
    m_lon, m_lat = _meters_per_deg(lat_o)
    r_in = int(INNER_RADIUS_KM * 1000 / (dem.res_lat * m_lat))
    c_in = int(INNER_RADIUS_KM * 1000 / (dem.res * m_lon))
    oc, orow = dem.lonlat_to_px(np.asarray(lon_o), np.asarray(lat_o))
    oc, orow = int(round(float(oc))), int(round(float(orow)))
    r0, r1 = max(0, orow - r_in), min(dem.h, orow + r_in)
    c0, c1 = max(0, oc - c_in), min(dem.w, oc + c_in)
    return dict(r0=r0, r1=r1, c0=c0, c1=c1, m_lon=m_lon, m_lat=m_lat,
                lon0=dem.lon0 + c0 * dem.res, lat0=dem.lat0 - r0 * dem.res_lat)


def _raster_bytes(dem: Dem, veg: Veg, origin: tuple[float, float],
                  b_in: np.ndarray) -> bytes:
    """Two-ring terrain+canopy raster + 10 m building/hedge layer, LE.

    layout: magic 'EFR2'
      outer/inner/bld grid hdrs: lon0,lat0,res_lon,res_lat (f64), w,h (u32)
      t_out int16, t_in int16, b_in u8, c_out u8, c_in u8
    canopy resampled (bilinear) onto the dem grid; heights in metres.
    """
    lon_o, lat_o = origin
    # canopy resampled onto dem native grid (bilinear, chunked)
    lons = dem.lon0 + np.arange(dem.w) * dem.res
    lats = dem.lat0 - np.arange(dem.h) * dem.res_lat
    cc = np.clip(np.floor((lons - veg.lon0) / veg.res).astype(np.int64), 0, veg.w - 2)
    fc = np.clip((lons - veg.lon0) / veg.res - cc, 0, 1).astype(np.float32)
    rr = np.clip(np.floor((veg.lat0 - lats) / veg.res).astype(np.int64), 0, veg.h - 2)
    fr = np.clip((veg.lat0 - lats) / veg.res - rr, 0, 1).astype(np.float32)
    H = veg.h_can
    can30 = np.empty((dem.h, dem.w), np.uint8)
    for i in range(0, dem.h, 1024):
        j = min(i + 1024, dem.h)
        v = (H[np.ix_(rr[i:j], cc)] * (1 - fc) * (1 - fr[i:j, None])
             + H[np.ix_(rr[i:j], cc + 1)] * fc * (1 - fr[i:j, None])
             + H[np.ix_(rr[i:j] + 1, cc)] * (1 - fc) * fr[i:j, None]
             + H[np.ix_(rr[i:j] + 1, cc + 1)] * fc * fr[i:j, None])
        can30[i:j] = np.clip(np.round(v), 0, 255).astype(np.uint8)

    t_out = dem.a[::3, ::3]
    H, W = can30.shape
    c_out = can30[: H // 3 * 3, : W // 3 * 3].reshape(H // 3, 3, W // 3, 3).max(axis=(1, 3))

    win = _inner_window(dem, lon_o, lat_o)
    t_in = dem.a[win["r0"]:win["r1"], win["c0"]:win["c1"]]
    cn_in = can30[win["r0"]:win["r1"], win["c0"]:win["c1"]]

    def hdr(lon0, lat0, rl, rt, w, h):
        return struct.pack("<4d2I", lon0, lat0, rl, rt, w, h)

    out = bytearray()
    out += b"EFR2"
    out += hdr(dem.lon0, dem.lat0, dem.res, dem.res_lat, t_out.shape[1], t_out.shape[0])
    out += hdr(win["lon0"], win["lat0"], dem.res, dem.res_lat,
               t_in.shape[1], t_in.shape[0])
    out += hdr(win["lon0"], win["lat0"], B_RES_M / win["m_lon"], B_RES_M / win["m_lat"],
               b_in.shape[1], b_in.shape[0])
    for a in (t_out, t_in):
        out += np.ascontiguousarray(a.astype("<i2")).tobytes()
    for a in (b_in, c_out, cn_in):
        out += np.ascontiguousarray(a).tobytes()
    return bytes(out)


# ---------------------------------------------------------------- build ----
def build_webmap(name: str, verbose: bool = True):
    meta = json.loads((OUTPUT_DIR / f"{name}_meta.json").read_text())
    with open(OUTPUT_DIR / f"{name}_candidates.csv") as f:
        rows = list(csv.DictReader(f))

    mr = meta.get("max_range_km", 55.0)
    dem = Dem(DEMO_DIR / f"{name}_{meta['radius_km']:.0f}km_h{mr:.0f}km.tif")
    geo = compute_eclipse_geometry(meta["lat"], meta["lon"], meta["date"])
    az0, az1 = azimuth_sector(geo, 12.0)

    bbox = (dem.lon0, dem.lat0 - dem.h * dem.res_lat,
            dem.lon0 + dem.w * dem.res, dem.lat0)
    veg = Veg.for_bbox(bbox, verbose=verbose)

    win = _inner_window(dem, meta["lon"], meta["lat"])
    bbox_in = (win["lon0"], win["lat0"] - (win["r1"] - win["r0"]) * dem.res_lat,
               win["lon0"] + (win["c1"] - win["c0"]) * dem.res, win["lat0"])
    vec = extract_vectors(name, bbox_in, verbose=verbose)
    b_w = int((win["c1"] - win["c0"]) * dem.res / (B_RES_M / win["m_lon"]))
    b_h = int((win["r1"] - win["r0"]) * dem.res_lat / (B_RES_M / win["m_lat"]))
    b_in = rasterize(vec, win["lon0"], win["lat0"], b_w, b_h,
                     win["m_lon"], win["m_lat"])
    if verbose:
        print(f"[webmap] building layer {b_w}x{b_h}, max {b_in.max()} m")
    raw = _raster_bytes(dem, veg, (meta["lon"], meta["lat"]), b_in)
    b64 = base64.b64encode(gzip.compress(raw, 6)).decode()
    if verbose:
        print(f"[webmap] raster {len(raw)/1e6:.1f} MB raw -> {len(b64)/1e6:.1f} MB base64")

    sites = [dict(
        rank=i, lon=float(r["lon"]), lat=float(r["lat"]),
        elev=round(float(r["elev"]), 1), min_clear=round(float(r["min_clearance"]), 2),
        veg_risk=round(float(r["veg_risk"]), 2), score=round(float(r["score"]), 2),
    ) for i, r in enumerate(rows, 1)]

    track = []
    for j, s in enumerate(geo.useful_samples):
        track.append([round(s.az_deg, 2), round(s.alt_deg, 2),
                      s.t_utc.strftime("%H:%M") if j % 10 == 0 else ""])

    data = dict(
        name=name, date=meta["date"],
        origin=[meta["lat"], meta["lon"]],
        sector=[round(az0, 1), round(az1, 1)],
        track=track, sites=sites,
        mag=round(geo.max_magnitude, 3),
        t_max=geo.contacts["maximum"].strftime("%H:%M"),
        inner_km=INNER_RADIUS_KM,
        t0m=geo.useful_samples[0].t_utc.hour * 60
            + geo.useful_samples[0].t_utc.minute,
    )
    html = (_HTML.replace("__DATA__", json.dumps(data))
                 .replace("__NAME__", name)
                 .replace("__RASTER_B64__", b64))
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
 #map{position:absolute;top:0;left:0;right:0;bottom:0;
   cursor:url("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' width='30' height='30'><circle cx='15' cy='15' r='9' fill='none' stroke='%23d32f2f' stroke-width='3'/><line x1='15' y1='0' x2='15' y2='10' stroke='%23d32f2f' stroke-width='3'/><line x1='15' y1='20' x2='15' y2='30' stroke='%23d32f2f' stroke-width='3'/><line x1='0' y1='15' x2='10' y2='15' stroke='%23d32f2f' stroke-width='3'/><line x1='20' y1='15' x2='30' y2='15' stroke='%23d32f2f' stroke-width='3'/></svg>") 15 15, crosshair}
 #panel{position:absolute;top:0;right:0;bottom:0;width:min(480px,92vw);
   background:#fff;box-shadow:-2px 0 8px rgba(0,0,0,.25);overflow-y:auto;
   transform:translateX(100%);transition:transform .18s;padding:14px 16px;
   box-sizing:border-box;z-index:1000}
 #panel.open{transform:none}
 #panel h3{margin:.2em 0 .4em}
 #panel .stats{display:grid;grid-template-columns:auto auto;gap:2px 14px;margin:.5em 0}
 a.btn{display:inline-block;margin:4px 6px 4px 0;padding:5px 10px;border-radius:6px;
   background:#1a73e8;color:#fff;text-decoration:none;font-weight:600}
 a.btn.alt{background:#5f6368}
 #close{float:right;cursor:pointer;font-size:16px;color:#888}
 #verdict{font-size:15px;font-weight:700;padding:8px 10px;border-radius:8px;margin:.4em 0}
 #verdict.ok{background:#e8f5e9;color:#1b5e20}
 #verdict.bad{background:#ffebee;color:#b71c1c}
 .muted{color:#777}
 svg{max-width:100%}
 #legend{position:absolute;bottom:12px;left:12px;z-index:900;background:#fffc;
   padding:6px 10px;border-radius:6px;font-size:12px;max-width:46%}
 #busy{position:absolute;top:12px;left:50%;transform:translateX(-50%);z-index:1100;
   background:#333e;color:#fff;padding:6px 14px;border-radius:20px;display:none}
</style></head><body>
<div id="map"></div>
<div id="legend"></div>
<div id="busy">computing horizon…</div>
<div id="panel"><span id="close" onclick="closePanel()">✕</span><div id="pbody"></div></div>
<script>
const D = __DATA__;
const RASTER_B64 = "__RASTER_B64__";
document.title = "Eclipse sites — " + D.name;

const map = L.map("map").setView(D.origin, 9);
const osm = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
  {maxZoom: 19, attribution: "© OpenStreetMap"});
const topo = L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
  {maxZoom: 17, attribution: "© OpenStreetMap, SRTM | © OpenTopoMap"});
topo.addTo(map);
L.control.layers({"OpenTopoMap": topo, "OSM": osm}).addTo(map);

(function(){  // sun-sector wedge
  const [la0, lo0] = D.origin, R = 60000, pts = [[la0, lo0]];
  for (let a = D.sector[0]; a <= D.sector[1]; a += 2) {
    const dlat = R * Math.cos(a*Math.PI/180) / 111320;
    const dlon = R * Math.sin(a*Math.PI/180) / (111320 * Math.cos(la0*Math.PI/180));
    pts.push([la0 + dlat, lo0 + dlon]);
  }
  L.polygon(pts, {color: "#c8a400", weight: 1, fillColor: "#ffd75e",
    fillOpacity: .15, interactive: false}).addTo(map);
})();

L.marker(D.origin).addTo(map)
  .bindTooltip("origin (calc centre) — click for horizon", {direction: "right"})
  .on("click", () => showPoint(D.origin[0], D.origin[1], "Origin (calc centre)"));

const scores = D.sites.map(s => s.score);
const smin = Math.min(...scores), smax = Math.max(...scores);
function color(s){ const t = (s - smin) / (smax - smin || 1);
  return `hsl(${Math.round(140*t)}, 75%, ${Math.round(45 - 10*t)}%)`; }
D.sites.forEach(s => {
  L.circleMarker([s.lat, s.lon], {radius: 7, color: "#222", weight: 1,
    fillColor: color(s.score), fillOpacity: .9})
    .bindTooltip(`#${s.rank}  ${s.elev} m  clr ${s.min_clear}°`, {direction: "top"})
    .on("click", () => showPoint(s.lat, s.lon, `#${s.rank} (ranked site)`)).addTo(map);
});

document.getElementById("legend").innerHTML =
  `<b>${D.name}</b> — eclipse ${D.date}, biggest around ${bstLabel(D.t_max)} UK time.<br>` +
  `Click anywhere to check the view from that spot (drag to move, scroll to zoom). ` +
  `Coloured dots = the best ranked spots (green = best). ` +
  `<a href="https://bacon.boutique" target="_blank">by Seb</a>`;

// ---------------------------------------------------------- raster decode --
let G = null;  // parsed raster grids
async function loadRaster(){
  const bin = Uint8Array.from(atob(RASTER_B64), c => c.charCodeAt(0));
  const stream = new Blob([bin]).stream()
    .pipeThrough(new DecompressionStream("gzip"));
  const buf = await new Response(stream).arrayBuffer();
  const dv = new DataView(buf);
  let off = 4;
  function grid(){
    const g = {lon0: dv.getFloat64(off, true), lat0: dv.getFloat64(off+8, true),
      resLon: dv.getFloat64(off+16, true), resLat: dv.getFloat64(off+24, true),
      w: dv.getUint32(off+32, true), h: dv.getUint32(off+36, true)};
    off += 40; g.n = g.w * g.h; return g;
  }
  G = {outer: grid(), inner: grid(), bld: grid(), buf};
  const u8 = new Uint8Array(buf);
  let p = off;
  G.outer.t = new Int16Array(buf, p, G.outer.n); p += 2*G.outer.n;
  G.inner.t = new Int16Array(buf, p, G.inner.n); p += 2*G.inner.n;
  G.bld.b = u8.subarray(p, p + G.bld.n); p += G.bld.n;
  G.outer.c = u8.subarray(p, p + G.outer.n); p += G.outer.n;
  G.inner.c = u8.subarray(p, p + G.inner.n);
}
loadRaster().then(() => {
  if (location.hash.startsWith("#test")) {
    let [la, lo] = D.origin;
    const m = location.hash.match(/#test=([-\d.]+),([-\d.]+)/);
    if (m) { la = +m[1]; lo = +m[2]; }
    const h = horizon(lo, la);
    let minC = 1e9;
    for (const s of D.track) {
      const i = Math.round(s[0]) - AZS[0];
      if (i >= 0 && i < AZS.length && s[1] - h.veg[i] < minC) minC = s[1] - h.veg[i];
    }
    document.title = "T=" + JSON.stringify([+h.elev.toFixed(1),
      +h.bare[118].toFixed(2), +h.veg[118].toFixed(2), +minC.toFixed(2)]);
    showPoint(la, lo, "Test");
  }
});

function bilinear(g, lon, lat, kind){
  const col = (lon - g.lon0) / g.resLon, row = (g.lat0 - lat) / g.resLat;
  if (col < 0 || row < 0 || col > g.w - 1 || row > g.h - 1) return 0;
  const c0 = Math.min(Math.floor(col), g.w - 2), r0 = Math.min(Math.floor(row), g.h - 2);
  const fc = col - c0, fr = row - r0, i = r0 * g.w + c0, A = g[kind];
  return A[i]*(1-fc)*(1-fr) + A[i+1]*fc*(1-fr) + A[i+g.w]*(1-fc)*fr + A[i+g.w+1]*fc*fr;
}
function sample(lon, lat, d, kind){
  if (kind === "b") {
    const g = G.bld;
    const col = (lon - g.lon0) / g.resLon, row = (g.lat0 - lat) / g.resLat;
    if (col < 0 || row < 0 || col > g.w - 1 || row > g.h - 1) return 0;
    return bilinear(g, lon, lat, "b");
  }
  // fine ring near the observer, coarse beyond
  if (d <= D.inner_km * 1000) {
    const g = G.inner;
    const col = (lon - g.lon0) / g.resLon, row = (g.lat0 - lat) / g.resLat;
    if (col >= 0 && row >= 0 && col <= g.w - 1 && row <= g.h - 1)
      return bilinear(g, lon, lat, kind);
  }
  return bilinear(G.outer, lon, lat, kind);
}

// range bins + curvature drop (mirror eclipse_finder/horizon.py)
const RANGES = (() => { const r = [];
  for (let d = 30; d < 8000; d += 30) r.push(d);
  for (let d = 8000; d < 24000; d += 60) r.push(d);
  for (let d = 24000; d <= 55000; d += 120) r.push(d);
  return r; })();
const DROP = RANGES.map(d => d*d / (2*6371000) * (1 - 0.13));
const AZS = (() => { const a = []; for (let x = 150; x <= 360; x += 1) a.push(x); return a; })();

function horizon(lon, lat){
  const mLon = 111412.84 * Math.cos(lat*Math.PI/180) - 93.5 * Math.cos(3*lat*Math.PI/180);
  const mLat = 111132.954 - 559.822 * Math.cos(2*lat*Math.PI/180)
             + 1.175 * Math.cos(4*lat*Math.PI/180);
  const hObs = sample(lon, lat, 0, "t");
  const bare = new Array(AZS.length), vegh = new Array(AZS.length);
  for (let ai = 0; ai < AZS.length; ai++) {
    const az = AZS[ai] * Math.PI / 180, sa = Math.sin(az), ca = Math.cos(az);
    let mb = -90, mv = -90;
    for (let ri = 0; ri < RANGES.length; ri++) {
      const d = RANGES[ri];
      const slon = lon + d * sa / mLon, slat = lat + d * ca / mLat;
      const t = sample(slon, slat, d, "t");
      const a1 = Math.atan2(t - hObs - DROP[ri], d) * 57.29578;
      if (a1 > mb) mb = a1;
      const o = Math.max(0.5 * sample(slon, slat, d, "c"),
                         sample(slon, slat, d, "b"));
      const a2 = Math.atan2(t + o - hObs - DROP[ri], d) * 57.29578;
      if (a2 > mv) mv = a2;
    }
    bare[ai] = mb; vegh[ai] = mv;
  }
  return {bare, veg: vegh, elev: hObs};
}

// ------------------------------------------------------------------ panel --
// minutes-after-midnight -> UK clock time (August = BST = UTC+1)
function fmtTime(m){
  m = Math.round(m + 60) % 1440;
  return String(Math.floor(m/60)).padStart(2,"0") + ":" + String(m%60).padStart(2,"0");
}
function bstLabel(hhmm){
  if (!hhmm) return "";
  const [h, m] = hhmm.split(":").map(Number);
  return fmtTime(h*60 + m);
}
function showPoint(lat, lon, label){
  if (!G) return;
  clickMark = clickMark
    ? clickMark.setLatLng([lat, lon])
    : L.circleMarker([lat, lon], {radius: 9, color: "#fff", weight: 3,
        fillColor: "#d32f2f", fillOpacity: 1, interactive: false}).addTo(map);
  document.getElementById("busy").style.display = "block";
  setTimeout(() => {
    const t0 = performance.now();
    const h = horizon(lon, lat);
    const ms = Math.round(performance.now() - t0);
    const b0 = sample(lon, lat, 0, "b");
    // clearances over the sun track
    let minC = 1e9, maxVeg = -90, maxBare = -90, bFirst = -1, bLast = -1;
    D.track.forEach((s, idx) => {
      if (s[0] < D.sector[0] || s[0] > D.sector[1]) return;
      const i = Math.round(s[0]) - AZS[0];
      const c = s[1] - h.veg[i];
      if (c < minC) minC = c;
      if (h.veg[i] > maxVeg) maxVeg = h.veg[i];
      if (h.bare[i] > maxBare) maxBare = h.bare[i];
      if (h.veg[i] > s[1]) { if (bFirst < 0) bFirst = idx; bLast = idx; }
    });
    const verdict = bFirst < 0
      ? "✔ Good view: the sun stays clear of hills, trees and buildings for the whole eclipse."
      : `✘ Blocked: the sun disappears behind hills, trees or buildings from about ` +
        `${fmtTime(D.t0m + bFirst)} until ${fmtTime(D.t0m + bLast)} (UK time).`;
    const gmap = `https://www.google.com/maps/dir/?api=1&destination=${lat},${lon}&travelmode=driving`;
    const osmL = `https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}#map=15/${lat}/${lon}`;
    document.getElementById("pbody").innerHTML = `
      <h3>${label} — ${h.elev.toFixed(0)} m above sea level</h3>
      <div id="verdict" class="${bFirst < 0 ? "ok" : "bad"}">${verdict}</div>
      <div class="stats">
        <span>Where</span><b>${lat.toFixed(5)}, ${lon.toFixed(5)}</b>
        <span>Tallest blockage towards the sun</span><b>${maxVeg.toFixed(1)}° above a flat horizon</b>
        <span>Worst gap between sun and blockage</span><b>${minC.toFixed(1)}° (below 0 = blocked)</b>
        <span>Calculated in</span><b>${ms} ms</b>
      </div>
      ${b0 > 0.5 ? `<p style="color:#b3261e"><b>⚠ This spot is inside a building on the ` +
        `map, so the result is only a rough guide.</b></p>` : ""}
      <a class="btn" target="_blank" href="${gmap}">Directions (Google Maps)</a>
      <a class="btn alt" target="_blank" href="${osmL}">See on OpenStreetMap</a>
      <div id="chart"></div>
      <p class="muted">The picture shows what stands in the way in each compass
      direction: brown = ground and hills, green = ground + trees + buildings,
      orange dots = where the sun will be (UK clock time). If the green line is
      above the orange line, the sun is hidden. Detailed data within
      ${D.inner_km} km of the origin, coarser beyond. Small hedges, walls and
      lone trees can be missing — double-check on the ground before
      travelling.</p>`;
    drawChart(h);
    document.getElementById("panel").classList.add("open");
    document.getElementById("busy").style.display = "none";
  }, 30);
}
map.on("click", e => showPoint(e.latlng.lat, e.latlng.lng, "Clicked point"));
let clickMark = null;
function closePanel(){ document.getElementById("panel").classList.remove("open"); }

function drawChart(h){
  const W = 448, H = 250, mL = 34, mB = 24, mT = 8, mR = 6;
  const ymax = Math.max(25, ...h.bare, ...h.veg) + 3;
  const X = a => mL + (a - AZS[0]) / (AZS[AZS.length-1] - AZS[0]) * (W - mL - mR);
  const Y = v => mT + (ymax - v) / (ymax + 2) * (H - mT - mB);
  let el = `<rect x="${X(D.sector[0])}" y="${mT}" width="${X(D.sector[1])-X(D.sector[0])}"
          height="${H-mT-mB}" fill="#ffd75e" opacity=".18"/>`;
  let p = `M ${X(AZS[0])} ${Y(-2)}`;
  AZS.forEach((a,i)=> p += ` L ${X(a)} ${Y(h.bare[i])}`);
  p += ` L ${X(AZS[AZS.length-1])} ${Y(-2)} Z`;
  el += `<path d="${p}" fill="saddlebrown" opacity=".55"/>`;
  let pv = ""; AZS.forEach((a,i)=> pv += `${i?"L":"M"} ${X(a)} ${Y(h.veg[i])} `);
  el += `<path d="${pv}" fill="none" stroke="forestgreen" stroke-width="1.4"/>`;
  let ps = ""; D.track.forEach(t => ps += `${ps?"L":"M"} ${X(t[0])} ${Y(t[1])} `);
  el += `<path d="${ps}" fill="none" stroke="tab:orange" stroke-width="1.6"/>`;
  D.track.forEach(t => { if (t[2]) el +=
    `<circle cx="${X(t[0])}" cy="${Y(t[1])}" r="2.2" fill="tab:orange"/>
     <text x="${X(t[0])}" y="${Y(t[1])-5}" font-size="8" text-anchor="middle"
       fill="#666">${bstLabel(t[2])}</text>`; });
  el += `<line x1="${mL}" y1="${Y(0)}" x2="${W-mR}" y2="${Y(0)}" stroke="#000" stroke-width=".7"/>`;
  const dir = a => a === 180 ? "S" : a === 270 ? "W" : a === 360 ? "N" : a + "°";
  for (let a = 180; a <= 360; a += 30)
    el += `<text x="${X(a)}" y="${H-8}" font-size="9" text-anchor="middle">${dir(a)}</text>`;
  for (let v = 0; v <= ymax; v += 10)
    el += `<text x="${mL-4}" y="${Y(v)+3}" font-size="9" text-anchor="end">${v}°</text>`;
  document.getElementById("chart").innerHTML =
    `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">${el}</svg>`;
}
</script></body></html>
"""
