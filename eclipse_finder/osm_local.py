"""Local OSM access data from a Geofabrik PBF (no Overpass needed).

Two streaming passes over great-britain-latest.osm.pbf with pyosmium:

  pass 1 (nodes): keep coordinates of nodes within 1.5 km of any candidate
                  site (tiny: ~10^5); tagged parking nodes attributed direct.
  pass 2 (ways):  for parking/path/land ways, attribute the way to a site if
                  any of its node refs is in the kept set (per-kind radius).

Writes the SAME per-site cache files data/osm/<lat>_<lon>.json that
access.osm_near() reads, so the access step then runs fully offline.
Overpass remains only as a fallback for sites outside the PBF.
"""
from __future__ import annotations

import json
import pathlib

import numpy as np
import osmium
from scipy.spatial import cKDTree

DATA_DIR = pathlib.Path(__file__).parent.parent / "data"
PBF_PATH = DATA_DIR / "osm" / "great-britain.osm.pbf"

PATH_HW = {"path", "footway", "track", "steps", "bridleway"}
LAND_NATURAL = {"heath", "moor", "grassland", "fell"}
R_PARK, R_PATH, R_LAND = 1500.0, 300.0, 400.0


def _proj(lat0: float):
    m_lat = 111_132.0
    m_lon = 111_412.84 * np.cos(np.radians(lat0)) - 93.5 * np.cos(np.radians(3 * lat0))
    return m_lon, m_lat


def way_kind(tags) -> str | None:
    t = dict(tags)
    if t.get("amenity") == "parking":
        return "parking"
    if t.get("highway") in PATH_HW:
        return "paths"
    if t.get("leisure") == "nature_reserve" or t.get("natural") in LAND_NATURAL \
            or t.get("designation") == "access_land":
        return "land"
    return None


def land_sub(tags) -> str:
    t = dict(tags)
    if t.get("leisure") == "nature_reserve":
        return "nature_reserve"
    if t.get("natural") in LAND_NATURAL:
        return "open_land"
    if t.get("designation") == "access_land":
        return "access_land"
    return "open_land"


class _Nodes(osmium.SimpleHandler):
    def __init__(self, tree, origin, sites_xy, m_lon, m_lat, sites, near, per):
        super().__init__()
        self.tree = tree
        self.origin = origin  # (lat0, lon0)
        self.sites_xy = sites_xy
        self.m_lon, self.m_lat = m_lon, m_lat
        self.sites = sites
        self.near = near  # node_id -> (lat, lon, [site idxs within R_PARK])
        self.per = per
        self.bbox = None

    def set_bbox(self, w, s, e, n):
        self.bbox = (w, s, e, n)

    def node(self, n):
        lat = n.location.lat
        lon = n.location.lon
        w, s, e, nn = self.bbox
        if not (w <= lon <= e and s <= lat <= nn):
            return
        x = (lon - self.origin[1]) * self.m_lon
        y = (lat - self.origin[0]) * self.m_lat
        d, i = self.tree.query((x, y))
        if d > R_PARK:
            return
        idxs = [
            i2 for i2 in range(len(self.sites))
            if (self.sites_xy[i2, 0] - x) ** 2 + (self.sites_xy[i2, 1] - y) ** 2 <= R_PARK ** 2
        ]
        self.near[n.id] = (lat, lon, idxs)
        if dict(n.tags).get("amenity") == "parking":
            for i2 in idxs:
                self.per[i2]["parking"].append([lat, lon])


class _Ways(osmium.SimpleHandler):
    def __init__(self, near, per):
        super().__init__()
        self.near = near
        self.per = per

    def way(self, w):
        kind = way_kind(w.tags)
        if kind is None:
            return
        r = {"parking": R_PARK, "paths": R_PATH, "land": R_LAND}[kind]
        seen = set()
        for ref in w.nodes:
            hit = self.near.get(ref.ref)
            if not hit:
                continue
            lat, lon, idxs = hit
            for i2 in idxs:
                sx, sy = self.per[i2]["_xy"]
                # re-check per-kind radius using stored site xy vs node xy is
                # already <= R_PARK; tighten for paths/land:
                if kind != "parking" and r < R_PARK:
                    # cheap lat/lon box prefilter then haversine-ish
                    dlat = (lat - self.per[i2]["_lat"]) * 111_132.0
                    dlon = (lon - self.per[i2]["_lon"]) * self.per[i2]["_mlon"]
                    if dlat * dlat + dlon * dlon > r * r:
                        continue
                if kind == "parking":
                    continue  # parking ways: use first near node as spot
                key = (i2, w.id)
                if key in seen:
                    continue
                seen.add(key)
                if kind == "paths":
                    self.per[i2]["n_paths"] += 1
                else:
                    self.per[i2]["land"][land_sub(w.tags)] += 1
            if kind == "parking":
                for i2 in idxs:
                    key = (i2, w.id)
                    if key not in seen:
                        seen.add(key)
                        self.per[i2]["parking"].append([lat, lon])
                break


def build_access_cache(sites: list[tuple[float, float]], verbose: bool = True,
                       pbf: pathlib.Path | str | None = None) -> int:
    """Fill data/osm/<lat>_<lon>.json for every site from the local PBF.
    Returns number of sites written."""
    from .access import OSM_CACHE

    lat0 = float(np.mean([s[0] for s in sites]))
    m_lon, m_lat = _proj(lat0)
    sites_xy = np.array([[(lo - sites[0][1]) * m_lon, (la - sites[0][0]) * m_lat]
                         for la, lo in sites])
    tree = cKDTree(sites_xy)

    per = []
    for (la, lo) in sites:
        per.append({"parking": [], "n_paths": 0,
                    "land": {"nature_reserve": 0, "open_land": 0,
                             "national_park": 0, "access_land": 0},
                    "_lat": la, "_lon": lo, "_mlon": m_lon, "_xy": [lo, la]})

    near: dict[int, tuple[float, float, list[int]]] = {}
    pad = R_PARK / 111_000.0 + 0.01
    w = min(lo for _, lo in sites) - pad
    e = max(lo for _, lo in sites) + pad
    s = min(la for la, _ in sites) - pad
    n = max(la for la, _ in sites) + pad

    nh = _Nodes(tree, sites[0], sites_xy, m_lon, m_lat, sites, near, per)
    nh.set_bbox(w, s, e, n)
    if verbose:
        print(f"[osm] pass 1/2 nodes … ({len(sites)} sites, bbox {w:.2f},{s:.2f}..{e:.2f},{n:.2f})")
    nh.apply_file(str(pbf or PBF_PATH))
    if verbose:
        print(f"[osm] kept {len(near):,} near-site nodes")

    wh = _Ways(near, per)
    if verbose:
        print("[osm] pass 2/2 ways …")
    wh.apply_file(str(pbf or PBF_PATH))

    written = 0
    for (la, lo), p in zip(sites, per):
        cache = OSM_CACHE / f"{la:.5f}_{lo:.5f}.json"
        out = {"parking": [[float(a), float(b)] for a, b in p["parking"]],
               "n_paths": int(p["n_paths"]),
               "land": {k: int(v) for k, v in p["land"].items()}
               | {"national_park": 0}}
        cache.write_text(json.dumps(out))
        written += 1
    if verbose:
        print(f"[osm] wrote {written} site caches")
    return written
