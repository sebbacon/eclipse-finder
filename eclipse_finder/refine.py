"""Sub-grid refinement of the standing point.

For each access-ranked site: score every 30 m DEM cell within ~250 m on
eclipse clearance (veg-corrected, sector only), penalise distance from mapped
public paths and local canopy, and emit the best exact standing point plus
its full horizon profile, walk-from-parking and access notes.
"""
from __future__ import annotations

import csv
import json
import pathlib

import numpy as np

from .dem import Dem, DEMO_DIR
from .horizon import (HorizonGrid, default_ranges, horizon_profile,
                      horizon_profile_batch, _interp_along_axis)
from .solar import azimuth_sector, compute_eclipse_geometry
from .vegetation import Veg, ALPHA

OUTPUT_DIR = pathlib.Path(__file__).parent.parent / "output"

PATH_BOX = 0.012  # ~1.3 km half-box for OSM path fetch


def _paths_and_parking(lat: float, lon: float):
    """Path node coords + parking coords from OSM main API (small box)."""
    import requests
    import xml.etree.ElementTree as ET
    root = None
    for dy in (PATH_BOX, 0.007, 0.004):
        dx = dy / max(0.3, np.cos(np.radians(lat)))
        bbox = f"{lon-dx:.5f},{lat-dy:.5f},{lon+dx:.5f},{lat+dy:.5f}"
        r = requests.get(f"https://api.openstreetmap.org/api/0.6/map?bbox={bbox}",
                         timeout=90, headers={"User-Agent": "eclipse-finder/0.1 (geospatial research)"})
        if r.status_code == 400 and b"too many nodes" in r.content:
            continue
        r.raise_for_status()
        root = ET.fromstring(r.content)
        break
    if root is None:
        return None, []
    nodes = {n.get("id"): (float(n.get("lat")), float(n.get("lon")),
                           {t.get("k"): t.get("v") for t in n.findall("tag")})
             for n in root.findall("node")}
    paths, parking = [], []
    for nid, (nlat, nlon, t) in nodes.items():
        if t.get("amenity") == "parking":
            parking.append((nlat, nlon))
        if t.get("highway") in ("path", "footway", "track", "steps", "bridleway"):
            paths.append((nlat, nlon))
    for w in root.findall("way"):
        t = {tag.get("k"): tag.get("v") for tag in w.findall("tag")}
        if t.get("amenity") == "parking":
            for nd in w.findall("nd"):
                h = nodes.get(nd.get("ref"))
                if h:
                    parking.append((h[0], h[1]))
                    break
        if t.get("highway") in ("path", "footway", "track", "steps", "bridleway"):
            for nd in w.findall("nd"):
                h = nodes.get(nd.get("ref"))
                if h:
                    paths.append((h[0], h[1]))
    return np.array(paths) if paths else None, parking


def refine_site(dem: Dem, veg: Veg, lat: float, lon: float, az_axis: np.ndarray,
                sun_track: list, grid: HorizonGrid, r_m: float = 250.0):
    """Return dict for the best exact standing point near (lat, lon)."""
    m_lon = 111_412.84 * np.cos(np.radians(lat)) - 93.5 * np.cos(np.radians(3 * lat))
    m_lat = 111_132.0
    cols = np.arange(-r_m, r_m + 1, 30.0)
    cc, rr = np.meshgrid(cols, cols)
    keep = cc**2 + rr**2 <= r_m**2
    clons = lon + cc[keep] / m_lon
    clats = lat + rr[keep] / m_lat
    elevs = dem.sample_bilinear(*dem.lonlat_to_px(clons, clats))
    ok = np.isfinite(elevs)
    clons, clats, elevs = clons[ok], clats[ok], elevs[ok]

    hor = horizon_profile_batch(dem, clons, clats, grid, obs_h=elevs, batch=64,
                                veg=veg, veg_alpha=ALPHA)
    sun_az = np.array([s[1] for s in sun_track])
    sun_alt = np.array([s[0] for s in sun_track])
    h_at = _interp_along_axis(hor, az_axis, sun_az, wrap=False)
    clr = sun_alt[None, :] - h_at
    min_c = clr.min(axis=1)
    mean_c = clr.mean(axis=1)

    tc = veg.tc_at(clons, clats)
    paths, parking = _paths_and_parking(lat, lon)
    if paths is not None and len(paths):
        px = (paths[:, 1] - lon) * m_lon
        py = (paths[:, 0] - lat) * m_lat
        from scipy.spatial import cKDTree
        tr = cKDTree(np.column_stack([px, py]))
        cx = (clons - lon) * m_lon
        cy = (clats - lat) * m_lat
        dpath, _ = tr.query(np.column_stack([cx, cy]))
    else:
        dpath = np.full(len(clons), 999.0)

    score = (
        np.clip(min_c, -5, 20)
        + 0.35 * np.clip(mean_c, -5, 20)
        - 0.004 * np.clip(dpath - 100, 0, 600)      # prefer within ~100 m of a path
        - 0.10 * np.clip(tc - 20, 0, 100)           # not inside canopy
    )
    i = int(np.argmax(score))

    az_full = np.arange(0, 360, 0.5)
    gfull = HorizonGrid.build(dem, az_full, default_ranges(55.0), lat)
    bare = horizon_profile(dem, clons[i], clats[i], gfull)
    vegp = horizon_profile(dem, clons[i], clats[i], gfull, veg=veg, veg_alpha=ALPHA)

    return dict(
        lon=float(clons[i]), lat=float(clats[i]), elev=float(elevs[i]),
        min_clearance=float(min_c[i]), mean_clearance=float(mean_c[i]),
        d_path_m=float(dpath[i]), tc_obs=float(tc[i]),
        n_parking=len(parking),
        parking=parking[:5],
        az_profile=az_full.tolist(), bare=bare.tolist(), veg=vegp.tolist(),
    )


def refine(name: str, meta: dict, k: int = 10, verbose: bool = True) -> list[dict]:
    with open(OUTPUT_DIR / f"{name}_access.csv") as f:
        rows = list(csv.DictReader(f))[:k]
    geo = compute_eclipse_geometry(meta["lat"], meta["lon"], meta["date"])
    az0, az1 = azimuth_sector(geo, 12.0)
    az_axis = np.arange(az0, az1 + 1e-9, 1.0)
    useful = geo.useful_samples
    sun_track = []
    for s in useful:
        if s.alt_deg <= -0.25:
            continue
        az = s.az_deg
        while az < az0:
            az += 360
        while az > az0 + 360:
            az -= 360
        sun_track.append((s.alt_deg, az))

    mr = meta.get("max_range_km", 55.0)
    dem_path = DEMO_DIR / f"{name}_{meta['radius_km']:.0f}km_h{mr:.0f}km.tif"
    dem = Dem(dem_path)
    d_lat = (meta["radius_km"] + mr) / 111.0
    d_lon = (meta["radius_km"] + mr) / (111.0 * np.cos(np.radians(meta["lat"])))
    veg = Veg.for_bbox((meta["lon"] - d_lon, meta["lat"] - d_lat,
                        meta["lon"] + d_lon, meta["lat"] + d_lat), verbose=verbose)
    grid = HorizonGrid.build(dem, az_axis, default_ranges(mr), meta["lat"])

    out = []
    for j, r in enumerate(rows, 1):
        if verbose:
            print(f"[refine {j}/{len(rows)}] {r['lat']},{r['lon']} …")
        res = refine_site(dem, veg, float(r["lat"]), float(r["lon"]), az_axis,
                          sun_track, grid)
        res["access_rank"] = j
        res["drive_min"] = float(r["drive_min"])
        res["final_score"] = float(r["final_score"])
        res["conf"] = int(r["access_conf"])
        res["n_paths"] = int(r["n_paths"])
        out.append(res)

    with open(OUTPUT_DIR / f"{name}_refined.json", "w") as f:
        json.dump(out, f)
    if verbose:
        print(f"wrote {OUTPUT_DIR / f'{name}_refined.json'}")
    return out
