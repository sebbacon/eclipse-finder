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

import pathlib
import pickle

import numpy as np
import osmium

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
