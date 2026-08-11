"""UK-wide explorer page (pages/index.html) for GitHub Pages.

Click anywhere in GB (or search a place/postcode): terrain+canopy tiles and
a hosted 10 m buildings layer are fetched on demand and the eclipse
visibility is computed client-side. All user-facing text is plain language.
"""
from __future__ import annotations

import json
import pathlib

from .solar import compute_eclipse_geometry
from .tiles import LON0, LAT0, TILE, levels

OUTPUT_DIR = pathlib.Path(__file__).parent.parent / "pages"


def _tracks_grid(date: str):
    lats = list(range(50, 62, 2))
    lons = [-8, -5, -2, 1]
    grid, t0 = {}, {}
    for la in lats:
        for lo in lons:
            geo = compute_eclipse_geometry(float(la), float(lo), date)
            grid[f"{la},{lo}"] = [
                [round(s.az_deg, 2), round(s.alt_deg, 2),
                 s.t_utc.strftime("%H:%M") if i % 10 == 0 else ""]
                for i, s in enumerate(geo.useful_samples)]
            s0 = geo.useful_samples[0].t_utc
            t0[f"{la},{lo}"] = s0.hour * 60 + s0.minute
    n = min(len(v) for v in grid.values())
    grid = {k: v[:n] for k, v in grid.items()}
    return dict(lats=lats, lons=lons, grid=grid, t0=t0,
                mag=round(geo.max_magnitude, 3))


def build_ukmap(date: str = "2026-08-12", verbose: bool = True):
    meta = json.loads((OUTPUT_DIR / "tiles" / "meta.json").read_text()) \
        if (OUTPUT_DIR / "tiles" / "meta.json").exists() else \
        dict(lon0=LON0, lat0=LAT0, tile=TILE, d_near=15000,
             levels=[dict(m=l["m"], resLon=l["resLon"], resLat=l["resLat"])
                     for l in levels()])
    data = dict(meta=meta, tracks=_tracks_grid(date), date=date)
    html = (_HTML.replace("__DATA__", json.dumps(data))
                 .replace("__DATE__", date))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / "index.html"
    out.write_text(html)
    if verbose:
        print(f"wrote {out} ({out.stat().st_size/1e3:.0f} KB)")
    return out


_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Will I see the eclipse? — UK explorer</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
 html,body{margin:0;height:100%;font:14px/1.5 system-ui,sans-serif}
 #map{position:absolute;top:0;left:0;right:0;bottom:0;
   cursor:url("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' width='30' height='30'><circle cx='15' cy='15' r='9' fill='none' stroke='%23d32f2f' stroke-width='3'/><line x1='15' y1='0' x2='15' y2='10' stroke='%23d32f2f' stroke-width='3'/><line x1='15' y1='20' x2='15' y2='30' stroke='%23d32f2f' stroke-width='3'/><line x1='0' y1='15' x2='10' y2='15' stroke='%23d32f2f' stroke-width='3'/><line x1='20' y1='15' x2='30' y2='15' stroke='%23d32f2f' stroke-width='3'/></svg>") 15 15, crosshair}
 #panel{position:absolute;top:0;right:0;bottom:0;width:min(480px,92vw);
   background:#fff;box-shadow:-2px 0 8px rgba(0,0,0,.25);overflow-y:auto;
   transform:translateX(100%);transition:transform .18s;padding:14px 16px;
   box-sizing:border-box;z-index:1000}
 #panel.open{transform:none}
 #panel h3{margin:.2em 0 .4em}
 #verdict{font-size:15px;font-weight:700;padding:8px 10px;border-radius:8px;margin:.4em 0}
 #verdict.ok{background:#e8f5e9;color:#1b5e20}
 #verdict.bad{background:#ffebee;color:#b71c1c}
 #panel .stats{display:grid;grid-template-columns:auto auto;gap:2px 14px;margin:.5em 0}
 a.btn{display:inline-block;margin:4px 6px 4px 0;padding:5px 10px;border-radius:6px;
   background:#1a73e8;color:#fff;text-decoration:none;font-weight:600}
 a.btn.alt{background:#5f6368}
 #close{float:right;cursor:pointer;font-size:16px;color:#888}
 .muted{color:#777;font-size:12px}
 svg{max-width:100%}
 #legend{position:absolute;bottom:12px;left:12px;z-index:900;background:#fffc;
   padding:6px 10px;border-radius:6px;font-size:12px;max-width:52%}
 #busy{position:absolute;top:12px;left:50%;transform:translateX(-50%);z-index:1100;
   background:#333e;color:#fff;padding:6px 14px;border-radius:20px;display:none}
 #search{position:absolute;top:12px;left:12px;z-index:1000}
 #search input{width:250px;padding:8px 10px;border:1px solid #bbb;border-radius:8px;
   font-size:14px;background:#fff}
 #results{display:none;list-style:none;margin:4px 0 0;padding:4px;background:#fff;
   border:1px solid #bbb;border-radius:8px;max-width:340px;box-shadow:0 2px 8px #0003}
 #results li{padding:6px 8px;cursor:pointer;border-radius:6px}
 #results li:hover{background:#eef}
 #intro{position:absolute;top:14%;left:50%;transform:translateX(-50%);z-index:1200;
   background:#fffdf5;border:2px solid #c8a400;border-radius:12px;max-width:430px;
   padding:16px 20px;box-shadow:0 4px 20px #0004;text-align:center}
 #intro h2{margin:.1em 0 .4em;font-size:18px}
 #intro button{margin-top:8px;padding:7px 18px;border:0;border-radius:8px;
   background:#c8a400;color:#fff;font-weight:700;cursor:pointer}
</style></head><body>
<div id="map"></div>
<div id="search"><input id="q" type="text" autocomplete="off"
  placeholder="Search a town or postcode…"><ul id="results"></ul></div>
<div id="legend"></div>
<div id="busy">Checking your spot…</div>
<div id="intro">
  <h2>☀️ The 12 August 2026 solar eclipse, from anywhere in Britain</h2>
  <p><b>Click the map</b> — or search a town or postcode — to see whether the
  eclipse will be visible from that exact spot.<br>
  <b>Drag</b> to move around. <b>Scroll</b> to zoom in and out.</p>
  <p class="muted">⚠️ This is a hobby project built from public map data and I
  make no promises it’s accurate. If you travel hundreds of miles based on it,
  that’s at your own risk!</p>
  <button onclick="dismissIntro()">Got it</button>
  <p class="muted"><a href="https://bacon.boutique" target="_blank">by Seb</a></p>
</div>
<div id="panel"><span id="close" onclick="closePanel()">✕</span><div id="pbody"></div></div>
<script>
const D = __DATA__;
const M = D.meta, LV = M.levels;

const map = L.map("map").setView([54.0, -2.5], 6);
const streets = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
  {maxZoom: 19, attribution: "© OpenStreetMap"});
const terrain = L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
  {maxZoom: 17, attribution: "© OpenStreetMap, SRTM | © OpenTopoMap"});
terrain.addTo(map);
L.control.layers({"Terrain map": terrain, "Street map": streets}).addTo(map);
document.getElementById("legend").innerHTML =
  `<b>Click the map</b> to check the eclipse view from that spot · ` +
  `<b>drag</b> to move · <b>scroll</b> to zoom. We work out whether hills, ` +
  `trees and buildings get in the way of the sun. ` +
  `<a href="https://bacon.boutique" target="_blank">by Seb</a>`;

// --------------------------------------------------------- first-load card
function dismissIntro(){
  document.getElementById("intro").style.display = "none";
  try { localStorage.setItem("ef_intro", "1"); } catch (e) {}
}
try { if (localStorage.getItem("ef_intro") === "1")
  document.getElementById("intro").style.display = "none"; } catch (e) {}

// ------------------------------------------------------------------ search
const q = document.getElementById("q"), resUl = document.getElementById("results");
let deb;
q.addEventListener("input", () => { clearTimeout(deb); deb = setTimeout(doSearch, 450); });
q.addEventListener("keydown", e => {
  if (e.key === "Enter" && resUl.children.length) resUl.children[0].click();
});
function shortName(s){ return s.split(",").slice(0, 3).join(",").trim(); }
async function doSearch(){
  const v = q.value.trim();
  if (v.length < 3) { resUl.style.display = "none"; return; }
  // Nominatim has no CORS headers, so use JSONP (script tag)
  const cbname = "ef_cb_" + Date.now();
  const s = document.createElement("script");
  window[cbname] = j => { delete window[cbname]; s.remove(); renderResults(j); };
  s.onerror = () => { delete window[cbname]; s.remove();
                      resUl.style.display = "none"; };
  s.src = "https://nominatim.openstreetmap.org/search?format=jsonv2&limit=6" +
          "&countrycodes=gb&json_callback=" + cbname +
          "&q=" + encodeURIComponent(v);
  document.head.appendChild(s);
}
function renderResults(j){
  resUl.innerHTML = "";
  (j || []).forEach(it => {
    const li = document.createElement("li");
    li.textContent = shortName(it.display_name);
    li.onclick = () => {
      resUl.style.display = "none";
      q.value = shortName(it.display_name);
      const la = +it.lat, lo = +it.lon;
      map.setView([la, lo], 13);
      showAt(la, lo, shortName(it.display_name));
    };
    resUl.appendChild(li);
  });
  resUl.style.display = (j && j.length) ? "block" : "none";
}

// ------------------------------------------------------------ sun tracks --
function sunTrack(lat, lon){
  const T = D.tracks, la = Math.min(Math.max(lat, T.lats[0]), T.lats.at(-1));
  const lo = Math.min(Math.max(lon, T.lons[0]), T.lons.at(-1));
  let i = 0; while (i < T.lats.length - 2 && T.lats[i+1] < la) i++;
  let j = 0; while (j < T.lons.length - 2 && T.lons[j+1] < lo) j++;
  const fa = (la - T.lats[i]) / (T.lats[i+1] - T.lats[i]);
  const fo = (lo - T.lons[j]) / (T.lons[j+1] - T.lons[j]);
  const g = (a, b) => T.grid[`${a},${b}`];
  const A = g(T.lats[i], T.lons[j]), B = g(T.lats[i], T.lons[j+1]);
  const C = g(T.lats[i+1], T.lons[j]), E = g(T.lats[i+1], T.lons[j+1]);
  const t0 = (1-fo)*((1-fa)*T.t0[`${T.lats[i]},${T.lons[j]}`]+fa*T.t0[`${T.lats[i+1]},${T.lons[j]}`])
           + fo*((1-fa)*T.t0[`${T.lats[i]},${T.lons[j+1]}`]+fa*T.t0[`${T.lats[i+1]},${T.lons[j+1]}`]);
  const samples = A.map((s, k) => {
    const az = (1-fo)*((1-fa)*A[k][0]+fa*C[k][0]) + fo*((1-fa)*B[k][0]+fa*E[k][0]);
    const al = (1-fo)*((1-fa)*A[k][1]+fa*C[k][1]) + fo*((1-fa)*B[k][1]+fa*E[k][1]);
    return [az, al, s[2]];
  });
  return {samples, t0m: t0};
}
function sector(track){
  let a0 = 999, a1 = -999;
  for (const s of track) { a0 = Math.min(a0, s[0]); a1 = Math.max(a1, s[0]); }
  return [a0 - 12, a1 + 12];
}
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

// ------------------------------------------------------------- tile cache --
const TC = new Map();
let fetchedBytes = 0;
async function tile(lv, ix, iy){
  const key = lv.m + "/" + ix + "_" + iy;
  if (TC.has(key)) return TC.get(key);
  const r = await fetch(`tiles/${key}.gz`);
  if (!r.ok) { TC.set(key, null); return null; }
  const buf = await new Response(
    r.body.pipeThrough(new DecompressionStream("gzip"))).arrayBuffer();
  fetchedBytes += buf.byteLength;
  const n = M.tile * M.tile;
  const t = {t: new Int16Array(buf, 0, n), c: new Uint8Array(buf, 2*n, n)};
  TC.set(key, t);
  return t;
}
function sampleAt(lv, t, lon, lat, kind){
  if (!t) return 0;
  const fx = (lon - M.lon0) / lv.resLon, fy = (M.lat0 - lat) / lv.resLat;
  const tx = fx - Math.floor(fx / M.tile) * M.tile - 0.5;
  const ty = fy - Math.floor(fy / M.tile) * M.tile - 0.5;
  const x = Math.min(Math.max(tx, 0), M.tile - 1.001);
  const y = Math.min(Math.max(ty, 0), M.tile - 1.001);
  const c0 = Math.floor(x), r0 = Math.floor(y), fc = x - c0, fr = y - r0;
  const A = t[kind], i = r0 * M.tile + c0;
  return A[i]*(1-fc)*(1-fr) + A[i+1]*fc*(1-fr) + A[i+M.tile]*(1-fc)*fr + A[i+M.tile+1]*fc*fr;
}
function tileAt(lv, lon, lat){
  const ix = Math.floor((lon - M.lon0) / (lv.resLon) / M.tile);
  const iy = Math.floor((M.lat0 - lat) / (lv.resLat) / M.tile);
  return TC.get(lv.m + "/" + ix + "_" + iy) || null;
}
async function ensure(lon, lat){
  const jobs = [];
  const mLon = 111412.84 * Math.cos(lat*Math.PI/180);
  const mLat = 111132.954;
  for (const [lv, rad] of [[LV[0], M.d_near], [LV[1], 55000]]) {
    const dLon = rad / mLon, dLat = rad / mLat;
    const ix0 = Math.floor((lon - dLon - M.lon0) / lv.resLon / M.tile);
    const ix1 = Math.floor((lon + dLon - M.lon0) / lv.resLon / M.tile);
    const iy0 = Math.floor((M.lat0 - lat - dLat) / lv.resLat / M.tile);
    const iy1 = Math.floor((M.lat0 - lat + dLat) / lv.resLat / M.tile);
    for (let ix = ix0; ix <= ix1; ix++)
      for (let iy = iy0; iy <= iy1; iy++)
        if (!TC.has(lv.m + "/" + ix + "_" + iy)) jobs.push(tile(lv, ix, iy));
  }
  await Promise.all(jobs);
}

// ------------------------------------------- hosted 10 m building tiles ----
const BL = M.bld || null;
let BMAN = null;
const BMAN_P = fetch("tiles/bld_manifest.json")
  .then(r => r.ok ? r.json() : []).then(a => { BMAN = new Set(a); })
  .catch(() => { BMAN = new Set(); });
const BTC = new Map();
async function bldTile(ix, iy){
  const key = ix + "_" + iy;
  if (BTC.has(key)) return BTC.get(key);
  let u = null;
  const r = await fetch(`tiles/bld/${key}.gz`);
  if (r.ok) {
    const buf = await new Response(
      r.body.pipeThrough(new DecompressionStream("gzip"))).arrayBuffer();
    fetchedBytes += buf.byteLength;
    u = new Uint8Array(buf);
  }
  BTC.set(key, u);
  return u;
}
async function ensureBld(lon, lat){
  if (!BL || !BMAN) return false;
  const d = BL.d_bld || 2000;
  const mLon = 111412.84 * Math.cos(lat*Math.PI/180), mLat = 111132.954;
  const tw = BL.resLon * M.tile, th = BL.resLat * M.tile;
  const ix0 = Math.floor((lon - d/mLon - M.lon0) / tw), ix1 = Math.floor((lon + d/mLon - M.lon0) / tw);
  const iy0 = Math.floor((M.lat0 - lat - d/mLat) / th), iy1 = Math.floor((M.lat0 - lat + d/mLat) / th);
  let any = false; const jobs = [];
  for (let ix = ix0; ix <= ix1; ix++)
    for (let iy = iy0; iy <= iy1; iy++)
      if (BMAN.has(ix + "_" + iy)) { any = true; jobs.push(bldTile(ix, iy)); }
  await Promise.all(jobs);
  return any;
}
function sampleBldHost(lon, lat){
  if (!BL) return 0;
  const fx = (lon - M.lon0) / BL.resLon, fy = (M.lat0 - lat) / BL.resLat;
  const ix = Math.floor(fx / M.tile), iy = Math.floor(fy / M.tile);
  const u = BTC.get(ix + "_" + iy);
  if (!u) return 0;
  const x = Math.min(Math.max(fx - ix*M.tile - 0.5, 0), M.tile - 1.001);
  const y = Math.min(Math.max(fy - iy*M.tile - 0.5, 0), M.tile - 1.001);
  const c0 = Math.floor(x), r0 = Math.floor(y), fc = x - c0, fr = y - r0;
  const i = r0 * M.tile + c0;
  return u[i]*(1-fc)*(1-fr) + u[i+1]*fc*(1-fr) + u[i+M.tile]*(1-fc)*fr + u[i+M.tile+1]*fc*fr;
}

// ------------------------------- live OSM buildings (fallback, e.g. NI) ----
const BLD = new Map();
function heightOf(t, dflt){
  if (t.height) { const v = parseFloat(String(t.height).split(" ")[0].split("m")[0]);
    if (isFinite(v)) return Math.max(1, Math.min(255, v)); }
  if (t["building:levels"]) { const v = parseFloat(String(t["building:levels"]).split(";")[0]);
    if (isFinite(v)) return Math.max(1, Math.min(255, 3*v + 2)); }
  return dflt;
}
function rasterizeOSM(j, lat, lon){
  const N = 400, R = 2000;
  const cv = document.createElement("canvas"); cv.width = N; cv.height = N;
  const cx = cv.getContext("2d", {willReadFrequently: true});
  cx.globalCompositeOperation = "lighten";
  const mLon = 111412.84 * Math.cos(lat*Math.PI/180), mLat = 111132.954;
  const S = N / (2 * R);
  const X = lo => ((lo - lon) * mLon + R) * S, Y = la => ((lat - la) * mLat + R) * S;
  let nways = 0;
  for (const el of (j.elements || [])) {
    const t = el.tags || {};
    let h = 0;
    if (t.building && t.building !== "no") h = Math.round(heightOf(t, 6));
    else if (t.barrier === "hedge" || t.natural === "hedge") h = 2;
    if (!h) continue;
    const g = el.geometry;
    if (!g || g.length < 2) continue;
    nways++;
    cx.fillStyle = cx.strokeStyle = `rgb(${h},${h},${h})`;
    cx.beginPath();
    g.forEach((p, i) => i ? cx.lineTo(X(p.lon), Y(p.lat)) : cx.moveTo(X(p.lon), Y(p.lat)));
    if (h > 2) { cx.closePath(); cx.fill(); } else { cx.lineWidth = 1; cx.stroke(); }
  }
  const d = cx.getImageData(0, 0, N, N).data;
  const u = new Uint8Array(N * N);
  for (let i = 0; i < N * N; i++) u[i] = d[4*i];
  return {lon0: lon - R/mLon, lat0: lat + R/mLat,
          resLon: 2*R/mLon/N, resLat: 2*R/mLat/N, n: N, b: u, nways};
}
async function buildingsAt(lat, lon){
  const key = (Math.round(lat*100)/100) + "," + (Math.round(lon*100)/100);
  if (BLD.has(key)) return BLD.get(key);
  let b = null;
  const qy = `[out:json][timeout:25];(way["building"](around:2000,${lat},${lon});` +
             `way["barrier"="hedge"](around:2000,${lat},${lon});` +
             `way["natural"="hedge"](around:2000,${lat},${lon}););out geom tags;`;
  for (const mirror of ["https://overpass-api.de/api/interpreter",
                        "https://overpass.kumi.systems/api/interpreter"]) {
    try {
      const r = await fetch(mirror, {method: "POST",
        headers: {"Content-Type": "application/x-www-form-urlencoded"},
        body: "data=" + encodeURIComponent(qy)});
      if (r.ok) { b = rasterizeOSM(await r.json(), lat, lon); break; }
    } catch (e) { /* try next mirror */ }
  }
  BLD.set(key, b);
  return b;
}
function sampleB(bld, lon, lat){
  if (!bld) return 0;
  const col = (lon - bld.lon0) / bld.resLon, row = (bld.lat0 - lat) / bld.resLat;
  if (col < 0 || row < 0 || col > bld.n - 1 || row > bld.n - 1) return 0;
  const c0 = Math.min(Math.floor(col), bld.n - 2), r0 = Math.min(Math.floor(row), bld.n - 2);
  const fc = col - c0, fr = row - r0, i = r0 * bld.n + c0, A = bld.b;
  return A[i]*(1-fc)*(1-fr) + A[i+1]*fc*(1-fr) + A[i+bld.n]*(1-fc)*fr + A[i+bld.n+1]*fc*fr;
}

// ---------------------------------------------------------------- horizon --
const RANGES = (() => { const r = [];
  for (let d = 30; d < 8000; d += 30) r.push(d);
  for (let d = 8000; d < 24000; d += 60) r.push(d);
  for (let d = 24000; d <= 55000; d += 120) r.push(d);
  return r; })();
const DROP = RANGES.map(d => d*d / (2*6371000) * (1 - 0.13));
const AZS = (() => { const a = []; for (let x = 140; x <= 370; x += 1) a.push(x); return a; })();

function horizon(lon, lat, bldS){
  const mLon = 111412.84 * Math.cos(lat*Math.PI/180) - 93.5 * Math.cos(3*lat*Math.PI/180);
  const mLat = 111132.954 - 559.822 * Math.cos(2*lat*Math.PI/180)
             + 1.175 * Math.cos(4*lat*Math.PI/180);
  const hObs = sampleAt(LV[0], tileAt(LV[0], lon, lat), lon, lat, "t") ||
               sampleAt(LV[1], tileAt(LV[1], lon, lat), lon, lat, "t");
  const bare = new Array(AZS.length), vegh = new Array(AZS.length);
  for (let ai = 0; ai < AZS.length; ai++) {
    const az = AZS[ai] * Math.PI / 180, sa = Math.sin(az), ca = Math.cos(az);
    let mb = -90, mv = -90;
    for (let ri = 0; ri < RANGES.length; ri++) {
      const d = RANGES[ri];
      const lv = d <= M.d_near ? LV[0] : LV[1];
      const slon = lon + d * sa / mLon, slat = lat + d * ca / mLat;
      const t = tileAt(lv, slon, slat);
      const h = sampleAt(lv, t, slon, slat, "t");
      const a1 = Math.atan2(h - hObs - DROP[ri], d) * 57.29578;
      if (a1 > mb) mb = a1;
      const c = sampleAt(lv, t, slon, slat, "c");
      const o = Math.max(0.5*c, bldS ? bldS(slon, slat) : 0);
      const a2 = Math.atan2(h + o - hObs - DROP[ri], d) * 57.29578;
      if (a2 > mv) mv = a2;
    }
    bare[ai] = mb; vegh[ai] = mv;
  }
  return {bare, veg: vegh, elev: hObs};
}

// ------------------------------------------------------------------ panel --
map.on("click", e => showAt(e.latlng.lat, e.latlng.lng));
let clickMark = null;
async function showAt(lat, lon, label){
  dismissIntro();
  clickMark = clickMark
    ? clickMark.setLatLng([lat, lon])
    : L.circleMarker([lat, lon], {radius: 9, color: "#fff", weight: 3,
        fillColor: "#d32f2f", fillOpacity: 1, interactive: false}).addTo(map);
  document.getElementById("busy").style.display = "block";
  const kb0 = fetchedBytes;
  await ensure(lon, lat);
  await BMAN_P;
  const hosted = await ensureBld(lon, lat);
  let bldS = null, bldLabel = "not available for this spot";
  if (hosted) { bldS = sampleBldHost; bldLabel = "included (OpenStreetMap buildings)"; }
  else {
    const ob = await buildingsAt(lat, lon);
    if (ob) { bldS = (lo, la) => sampleB(ob, lo, la);
             bldLabel = "included (OpenStreetMap buildings)"; }
  }
  const t0 = performance.now();
  const h = horizon(lon, lat, bldS);
  const ms = Math.round(performance.now() - t0);
  const b0 = bldS ? bldS(lon, lat) : 0;
  const trk = sunTrack(lat, lon), track = trk.samples, sec = sector(track);
  let minC = 1e9, maxVeg = -90, bFirst = -1, bLast = -1;
  track.forEach((s, idx) => {
    if (s[0] < sec[0] || s[0] > sec[1]) return;
    const i = Math.round(s[0]) - AZS[0];
    if (i < 0 || i >= AZS.length) return;
    minC = Math.min(minC, s[1] - h.veg[i]);
    maxVeg = Math.max(maxVeg, h.veg[i]);
    if (h.veg[i] > s[1]) { if (bFirst < 0) bFirst = idx; bLast = idx; }
  });
  const verdict = bFirst < 0
    ? "✔ Good view: the sun stays clear of hills, trees and buildings for the whole eclipse."
    : `✘ Blocked: the sun disappears behind hills, trees or buildings from about ` +
      `${fmtTime(trk.t0m + bFirst)} until ${fmtTime(trk.t0m + bLast)} (UK time).`;
  const gmap = `https://www.google.com/maps/dir/?api=1&destination=${lat},${lon}&travelmode=driving`;
  const osmL = `https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}#map=15/${lat}/${lon}`;
  document.getElementById("pbody").innerHTML = `
    <h3>${label ? label + " — " : ""}${h.elev.toFixed(0)} m above sea level</h3>
    <div id="verdict" class="${bFirst < 0 ? "ok" : "bad"}">${verdict}</div>
    <div class="stats">
      <span>Where</span><b>${lat.toFixed(5)}, ${lon.toFixed(5)}</b>
      <span>Tallest blockage towards the sun</span><b>${maxVeg.toFixed(1)}° above a flat horizon</b>
      <span>Worst gap between sun and blockage</span><b>${minC.toFixed(1)}° (below 0 = blocked)</b>
      <span>Buildings &amp; hedges</span><b>${bldLabel}</b>
      <span>Map data downloaded</span><b>${((fetchedBytes-kb0)/1024).toFixed(0)} KB</b>
      <span>Calculated in</span><b>${ms} ms</b>
    </div>
    ${b0 > 0.5 ? `<p style="color:#b3261e"><b>⚠ This spot is inside a building on the ` +
      `map, so the result is only a rough guide.</b></p>` : ""}
    <a class="btn" target="_blank" href="${gmap}">Directions (Google Maps)</a>
    <a class="btn alt" target="_blank" href="${osmL}">See on OpenStreetMap</a>
    <div id="chart"></div>
    <p class="muted">The picture shows what stands in the way in each compass direction
    (270° = due west): brown = ground and hills, green = ground + trees + buildings,
    orange dots = where the sun will be (UK clock time). If the green line is above
    the orange line, the sun is hidden. Estimates from public map data — small
    hedges, walls and lone trees can be missing, so always double-check on the
    ground before travelling.</p>`;
  drawChart(h, track, sec);
  document.getElementById("panel").classList.add("open");
  document.getElementById("busy").style.display = "none";
}
if (location.hash.startsWith("#test=")) {
  const [la, lo] = location.hash.slice(6).split(",").map(Number);
  setTimeout(() => showAt(la, lo), 300);
}
function closePanel(){ document.getElementById("panel").classList.remove("open"); }

function drawChart(h, track, sec){
  const W = 448, H = 250, mL = 34, mB = 24, mT = 8, mR = 6;
  const ymax = Math.max(25, ...h.bare, ...h.veg) + 3;
  const X = a => mL + (a - AZS[0]) / (AZS[AZS.length-1] - AZS[0]) * (W - mL - mR);
  const Y = v => mT + (ymax - v) / (ymax + 2) * (H - mT - mB);
  let el = `<rect x="${X(sec[0])}" y="${mT}" width="${X(sec[1])-X(sec[0])}"
          height="${H-mT-mB}" fill="#ffd75e" opacity=".18"/>`;
  let p = `M ${X(AZS[0])} ${Y(-2)}`;
  AZS.forEach((a,i)=> p += ` L ${X(a)} ${Y(h.bare[i])}`);
  p += ` L ${X(AZS[AZS.length-1])} ${Y(-2)} Z`;
  el += `<path d="${p}" fill="saddlebrown" opacity=".55"/>`;
  let pv = ""; AZS.forEach((a,i)=> pv += `${i?"L":"M"} ${X(a)} ${Y(h.veg[i])} `);
  el += `<path d="${pv}" fill="none" stroke="forestgreen" stroke-width="1.4"/>`;
  let ps = ""; track.forEach(t => ps += `${ps?"L":"M"} ${X(t[0])} ${Y(t[1])} `);
  el += `<path d="${ps}" fill="none" stroke="tab:orange" stroke-width="1.6"/>`;
  track.forEach((t, k) => { if (t[2]) el +=
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
