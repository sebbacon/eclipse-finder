"""CLI: drive everything for any lat/lon/date.

Subcommands:
  sun       -- print eclipse geometry / sun track
  dem       -- fetch DEM tiles for a bounding radius
  horizon   -- horizon profile for one point (+ sun overlay plot)
  search    -- full grid search, writes CSV + meta JSON
  plot      -- map + horizon plots for the top candidates
"""
from __future__ import annotations

import argparse
import json
import pathlib

OUTPUT_DIR = pathlib.Path(__file__).parent.parent / "output"


def cmd_sun(a):
    from .solar import azimuth_sector, compute_eclipse_geometry, format_report
    geo = compute_eclipse_geometry(a.lat, a.lon, a.date, tz=a.tz)
    print(format_report(geo, tz=a.tz))
    az0, az1 = azimuth_sector(geo, a.margin)
    print(f"\nAzimuth sector to keep clear (±{a.margin:.0f}° margin): {az0:.1f}° – {az1:.1f}°")


def cmd_dem(a):
    import numpy as np
    from .dem import fetch_dem
    d_lat = a.radius_km / 111.0
    d_lon = a.radius_km / (111.0 * np.cos(np.radians(a.lat)))
    bbox = (a.lon - d_lon, a.lat - d_lat, a.lon + d_lon, a.lat + d_lat)
    path = fetch_dem(bbox, f"{a.name}_{a.radius_km:.0f}km")
    print(path)


def cmd_horizon(a):
    import numpy as np
    from .dem import Dem, fetch_dem
    from .horizon import HorizonGrid, default_ranges, horizon_profile
    from .solar import azimuth_sector, compute_eclipse_geometry
    from .plot import plot_horizon_profile

    plat = a.plat if a.plat is not None else a.lat
    plon = a.plon if a.plon is not None else a.lon
    if a.radius_km:
        rng = a.max_range_km + 5.0
        d_lat = rng / 111.0
        d_lon = rng / (111.0 * np.cos(np.radians(plat)))
        bbox = (plon - d_lon, plat - d_lat, plon + d_lon, plat + d_lat)
        dem_path = fetch_dem(bbox, f"{a.name}_pt_{abs(plat):.3f}N{abs(plon):.3f}E")
    else:
        dem_path = a.dem
    dem = Dem(dem_path)
    az_axis = np.arange(0, 360, 0.5)
    grid = HorizonGrid.build(dem, az_axis, default_ranges(a.max_range_km), plat)
    from .vegetation import Veg, ALPHA
    rng = a.max_range_km + 5.0
    vbbox = (plon - rng / (111.0 * np.cos(np.radians(plat))), plat - rng / 111.0,
             plon + rng / (111.0 * np.cos(np.radians(plat))), plat + rng / 111.0)
    veg = Veg.for_bbox(vbbox)
    prof = horizon_profile(dem, plon, plat, grid)
    prof_veg = horizon_profile(dem, plon, plat, grid, veg=veg, veg_alpha=ALPHA)
    h_obs = dem.elevation_at(plon, plat)
    tc = float(veg.tc_at(np.asarray([plon]), np.asarray([plat]))[0])
    print(f"observer: {plat:.6f}, {plon:.6f}  elev {h_obs:.1f} m  local tree cover {tc:.0f}%")
    print(f"horizon max {prof.max():.2f}° at az {az_axis[prof.argmax()]:.1f}, "
          f"min {prof.min():.2f}° at az {az_axis[prof.argmin()]:.1f}")
    print(f"{'az':>4} {'bare':>6} {'veg':>6}")
    for az in (250, 260, 270, 280, 290, 300, 310, 320):
        i = np.argmin(np.abs(az_axis - az))
        print(f"{az:4d} {prof[i]:6.2f} {prof_veg[i]:6.2f}")

    geo = compute_eclipse_geometry(a.lat, a.lon, a.date, tz=a.tz)
    az0, az1 = azimuth_sector(geo, 15.0)
    # widen to a nice western view for the plot
    plot_az0, plot_az1 = min(az0, 180), max(az1, 350)
    sel = (az_axis >= plot_az0) & (az_axis <= plot_az1)
    out = OUTPUT_DIR / f"horizon_{a.name}.png"
    plot_horizon_profile(az_axis[sel], prof[sel], geo, f"Horizon @ {a.name}", out,
                         obs_lon=plon, obs_lat=plat, elev_m=h_obs,
                         horizon_veg=prof_veg[sel])
    print(f"wrote {out}")


def cmd_search(a):
    from .search import SearchConfig, run_search
    cfg = SearchConfig(lat=a.lat, lon=a.lon, date=a.date, tz=a.tz,
                       radius_km=a.radius_km, grid_m=a.grid_m, name=a.name,
                       margin_deg=a.margin, max_range_km=a.max_range_km)
    results = run_search(cfg)
    print(f"\nTop {min(a.top, len(results))} candidates:")
    hdr = (f"{'#':>2} {'lat':>9} {'lon':>9} {'elev':>6} {'minClr':>7} {'bareClr':>8} "
           f"{'vegRisk':>8} {'tcObs':>6} {'breadth':>8} {'score':>7}")
    print(hdr)
    for i, r in enumerate(results[: a.top], 1):
        print(f"{i:2d} {r['lat']:9.5f} {r['lon']:9.5f} {r['elev']:6.0f} "
              f"{r['min_clearance']:7.2f} {r['min_clear_bare']:8.2f} "
              f"{r['veg_risk']:8.2f} {r['tc_obs']:6.0f} "
              f"{r['breadth_deg']:8.1f} {r['score']:7.2f}")


def cmd_plot(a):
    import csv
    import numpy as np
    from .dem import Dem, DEMO_DIR
    from .plot import plot_horizon_profile, plot_map
    from .solar import azimuth_sector, compute_eclipse_geometry
    from .search import full_profile

    with open(OUTPUT_DIR / f"{a.name}_candidates.csv") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ("lon", "lat", "elev", "min_clearance", "mean_clearance",
                  "low_horizon_frac", "breadth_deg", "score"):
            r[k] = float(r[k])
    meta = json.loads((OUTPUT_DIR / f"{a.name}_meta.json").read_text())
    dem_path = DEMO_DIR / f"{a.name}_{meta['radius_km']:.0f}km_h{meta.get('max_range_km', 55):.0f}km.tif"
    dem = Dem(dem_path)
    geo = compute_eclipse_geometry(meta["lat"], meta["lon"], meta["date"])
    from .vegetation import Veg
    mr = meta.get("max_range_km", 55.0)
    d_lat = (meta["radius_km"] + mr) / 111.0
    d_lon = (meta["radius_km"] + mr) / (111.0 * np.cos(np.radians(meta["lat"])))
    veg = Veg.for_bbox((meta["lon"] - d_lon, meta["lat"] - d_lat,
                        meta["lon"] + d_lon, meta["lat"] + d_lat))

    out = OUTPUT_DIR / f"map_{a.name}.png"
    plot_map(dem, (meta["lon"], meta["lat"]), rows, a.top,
             f"Eclipse-viewing candidates — {a.name}", out,
             sun_az_range=tuple(meta["az_sector"]))
    print(f"wrote {out}")

    for i, r in enumerate(rows[: a.top], 1):
        az_axis, prof, prof_veg = full_profile(dem_path, r["lon"], r["lat"],
                                               max_range_km=mr, veg=veg)
        az0, az1 = azimuth_sector(geo, 25.0)
        plot_az0, plot_az1 = min(az0, 160), max(az1, 355)
        sel = (az_axis >= plot_az0) & (az_axis <= plot_az1)
        outp = OUTPUT_DIR / f"horizon_{a.name}_{i:02d}.png"
        plot_horizon_profile(az_axis[sel], prof[sel], geo, f"#{i} horizon", outp,
                             obs_lon=r["lon"], obs_lat=r["lat"], elev_m=r["elev"],
                             horizon_veg=prof_veg[sel])
        print(f"wrote {outp}")


def cmd_ukmap(a):
    from .ukwebmap import build_ukmap
    build_ukmap(a.date)


def cmd_tiles(a):
    from .tiles import build_tiles
    build_tiles()


def cmd_webmap(a):
    from .webmap import build_webmap
    build_webmap(a.name)


def cmd_report(a):
    from .report import build_report
    build_report(a.name)


def cmd_refine(a):
    import json
    from .refine import refine
    meta = json.loads((OUTPUT_DIR / f"{a.name}_meta.json").read_text())
    out = refine(a.name, meta, k=a.top)
    for i, r in enumerate(out, 1):
        print(f"{i:2d} {r['lat']:.5f},{r['lon']:.5f} elev {r['elev']:5.0f}m "
              f"minClr {r['min_clearance']:5.2f} d_path {r['d_path_m']:5.0f}m "
              f"tc {r['tc_obs']:3.0f} drive {r['drive_min']:4.1f}")


def cmd_osm(a):
    import csv
    from .osm_local import PBF_PATH, build_access_cache
    if not PBF_PATH.exists():
        raise SystemExit(
            f"PBF missing: {PBF_PATH}\n"
            "download with: curl -L -C - -o data/osm/great-britain.osm.pbf "
            "https://download.geofabrik.de/europe/great-britain-latest.osm.pbf")
    with open(OUTPUT_DIR / f"{a.name}_candidates.csv") as f:
        rows = list(csv.DictReader(f))[: a.top]
    sites = [(float(r["lat"]), float(r["lon"])) for r in rows]
    build_access_cache(sites)


def cmd_access(a):
    import json
    from .dem import Dem, DEMO_DIR
    from .access import analyse_candidates
    meta = json.loads((OUTPUT_DIR / f"{a.name}_meta.json").read_text())
    mr = meta.get("max_range_km", 55.0)
    dem_path = DEMO_DIR / f"{a.name}_{meta['radius_km']:.0f}km_h{mr:.0f}km.tif"
    dem = Dem(dem_path)
    rows = analyse_candidates(a.name, dem, (meta["lat"], meta["lon"]), k=a.top)
    print(f"\nRe-ranked by geometry + access:")
    print(f"{'#':>2} {'lat':>9} {'lon':>9} {'drive':>6} {'walk':>5} {'asc':>5} "
          f"{'paths':>5} {'conf':>4} {'minClr':>7} {'final':>6}")
    for i, r in enumerate(rows[: a.show], 1):
        w = r["walk_min"] if r["walk_min"] is not None else -1
        print(f"{i:2d} {r['lat']:9.5f} {r['lon']:9.5f} {r['drive_min']:6.1f} {w:5.1f} "
              f"{r['ascent_m'] if r['ascent_m'] is not None else -1:5d} "
              f"{r['n_paths']:5d} {r['access_conf']:4d} {r['min_clearance']:7.2f} {r['final_score']:6.2f}")


def main():
    p = argparse.ArgumentParser(prog="eclipse_finder")
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lon", type=float, required=True)
    p.add_argument("--date", default="2026-08-12")
    p.add_argument("--tz", default="Europe/London")
    p.add_argument("--name", default="site")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sun"); s.set_defaults(fn=cmd_sun)
    s.add_argument("--margin", type=float, default=12.0)

    s = sub.add_parser("dem"); s.set_defaults(fn=cmd_dem)
    s.add_argument("--radius-km", type=float, default=45.0)

    s = sub.add_parser("horizon"); s.set_defaults(fn=cmd_horizon)
    s.add_argument("--plat", type=float, default=None, help="observer lat (default: origin)")
    s.add_argument("--plon", type=float, default=None, help="observer lon (default: origin)")
    s.add_argument("--radius-km", type=float, default=None)
    s.add_argument("--dem", default=None)
    s.add_argument("--max-range-km", type=float, default=55.0)

    s = sub.add_parser("search"); s.set_defaults(fn=cmd_search)
    s.add_argument("--radius-km", type=float, default=40.0)
    s.add_argument("--grid-m", type=float, default=250.0)
    s.add_argument("--margin", type=float, default=12.0)
    s.add_argument("--max-range-km", type=float, default=55.0)
    s.add_argument("--top", type=int, default=15)

    s = sub.add_parser("plot"); s.set_defaults(fn=cmd_plot)
    s.add_argument("--top", type=int, default=5)

    s = sub.add_parser("ukmap"); s.set_defaults(fn=cmd_ukmap)

    s = sub.add_parser("tiles"); s.set_defaults(fn=cmd_tiles)

    s = sub.add_parser("webmap"); s.set_defaults(fn=cmd_webmap)

    s = sub.add_parser("report"); s.set_defaults(fn=cmd_report)

    s = sub.add_parser("refine"); s.set_defaults(fn=cmd_refine)
    s.add_argument("--top", type=int, default=10)

    s = sub.add_parser("osm"); s.set_defaults(fn=cmd_osm)
    s.add_argument("--top", type=int, default=20)

    s = sub.add_parser("access"); s.set_defaults(fn=cmd_access)
    s.add_argument("--top", type=int, default=20)
    s.add_argument("--show", type=int, default=15)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
