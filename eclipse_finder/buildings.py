"""Buildings + hedges obstacle layer from the local GB PBF.

Two streaming pyosmium passes over the PBF (cached as a pickle per name+bbox):
  pass 1: node coords inside the bbox
  pass 2: building footprints (way + building=*) and hedge lines
          (natural=hedge); heights from height / building:levels tags,
          else defaults (building 6 m, hedge 2 m).

Rasterised at ~10 m into a uint8 obstacle-height layer for the webmap's
inner region. Relations (multipolygon buildings) are skipped in v1.
"""
from __future__ import annotations

import gzip
import json
import pathlib
import pickle

import numpy as np
import osmium
from matplotlib.path import Path

from .osm_local import PBF_PATH

DATA_DIR = pathlib.Path(__file__).parent.parent / "data"
BLD_CACHE = DATA_DIR / "buildings"
BLD_CACHE.mkdir(parents=True, exist_ok=True)

B_RES_M = 10.0
DEFAULT_B_H, DEFAULT_H_H = 6.0, 2.0


def _height_m(tags: dict, default: float) -> float:
    h = tags.get("height")
    if h:
        try:
            return max(1.0, min(255.0, float(str(h).split(" ")[0].split("m")[0])))
        except ValueError:
            pass
    lv = tags.get("building:levels")
    if lv:
        try:
            return max(1.0, min(255.0, 3.0 * float(str(lv).split(";")[0]) + 2.0))
        except ValueError:
            pass
    return default


class _Nodes(osmium.SimpleHandler):
    def __init__(self, bbox, nodes):
        super().__init__()
        self.bbox = bbox
        self.nodes = nodes

    def node(self, n):
        w, s, e, nn = self.bbox
        lat, lon = n.location.lat, n.location.lon
        if w <= lon <= e and s <= lat <= nn:
            self.nodes[n.id] = (lon, lat)


class _Ways(osmium.SimpleHandler):
    def __init__(self, nodes, buildings, hedges):
        super().__init__()
        self.nodes = nodes
        self.buildings = buildings
        self.hedges = hedges

    def way(self, w):
        t = dict(w.tags)
        if t.get("building") not in (None, "no"):
            kind = "building"
        elif t.get("natural") == "hedge" or t.get("barrier") == "hedge":
            kind = "hedge"
        else:
            return
        coords = []
        for ref in w.nodes:
            c = self.nodes.get(ref.ref)
            if c is None:
                return  # geometry crosses the bbox; skip incomplete
            coords.append(c)
        if kind == "building":
            if len(coords) >= 3:
                self.buildings.append((_height_m(t, DEFAULT_B_H), coords))
        elif len(coords) >= 2:
            self.hedges.append(coords)


def extract_vectors(name: str, bbox: tuple[float, float, float, float],
                    verbose: bool = True) -> dict:
    """(w,s,e,n) -> {buildings: [(h, [(lon,lat)..])..], hedges: [..]}; cached."""
    key = f"{name}_{bbox[0]:.3f}_{bbox[1]:.3f}_{bbox[2]:.3f}_{bbox[3]:.3f}"
    path = BLD_CACHE / f"{key}.pkl"
    if path.exists():
        if verbose:
            print(f"[buildings] cached {path.name}")
        return pickle.loads(path.read_bytes())
    if not PBF_PATH.exists():
        if verbose:
            print("[buildings] no local PBF — skipping building layer")
        return {"buildings": [], "hedges": []}

    pad = 0.02
    bb = (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)
    nodes: dict[int, tuple[float, float]] = {}
    if verbose:
        print(f"[buildings] pass 1/2 nodes (bbox {bbox[0]:.2f},{bbox[1]:.2f}.."
              f"{bbox[2]:.2f},{bbox[3]:.2f}) …")
    _Nodes(bb, nodes).apply_file(str(PBF_PATH))
    if verbose:
        print(f"[buildings] kept {len(nodes):,} nodes")

    buildings, hedges = [], []
    if verbose:
        print("[buildings] pass 2/2 ways …")
    _Ways(nodes, buildings, hedges).apply_file(str(PBF_PATH))
    if verbose:
        print(f"[buildings] {len(buildings):,} buildings, {len(hedges):,} hedges")

    out = {"buildings": buildings, "hedges": hedges}
    path.write_bytes(pickle.dumps(out, protocol=4))
    return out


def rasterize(vec: dict, lon0: float, lat0: float, w: int, h: int,
              m_lon: float, m_lat: float) -> np.ndarray:
    """uint8 obstacle heights on a ~10 m grid; (lon0, lat0) = top-left."""
    from matplotlib.path import Path

    res_lon = B_RES_M / m_lon
    res_lat = B_RES_M / m_lat
    out = np.zeros((h, w), np.uint8)

    def stamp_poly(pts, hgt: int):
        xs = np.array([p[0] for p in pts])
        ys = np.array([p[1] for p in pts])
        c0, c1 = int((xs.min() - lon0) / res_lon) - 1, int((xs.max() - lon0) / res_lon) + 2
        r0, r1 = int((lat0 - ys.max()) / res_lat) - 1, int((lat0 - ys.min()) / res_lat) + 2
        c0, r0 = max(0, c0), max(0, r0)
        c1, r1 = min(w, c1), min(h, r1)
        if c1 <= c0 or r1 <= r0:
            return
        cc, rr = np.meshgrid(np.arange(c0, c1), np.arange(r0, r1))
        lons = lon0 + (cc + 0.5) * res_lon
        lats = lat0 - (rr + 0.5) * res_lat
        mask = Path(pts).contains_points(
            np.column_stack([lons.ravel(), lats.ravel()])).reshape(rr.shape[0], cc.shape[1])
        sub = out[r0:r1, c0:c1]
        np.maximum(sub, np.where(mask, hgt, sub), out=sub)

    for hgt, pts in vec["buildings"]:
        if pts[0] != pts[-1]:
            pts = pts + [pts[0]]
        if len(pts) >= 4:
            stamp_poly(pts, int(round(max(1.0, min(255.0, hgt)))))

    hh = int(round(DEFAULT_H_H))
    for pts in vec["hedges"]:
        arr = np.asarray(pts)
        for (lo1, la1), (lo2, la2) in zip(arr, arr[1:]):
            d = np.hypot((lo2 - lo1) * m_lon, (la2 - la1) * m_lat)
            n = max(1, int(d / (B_RES_M * 0.7)))
            for t in np.linspace(0.0, 1.0, n + 1):
                c = int((lo1 + (lo2 - lo1) * t - lon0) / res_lon)
                r = int((lat0 - (la1 + (la2 - la1) * t)) / res_lat)
                if 0 <= c < w and 0 <= r < h and out[r, c] < hh:
                    out[r, c] = hh
    return out


# ------------------------------------------------- hosted 10 m UK layer ----
def build_bld_tiles(verbose: bool = True):
    """Rasterise GB buildings+hedges at 10 m into pages/tiles/bld/ (sparse,
    gzip) + bld_manifest.json. Single pass over the location-augmented PBF
    (osmium add-locations-to-ways), LRU of per-1-degree cell rasters."""
    from collections import OrderedDict
    from types import SimpleNamespace
    from .tiles import LON0, LAT0, TILE, OUTPUT_DIR as PAGES_DIR

    loc_pbf = PBF_PATH.parent / "gb_loc.osm.pbf"
    if not loc_pbf.exists():
        raise RuntimeError("run: osmium add-locations-to-ways -i sparse_file_array "
                           f"{PBF_PATH} -o {loc_pbf}")

    resLon, resLat = B_RES_M / 65_578.0, B_RES_M / 111_306.0
    out = PAGES_DIR / "tiles" / "bld"
    out.mkdir(parents=True, exist_ok=True)
    manifest: set[str] = set()
    cache: "OrderedDict[tuple[int,int], SimpleNamespace]" = OrderedDict()
    state = {"ways": 0}

    def flush(key, C):
        ny, nx = C.arr.shape
        for ty in range(ny // TILE):
            for tx in range(nx // TILE):
                sub = C.arr[ty*TILE:(ty+1)*TILE, tx*TILE:(tx+1)*TILE]
                if sub.max() == 0:
                    continue
                p = out / f"{C.ix0+tx}_{C.iy0+ty}.gz"
                if p.exists():  # merge: cell may have been evicted & re-stamped
                    old = np.frombuffer(gzip.decompress(p.read_bytes()),
                                        np.uint8).reshape(TILE, TILE)
                    np.maximum(sub, old, out=sub)
                p.write_bytes(gzip.compress(sub.tobytes(), 6))
                manifest.add(f"{C.ix0+tx}_{C.iy0+ty}")

    def cell_for(lat: float, lon: float):
        key = (int(np.floor(lat)), int(np.floor(lon)))
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
        if len(cache) >= 16:
            flush(*cache.popitem(last=False))
        la0, lo0 = key
        ix0 = int(np.floor((lo0 - LON0) / (resLon * TILE)))
        ix1 = int(np.floor((lo0 + 1 - LON0) / (resLon * TILE) - 1e-9))
        iy0 = int(np.floor((LAT0 - (la0 + 1)) / (resLat * TILE)))
        iy1 = int(np.floor((LAT0 - la0) / (resLat * TILE) - 1e-9))
        C = SimpleNamespace(arr=np.zeros(((iy1-iy0+1)*TILE, (ix1-ix0+1)*TILE),
                                         np.uint8), ix0=ix0, iy0=iy0)
        cache[key] = C
        return C

    def stamp_poly(C, pts, hgt):
        xs = np.array([p[0] for p in pts]); ys = np.array([p[1] for p in pts])
        gx = (xs - LON0) / resLon - C.ix0 * TILE
        gy = (LAT0 - ys) / resLat - C.iy0 * TILE
        c0, c1 = int(gx.min()) - 1, int(gx.max()) + 2
        r0, r1 = int(gy.min()) - 1, int(gy.max()) + 2
        H, W = C.arr.shape
        c0, r0 = max(0, c0), max(0, r0)
        c1, r1 = min(W, c1), min(H, r1)
        if c1 <= c0 or r1 <= r0:
            return
        if c1 - c0 <= 3 and r1 - r0 <= 3:  # tiny footprint: centre pixel
            cx_, cy_ = int((c0 + c1) / 2), int((r0 + r1) / 2)
            if C.arr[cy_, cx_] < hgt:
                C.arr[cy_, cx_] = hgt
            return
        cc, rr = np.meshgrid(np.arange(c0, c1), np.arange(r0, r1))
        lons = LON0 + (C.ix0 * TILE + cc + 0.5) * resLon
        lats = LAT0 - (C.iy0 * TILE + rr + 0.5) * resLat
        mask = Path(pts).contains_points(
            np.column_stack([lons.ravel(), lats.ravel()])).reshape(rr.shape[0], cc.shape[1])
        sub = C.arr[r0:r1, c0:c1]
        np.maximum(sub, np.where(mask, hgt, sub), out=sub)

    def stamp_line(C, pts, hgt):
        arr = np.asarray(pts)
        for (lo1, la1), (lo2, la2) in zip(arr, arr[1:]):
            d = np.hypot((lo2 - lo1) * 65_578, (la2 - la1) * 111_306)
            n = max(1, int(d / (B_RES_M * 0.7)))
            gx = (lo1 + (lo2 - lo1) * np.linspace(0, 1, n + 1) - LON0) / resLon - C.ix0 * TILE
            gy = (LAT0 - (la1 + (la2 - la1) * np.linspace(0, 1, n + 1))) / resLat - C.iy0 * TILE
            for x, y in zip(gx, gy):
                c, r = int(x), int(y)
                if 0 <= c < C.arr.shape[1] and 0 <= r < C.arr.shape[0] \
                        and C.arr[r, c] < hgt:
                    C.arr[r, c] = hgt

    class H(osmium.SimpleHandler):
        def way(self, w):
            t = w.tags
            if t.get("building") not in (None, "no"):
                hgt = int(round(_height_m(t, DEFAULT_B_H)))
                kind = "b"
            elif t.get("natural") == "hedge" or t.get("barrier") == "hedge":
                hgt = int(DEFAULT_H_H)
                kind = "h"
            else:
                return
            coords = []
            for n in w.nodes:
                if not n.location.valid():
                    return
                coords.append((n.location.lon, n.location.lat))
            if len(coords) < (3 if kind == "b" else 2):
                return
            state["ways"] += 1
            if verbose and state["ways"] % 1_000_000 == 0:
                print(f"[bldtiles] {state['ways']//1_000_000}M ways …", flush=True)
            if kind == "b":
                if coords[0] != coords[-1]:
                    coords = coords + [coords[0]]
                la, lo = coords[0][1], coords[0][0]
                stamp_poly(cell_for(la, lo), coords, hgt)
            else:
                done = set()
                for lo, la in coords:
                    k = (int(np.floor(la)), int(np.floor(lo)))
                    if k in done:
                        continue
                    done.add(k)
                    stamp_line(cell_for(la, lo), coords, hgt)

    if verbose:
        print("[bldtiles] single pass over gb_loc.osm.pbf …")
    H().apply_file(str(loc_pbf))
    for key, C in cache.items():
        flush(key, C)
    (PAGES_DIR / "tiles" / "bld_manifest.json").write_text(
        json.dumps(sorted(manifest)))
    meta_path = PAGES_DIR / "tiles" / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["bld"] = dict(resLon=resLon, resLat=resLat, d_bld=2000)
    meta_path.write_text(json.dumps(meta, indent=1))
    if verbose:
        total = sum(p.stat().st_size for p in out.glob("*.gz"))
        print(f"[bldtiles] {state['ways']:,} ways -> {len(manifest):,} tiles, "
              f"{total/1e6:.1f} MB")
    return len(manifest)
