"""Practical-access layer: drive time, parking, walk, public-access proxies.

Sources (free, no keys):
  - OSRM demo (router.project-osrm.org) for drive time/distance matrices.
  - Overpass (OSM) for parking, public paths/footways, and land-designation
    tags near each candidate.

Walking model: Naismith — 5 km/h on the flat + 10 min per 100 m ascent,
distance inflated x1.25 for path sinuosity. Ascent sampled from the DEM
along the straight parking->site line.

Access confidence (0-3, inferred from OSM proxies):
  +1 parking within 1.5 km walk
  +1 a path/footway/track within 300 m of the site (PROW proxy)
  +1 site on tagged open/recreational land (nature reserve / heath / moor /
     national park) OR on open moorland per landcover tags
Everything flagged 'inferred' — ground truth still required.
"""
from __future__ import annotations

import csv
import json
import math
import pathlib
import time as _time

import numpy as np
import requests

from .dem import Dem

OUTPUT_DIR = pathlib.Path(__file__).parent.parent / "output"

OSRM = "https://router.project-osrm.org"
OVERPASS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
OSM_CACHE = pathlib.Path(__file__).parent.parent / "data" / "osm"
OSM_CACHE.mkdir(parents=True, exist_ok=True)

WALK_SPEED_KMH = 5.0
SINUOSITY = 1.25


def _osrm_table(origin: tuple[float, float], dests: list[tuple[float, float]]):
    """Return list of (drive_s, drive_m) origin->each dest."""
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in [origin] + dests)
    url = f"{OSRM}/table/v1/driving/{coords}?sources=0&annotations=duration,distance"
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != "Ok":
        raise RuntimeError(f"OSRM: {j.get('code')} {j.get('message')}")
    dur = j["durations"][0][1:]
    dist = j["distances"][0][1:]
    return [(d, m) for d, m in zip(dur, dist)]


def _overpass(query: str, retries: int = 2):
    last = None
    for i in range(retries):
        mirror = OVERPASS[i % len(OVERPASS)]
        try:
            r = requests.post(mirror, data={"data": query}, timeout=45,
                              headers={"User-Agent": "eclipse-finder/0.1 (geospatial research)"})
            if r.status_code in (429, 504, 406, 500):
                _time.sleep(1.5)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            _time.sleep(1.5)
    raise RuntimeError(f"overpass failed: {last}")


def _center(el: dict) -> tuple[float, float]:
    if el["type"] == "node":
        return el["lat"], el["lon"]
    return el.get("center", {}).get("lat"), el.get("center", {}).get("lon")


def _osm_api_map(lat: float, lon: float) -> dict:
    """Fallback source: official OSM API /map for a ~4x4 km box.
    Same schema as osm_near()."""
    import xml.etree.ElementTree as ET
    nodes = {}
    root = None
    for dy in (0.02, 0.012, 0.007):
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
        raise RuntimeError("OSM API: area too dense even at smallest box")
    nodes = {}
    for n in root.findall("node"):
        nodes[n.get("id")] = (float(n.get("lat")), float(n.get("lon")),
                              {t.get("k"): t.get("v") for t in n.findall("tag")})
    parking, n_paths = [], 0
    land = {"nature_reserve": 0, "open_land": 0, "national_park": 0, "access_land": 0}
    for nid, (nlat, nlon, t) in nodes.items():
        if t.get("amenity") == "parking" and haversine_m(nlat, nlon, lat, lon) <= R_PARK:
            parking.append([nlat, nlon])
    _PATHS = {"path", "footway", "track", "steps", "bridleway"}
    _LANDNAT = {"heath", "moor", "grassland", "fell"}
    for w in root.findall("way"):
        t = {tag.get("k"): tag.get("v") for tag in w.findall("tag")}
        kind = None
        if t.get("amenity") == "parking":
            kind = "parking"
        elif t.get("highway") in _PATHS:
            kind = "paths"
        elif t.get("leisure") == "nature_reserve" or t.get("natural") in _LANDNAT \
                or t.get("designation") == "access_land":
            kind = "land"
        if kind is None:
            continue
        r_lim = {"parking": R_PARK, "paths": R_PATH, "land": R_LAND}[kind]
        for nd in w.findall("nd"):
            hit = nodes.get(nd.get("ref"))
            if hit and haversine_m(hit[0], hit[1], lat, lon) <= r_lim:
                if kind == "parking":
                    parking.append([hit[0], hit[1]])
                elif kind == "paths":
                    n_paths += 1
                else:
                    if t.get("leisure") == "nature_reserve":
                        land["nature_reserve"] += 1
                    elif t.get("designation") == "access_land":
                        land["access_land"] += 1
                    else:
                        land["open_land"] += 1
                break
    return {"parking": parking, "n_paths": n_paths, "land": land}


def osm_near(lat: float, lon: float, verbose: bool = False) -> dict | None:
    """One small combined Overpass query per site, cached on disk.
    Returns {parking: [(lat,lon),...], n_paths:int, land:{...}} or None if
    all mirrors failed (caller should defer the site)."""
    key = f"{lat:.5f}_{lon:.5f}"
    cache = OSM_CACHE / f"{key}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    q = (
        '[out:json][timeout:60];('
        f'nwr["amenity"="parking"](around:1500,{lat},{lon});'
        f'way["highway"~"^(path|footway|track|steps|bridleway)$"](around:300,{lat},{lon});'
        f'way["leisure"="nature_reserve"](around:400,{lat},{lon});'
        f'relation["leisure"="nature_reserve"](around:400,{lat},{lon});'
        f'way["natural"~"^(heath|moor|grassland|fell)$"](around:400,{lat},{lon});'
        f'relation["boundary"="national_park"](around:400,{lat},{lon});'
        f'way["designation"="access_land"](around:400,{lat},{lon});'
        ');out center 150;'
    )
    try:
        j = _overpass(q, retries=3)
    except RuntimeError as e:
        if verbose:
            print(f"    overpass failed for {key}; trying api.openstreetmap.org …")
        try:
            result = _osm_api_map(lat, lon)
            cache.write_text(json.dumps(result))
            return result
        except Exception as e2:  # noqa: BLE001
            if verbose:
                print(f"    deferred {key}: {e2}")
            return None
    parking, n_paths = [], 0
    land = {"nature_reserve": 0, "open_land": 0, "national_park": 0, "access_land": 0}
    for el in j.get("elements", []):
        t = el.get("tags", {})
        if t.get("amenity") == "parking":
            c = _center(el)
            if c[0] is not None:
                parking.append(c)
        if el["type"] == "way" and t.get("highway") in ("path", "footway", "track", "steps", "bridleway"):
            n_paths += 1
        if t.get("leisure") == "nature_reserve":
            land["nature_reserve"] += 1
        if t.get("natural") in ("heath", "moor", "grassland", "fell"):
            land["open_land"] += 1
        if t.get("boundary") == "national_park":
            land["national_park"] += 1
        if t.get("designation") == "access_land":
            land["access_land"] += 1
    result = {"parking": parking, "n_paths": n_paths, "land": land}
    cache.write_text(json.dumps(result))
    return result


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    p = math.radians
    dlat = p(lat2 - lat1)
    dlon = p(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p(lat1)) * math.cos(p(lat2)) * math.sin(dlon / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(a))


def walk_ascent(dem: Dem, lat1, lon1, lat2, lon2, n: int = 40) -> float:
    lats = np.linspace(lat1, lat2, n)
    lons = np.linspace(lon1, lon2, n)
    h = dem.sample_bilinear(*dem.lonlat_to_px(lons, lats))
    h = np.nan_to_num(h)
    return float(np.sum(np.clip(np.diff(h), 0, None)))


def naismith_min(dist_m: float, ascent_m: float) -> float:
    return dist_m / 1000 / WALK_SPEED_KMH * 60 + ascent_m / 100 * 10


def analyse_candidates(name: str, dem: Dem, origin: tuple[float, float],
                       k: int = 20, verbose: bool = True) -> list[dict]:
    with open(OUTPUT_DIR / f"{name}_candidates.csv") as f:
        rows = list(csv.DictReader(f))
    rows = rows[:k]
    dests = [(float(r["lat"]), float(r["lon"])) for r in rows]
    if verbose:
        print(f"[access] OSRM drive times for {len(dests)} candidates …")
    drives = _osrm_table(origin, dests)

    out, deferred = [], []
    consec = 0
    for i, (r, (lat, lon)) in enumerate(zip(rows, dests), 1):
        drive_s, drive_m = drives[i - 1]
        drive_min = (drive_s or 1e9) / 60

        osm = osm_near(lat, lon, verbose)
        if osm is None:
            deferred.append((lat, lon))
            consec += 1
            if consec >= 4:
                if verbose:
                    print("Overpass congested — aborting; re-run `just access` later.")
                break
            continue
        consec = 0
        parks = osm["parking"]
        if parks:
            p_lat, p_lon = min(parks, key=lambda p: haversine_m(p[0], p[1], lat, lon))
        else:
            p_lat = p_lon = None
        if p_lat is not None:
            walk_d = haversine_m(p_lat, p_lon, lat, lon) * SINUOSITY
        else:
            walk_d = float("nan")
        ascent = walk_ascent(dem, p_lat, p_lon, lat, lon) if p_lat is not None else float("nan")
        walk_min = naismith_min(walk_d, ascent) if p_lat is not None else float("nan")

        n_paths = osm["n_paths"]
        land = osm["land"]

        conf = 0
        if p_lat is not None and walk_d <= 1500:
            conf += 1
        if n_paths > 0:
            conf += 1
        if any(land.values()):
            conf += 1

        geom = float(r["score"])
        final = (
            geom
            - 0.15 * max(0.0, drive_min - 40)
            - 0.20 * max(0.0, (walk_min if walk_min == walk_min else 30) - 15)
            - 0.5 * (3 - conf)
        )
        out.append(dict(
            rank_geom=i, lat=lat, lon=lon, elev=float(r["elev"]),
            min_clearance=float(r["min_clearance"]), veg_risk=float(r["veg_risk"]),
            drive_min=round(drive_min, 1),
            park_km=round(haversine_m(p_lat, p_lon, lat, lon) / 1000, 2) if p_lat else None,
            walk_min=round(walk_min, 1) if walk_min == walk_min else None,
            ascent_m=round(ascent) if ascent == ascent else None,
            n_paths=n_paths, land=land, access_conf=conf,
            geom_score=round(geom, 2), final_score=round(final, 2),
        ))
        if verbose:
            print(f"  #{i:2d} {lat:8.5f},{lon:8.5f} drive {drive_min:5.1f}m "
                  f"walk {walk_min if walk_min == walk_min else -1:5.1f}m "
                  f"paths {n_paths:2d} conf {conf} final {final:6.2f}")

    out.sort(key=lambda d: -d["final_score"])
    if out:
        with open(OUTPUT_DIR / f"{name}_access.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
            w.writeheader()
            w.writerows(out)
    if verbose:
        print(f"wrote {OUTPUT_DIR / f'{name}_access.csv'}")
        if deferred:
            print(f"deferred {len(deferred)} sites (Overpass busy) — re-run to fill: "
                  + ", ".join(f"{la:.4f},{lo:.4f}" for la, lo in deferred))
    return out
