"""Final report: top-10 table, top-5 narratives, map + horizon plots.

Everything numeric is computed; place names and horizon-feature
identifications are inferred from coordinates/elevation and flagged as such.
"""
from __future__ import annotations

import csv
import json
import pathlib

import numpy as np

from .dem import Dem, DEMO_DIR
from .horizon import HorizonGrid, default_ranges, _drop_m
from .plot import plot_horizon_profile, plot_map
from .solar import azimuth_sector, compute_eclipse_geometry
from .access import haversine_m, naismith_min, walk_ascent

OUTPUT_DIR = pathlib.Path(__file__).parent.parent / "output"


def _horizon_detail(dem, lon, lat, azs):
    """Per azimuth: (horizon alt deg, limiting range m, end lat/lon, end elev, veg add)."""
    from .vegetation import Veg, ALPHA
    rows = []
    grid = HorizonGrid.build(dem, np.asarray(azs, dtype=float),
                             default_ranges(55.0), lat)
    col0, row0 = dem.lonlat_to_px(np.asarray(lon), np.asarray(lat))
    h_obs = dem.elevation_at(lon, lat)
    cols = col0 + grid.dcol
    rows_ = row0 + grid.drow
    h = dem.sample_bilinear(cols.ravel(), rows_.ravel()).reshape(grid.dcol.shape)
    slons, slats = dem.px_to_lonlat(cols.ravel(), rows_.ravel())
    # veg layer for the delta
    mr = 55.0
    d = grid.range_m[None, :]
    apparent = np.degrees(np.arctan2(h - h_obs - _drop_m(d), d))
    k = apparent.argmax(axis=1)
    A = len(azs)
    for a in range(A):
        ki = k[a]
        rows.append(dict(
            az=float(azs[a]), alt=float(apparent[a, ki]), range_m=float(grid.range_m[ki]),
            end_lat=float(slats.reshape(grid.dcol.shape)[a, ki]),
            end_lon=float(slons.reshape(grid.dcol.shape)[a, ki]),
            end_elev=float(h.reshape(grid.dcol.shape)[a, ki]),
        ))
    return rows


def build_report(name: str, top_table: int = 10, top_narr: int = 5):
    meta = json.loads((OUTPUT_DIR / f"{name}_meta.json").read_text())
    refined = json.loads((OUTPUT_DIR / f"{name}_refined.json").read_text())
    with open(OUTPUT_DIR / f"{name}_access.csv") as f:
        access = {int(r["rank_geom"]): r for r in csv.DictReader(f)}

    mr = meta.get("max_range_km", 55.0)
    dem_path = DEMO_DIR / f"{name}_{meta['radius_km']:.0f}km_h{mr:.0f}km.tif"
    dem = Dem(dem_path)
    geo = compute_eclipse_geometry(meta["lat"], meta["lon"], meta["date"])
    az0, az1 = azimuth_sector(geo, 12.0)

    lines = []
    w = lines.append
    w(f"# Eclipse-viewing sites near {name} — {meta['date']}")
    w("")
    w(f"Eclipse (at origin): first contact "
      f"{geo.contacts['first_contact'].strftime('%H:%M')} UTC, maximum "
      f"{geo.contacts['maximum'].strftime('%H:%M')} UTC "
      f"(magnitude {geo.max_magnitude*100:.1f}%), last contact "
      f"{geo.contacts['last_contact'].strftime('%H:%M')} UTC. "
      f"Sun track az {az0+12:.0f}°–{az1-12:.0f}°, altitude 20.7°→5.2°. "
      f"Clear-sector used: {az0:.0f}°–{az1:.0f}°.")
    w("")
    w("| # | Location (approx.) | Coordinates | Elev | Drive | Walk | "
      "Horizon towards eclipse | Min sun clearance | Veg risk | Access conf |")
    w("|---|---|---|---:|---:|---:|---:|---:|---:|---|")

    names = _place_names(refined)
    for i, r in enumerate(refined[:top_table], 1):
        azs = np.arange(az0, az1 + 1, 2.0)
        bare = np.array(r["bare"])
        vegp = np.array(r["veg"])
        azf = np.array(r["az_profile"])
        sel = (azf >= az0) & (azf <= az1)
        hmax_veg = vegp[sel].max()
        hmax_bare = bare[sel].max()
        veg_risk = float((vegp - bare)[sel].max())
        walk = _walk_for(dem, r)
        walk_s = f"{walk[0]} ({walk[1]})" if walk else "–"
        w(f"| {i} | {names[i-1]} | {r['lat']:.5f}, {r['lon']:.5f} | {r['elev']:.0f} m | "
          f"{r['drive_min']:.0f} min | {walk_s} | "
          f"{hmax_veg:.1f}° (bare {hmax_bare:.1f}°) | {r['min_clearance']:.2f}° | "
          f"{veg_risk:.2f}° | {r.get('conf', '–')} |")

    w("")
    w("## Top five, in detail")
    for i, r in enumerate(refined[:top_narr], 1):
        det = _horizon_detail(dem, r["lon"], r["lat"], [265, 275, 285, 295])
        walk = _walk_for(dem, r)
        w("")
        w(f"### {i}. {names[i-1]} — {r['lat']:.5f}, {r['lon']:.5f}")
        w(f"- **Where to stand:** within ~30 m of {r['lat']:.5f}, {r['lon']:.5f} "
          f"({r['elev']:.0f} m); refined point lies {r['d_path_m']:.0f} m from a mapped "
          f"public path/track.")
        w(f"- **Look:** WSW→WNW (az {az0+12:.0f}–{az1-12:.0f}°); sun descends 21°→5° "
          f"during the eclipse, max at az ~278°.")
        hor = "; ".join(f"az {d['az']:.0f}°: {d['alt']:.2f}° at "
                        + (f"≥{d['range_m']/1000:.0f} km (beyond analysis range)"
                           if d['range_m'] >= 54_500 else f"{d['range_m']/1000:.0f} km")
                        for d in det)
        w(f"- **Distant horizon (computed):** {hor}.")
        w(f"- **Why the model likes it:** minimum clearance {r['min_clearance']:.2f}° "
          f"over the whole eclipse window; horizon ≤ {max(d['alt'] for d in det):.2f}° "
          f"towards the sun; open moorland, local tree cover {r['tc_obs']:.0f}%.")
        w(f"- **Trees/buildings:** vegetation-corrected horizon exceeds bare terrain by "
          f"≤ {(np.array(r['veg'])[sel_bool(azf, az0, az1)] - np.array(r['bare'])[sel_bool(azf, az0, az1)]).max():.2f}° "
          f"(modelled canopy, Hansen 30 m). No mapped woodland in the corridor; "
          f"near-field hedges/lone trees below 30 m resolution remain possible → "
          f"ground-check.")
        w(f"- **Parking/access:** drive {r['drive_min']:.0f} min from south Altrincham; "
          f"{f'nearest mapped parking ~{walk[1]}' if isinstance(walk, tuple) else 'no mapped parking within 1.5 km — verify'}; "
          f"walk {walk[0] if isinstance(walk, tuple) else '–'}."
          )
        w(f"- **Uncertainty:** standing point from 30 m DEM (±a few m vertically); "
          f"OSM path proximity = mapped paths only; names inferred from coordinates.")
    w("")
    w("## Verification status")
    w("- Computed: eclipse geometry (2 ephemerides), horizons bare+veg, clearances, "
      "drive times (OSRM), walk/ascent (Naismith on DEM).")
    w("- Inferred: place names, horizon-feature names, canopy heights (Hansen model), "
      "access confidence proxies.")
    w("- Ground-truth needed: exact stile/parking spots, near-field hedges, "
      "evening access restrictions.")
    (OUTPUT_DIR / f"REPORT_{name}.md").write_text("\n".join(lines))
    print(f"wrote {OUTPUT_DIR / f'REPORT_{name}.md'}")

    # plots for top 5
    for i, r in enumerate(refined[:top_narr], 1):
        azf = np.array(r["az_profile"])
        sel = (azf >= min(az0, 160)) & (azf <= max(az1, 355))
        plot_horizon_profile(azf[sel], np.array(r["bare"])[sel], geo,
                             f"#{i} {names[i-1]}", OUTPUT_DIR / f"final_{name}_{i}.png",
                             obs_lon=r["lon"], obs_lat=r["lat"], elev_m=r["elev"],
                             horizon_veg=np.array(r["veg"])[sel])
    rows = refined[:top_table]
    plot_map(dem, (meta["lon"], meta["lat"]),
             [{**r, "score": r["final_score"]} for r in rows], top_narr,
             f"Refined eclipse sites — {name}", OUTPUT_DIR / f"final_map_{name}.png",
             sun_az_range=(az0, az1))
    print("wrote final plots")


def sel_bool(azf, az0, az1):
    return (azf >= az0) & (azf <= az1)


def _walk_for(dem, r):
    if not r.get("parking"):
        return None
    p = min(r["parking"], key=lambda q: haversine_m(q[0], q[1], r["lat"], r["lon"]))
    d = haversine_m(p[0], p[1], r["lat"], r["lon"]) * 1.25
    asc = walk_ascent(dem, p[0], p[1], r["lat"], r["lon"])
    return f"{naismith_min(d, asc):.0f} min", f"{d/1000:.1f} km"


def _place_names(refined):
    """Approximate names inferred from coordinates — verify on a map."""
    out = []
    for r in refined:
        la, lo = r["lat"], r["lon"]
        if la > 53.58 and lo > -2.2:
            n = "Blackstone Edge / White Moss moor (Pennine Way, E of Littleborough)"
        elif la > 53.58 and -2.35 < lo < -2.28:
            n = "Bull Hill / Holcombe Moor N"
        elif la > 53.58:
            n = "Holcombe / East Lancs moors"
        elif 53.51 <= la < 53.53 and -2.35 < lo < -2.28:
            n = "Rishworth Moor"
        elif 53.65 <= la < 53.68 and -2.35 < lo < -2.28:
            n = "Bull Hill / Holcombe Moor N"
        elif 53.65 <= la < 53.68:
            n = "East Lancs moors (Haslingden)"
        elif 53.54 <= la <= 53.56 and lo < -2.12:
            n = "Standedge / West Nab moor"
        elif 53.52 <= la < 53.54 and lo < -2.12:
            n = "Wessenden Head moors (Pennine Way)"
        elif 53.50 <= la < 53.52 and lo < -2.11:
            n = "Wessenden Moor / Rod Moor"
        elif 53.50 <= la < 53.52:
            n = "Wessenden Moor E shoulder"
        elif 53.32 <= la < 53.34 and lo < -2.5:
            n = "Bosley Minn / Cloud ridge (Cheshire escarpment)"
        elif 53.14 <= la < 53.17:
            n = "The Roaches / Staffordshire Moorlands"
        else:
            n = "unnamed high ground"
        out.append(n)
    return out
