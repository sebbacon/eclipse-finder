"""Candidate generation, grid search and local-maximum extraction."""
from __future__ import annotations

import csv
import dataclasses
import json
import pathlib

import numpy as np

from .dem import Dem, fetch_dem
from .horizon import (HorizonGrid, default_ranges, horizon_profile,
                      horizon_profile_batch, score_candidates, _interp_along_axis)
from .solar import EclipseGeometry, azimuth_sector, compute_eclipse_geometry

OUTPUT_DIR = pathlib.Path(__file__).parent.parent / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


@dataclasses.dataclass
class SearchConfig:
    lat: float
    lon: float
    date: str
    tz: str = "Europe/London"
    radius_km: float = 40.0
    grid_m: float = 250.0
    margin_deg: float = 12.0
    max_range_km: float = 55.0
    az_step_deg: float = 1.0
    name: str = "site"
    min_elev_m: float = 30.0  # skip obvious valley floors/built-up low ground


def run_search(cfg: SearchConfig, verbose: bool = True) -> list[dict]:
    """Full pipeline: eclipse geometry -> DEM -> grid -> horizons -> scores."""
    if verbose:
        print(f"[1/5] eclipse geometry for {cfg.lat:.4f},{cfg.lon:.4f} on {cfg.date} …")
    geo = compute_eclipse_geometry(cfg.lat, cfg.lon, cfg.date, tz=cfg.tz)
    az0, az1 = azimuth_sector(geo, cfg.margin_deg)
    az_axis = np.arange(az0, az1 + 1e-9, cfg.az_step_deg)
    useful = geo.useful_samples
    # sun track for scoring: every minute, sun up, magnitude>0.
    # normalise sun azimuths into the (possibly 360-crossing) sector range
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
    if verbose:
        print(f"      azimuth sector to keep clear: {az0:.1f}°–{az1:.1f}° "
              f"({len(sun_track)} scored minutes, max mag {geo.max_magnitude*100:.1f}%)")

    if verbose:
        print(f"[2/5] DEM over ±{cfg.radius_km:.0f} km …")
    d_lat = cfg.radius_km / 111.0 + cfg.max_range_km / 111.0
    d_lon = (cfg.radius_km + cfg.max_range_km) / (111.0 * np.cos(np.radians(cfg.lat)))
    bbox = (cfg.lon - d_lon, cfg.lat - d_lat, cfg.lon + d_lon, cfg.lat + d_lat)
    dem_path = fetch_dem(bbox, f"{cfg.name}_{cfg.radius_km:.0f}km_h{cfg.max_range_km:.0f}km")
    dem = Dem(dem_path)

    if verbose:
        print(f"[3/5] building candidate grid ({cfg.grid_m:.0f} m) …")
    m_lon, m_lat = 111320 * np.cos(np.radians(cfg.lat)), 110574
    cols = np.arange(-cfg.radius_km * 1000, cfg.radius_km * 1000 + 1, cfg.grid_m)
    rows = np.arange(-cfg.radius_km * 1000, cfg.radius_km * 1000 + 1, cfg.grid_m)
    cc, rr = np.meshgrid(cols, rows)
    keep = cc**2 + rr**2 <= (cfg.radius_km * 1000) ** 2
    east, north = cc[keep], rr[keep]
    lons = cfg.lon + east / m_lon
    lats = cfg.lat + north / m_lat
    elevs = dem.sample_bilinear(*dem.lonlat_to_px(lons, lats))
    valid = np.isfinite(elevs) & (elevs >= cfg.min_elev_m)
    lons, lats, elevs = lons[valid], lats[valid], elevs[valid]
    if verbose:
        print(f"      {len(lons):,} candidates on grid")

    if verbose:
        print(f"[4/6] horizon profiles ({len(az_axis)} azimuths × "
              f"{len(default_ranges(cfg.max_range_km))} ranges), bare + veg-corrected …")
    from .vegetation import Veg, ALPHA
    veg = Veg.for_bbox(bbox, verbose=verbose)
    grid = HorizonGrid.build(dem, az_axis, default_ranges(cfg.max_range_km), cfg.lat)
    horizon_bare = horizon_profile_batch(dem, lons, lats, grid, obs_h=elevs, batch=96)
    horizon_veg = horizon_profile_batch(dem, lons, lats, grid, obs_h=elevs, batch=96,
                                        veg=veg, veg_alpha=ALPHA)

    if verbose:
        print("[5/6] scoring …")
    metrics = score_candidates(lons, lats, horizon_veg, az_axis, sun_track, elevs)
    min_c, mean_c, low_frac, breadth, score = (metrics[:, i] for i in range(5))
    metrics_bare = score_candidates(lons, lats, horizon_bare, az_axis, sun_track, elevs)
    min_c_bare = metrics_bare[:, 0]
    veg_risk = min_c_bare - min_c

    # observer-in-woodland mask: local effective tree cover at the standing point
    tc_obs = veg.tc_at(lons, lats)
    wood_penalty = np.clip((tc_obs - 30) / 70, 0, 1) * 6.0
    score = score - wood_penalty

    # local maxima on the score field (suppress neighbours within 750 m)
    results = []
    order = np.argsort(-score)
    taken_lons, taken_lats = [], []
    for i in order:
        if len(results) >= 60:
            break
        if taken_lons:
            d2 = (np.array(taken_lons) - lons[i]) ** 2 * m_lon**2 + \
                 (np.array(taken_lats) - lats[i]) ** 2 * m_lat**2
            if (d2 < 750**2).any():
                continue
        taken_lons.append(lons[i])
        taken_lats.append(lats[i])
        results.append(dict(
            lon=float(lons[i]), lat=float(lats[i]), elev=float(elevs[i]),
            min_clearance=float(min_c[i]), min_clear_bare=float(min_c_bare[i]),
            veg_risk=float(veg_risk[i]), tc_obs=float(tc_obs[i]),
            mean_clearance=float(mean_c[i]),
            low_horizon_frac=float(low_frac[i]), breadth_deg=float(breadth[i]),
            score=float(score[i]),
        ))

    out = OUTPUT_DIR / f"{cfg.name}_candidates.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)
    meta = dict(
        lat=cfg.lat, lon=cfg.lon, date=cfg.date, az_sector=[float(az0), float(az1)],
        max_magnitude=geo.max_magnitude,
        contacts={k: (v.isoformat() if hasattr(v, "isoformat") else v)
                  for k, v in geo.contacts.items()},
        n_candidates=int(len(lons)), grid_m=cfg.grid_m, radius_km=cfg.radius_km,
    )
    (OUTPUT_DIR / f"{cfg.name}_meta.json").write_text(json.dumps(meta, indent=2))
    if verbose:
        print(f"      wrote {out} ({len(results)} local maxima)")
    return results


def full_profile(dem_path: str | pathlib.Path, lon: float, lat: float,
                 max_range_km: float = 55.0, az_step: float = 0.5, veg=None):
    """Full 360° horizon profile for a single site (for plots / reporting).
    Returns (az_axis, bare_profile); with veg also veg-corrected profile."""
    dem = Dem(dem_path)
    az_axis = np.arange(0, 360, az_step)
    grid = HorizonGrid.build(dem, az_axis, default_ranges(max_range_km), lat)
    bare = horizon_profile(dem, lon, lat, grid)
    if veg is None:
        return az_axis, bare
    return az_axis, bare, horizon_profile(dem, lon, lat, grid, veg=veg)
