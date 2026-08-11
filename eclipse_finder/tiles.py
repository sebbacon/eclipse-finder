"""UK-wide tile pyramid for the GitHub-Pages explorer.

Two levels, fixed global grid (top-left origin), 256 px tiles, gzip raw:
  60 m  terrain(int16) + canopy(u8)   for rays <= 15 km from the click
  180 m terrain(int16) + canopy(u8)   beyond, to 55 km
Tiles that are all sea/zero are not written. Buildings are NOT in this
pyramid (per-origin bundles carry them).

Output: pages/tiles/{m}/{ix}_{iy}.gz  +  pages/tiles/meta.json
"""
from __future__ import annotations

import gzip
import json
import pathlib

import numpy as np

from .dem import DEMO_DIR, Dem, _download, cop30_tile_url, tile_name
from .vegetation import Veg

OUTPUT_DIR = pathlib.Path(__file__).parent.parent / "pages"
TILE = 256
GB_BBOX = (-9.0, 49.0, 2.0, 61.0)  # w s e n
LON0, LAT0 = -9.0, 61.0            # global grid top-left

_M_LAT = 111_306.0
_M_LON = 65_578.0  # at ~54N


def levels():
    return [
        dict(m=60, resLat=60 / _M_LAT, resLon=60 / _M_LON),
        dict(m=180, resLat=180 / _M_LAT, resLon=180 / _M_LON),
    ]


def _tile_bounds(ix, iy, lv):
    w = LON0 + ix * TILE * lv["resLon"]
    n = LAT0 - iy * TILE * lv["resLat"]
    return w, n - TILE * lv["resLat"], w + TILE * lv["resLon"], n


def build_tiles(verbose: bool = True):
    import rasterio  # noqa: F401  (dem/veg use it)

    out = OUTPUT_DIR / "tiles"
    meta = dict(lon0=LON0, lat0=LAT0, tile=TILE, d_near=15000,
                levels=[dict(m=l["m"], resLon=l["resLon"], resLat=l["resLat"])
                        for l in levels()])
    w0, s0, e0, n0 = GB_BBOX
    written = 0
    for lat in range(int(s0), int(n0)):
        for lon in range(int(w0), int(e0)):
            tname = tile_name(lat, lon)
            dest = DEMO_DIR / f"cop30_{tname}.tif"
            if not dest.exists():
                url = cop30_tile_url(lat, lon)
                import requests
                head = requests.head(url, timeout=30)
                if head.status_code != 200:
                    continue  # sea cell
                _download(url, dest)
            dem = Dem(dest)
            if float(np.nanmax(dem.a)) <= 0:
                continue  # all-sea cell
            try:
                veg = Veg.for_bbox((lon, lat, lon + 1, lat + 1), verbose=False)
                if veg.h_can.size == 0:
                    veg = None
            except Exception as e:  # noqa: BLE001
                if verbose:
                    print(f"[tiles] no veg for {tname}: {e}")
                veg = None
            for lv in levels():
                written += _cell_tiles(dem, veg, lv, out, verbose)
            if verbose:
                print(f"[tiles] cell {tname} done ({written} tiles)")
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    if verbose:
        total = sum(p.stat().st_size for p in out.rglob("*.gz"))
        print(f"[tiles] wrote {written} tiles, {total/1e6:.1f} MB gzipped")
    return written


def _cell_tiles(dem: Dem, veg: Veg | None, lv: dict, out: pathlib.Path,
                verbose: bool) -> int:
    written = 0
    # tile index range intersecting this 1-deg cell
    ix0 = int(np.floor((dem.lon0 - LON0) / (TILE * lv["resLon"])))
    ix1 = int(np.floor((dem.lon0 + dem.w * dem.res - LON0) / (TILE * lv["resLon"])))
    iy0 = int(np.floor((LAT0 - dem.lat0) / (TILE * lv["resLat"])))
    iy1 = int(np.floor((LAT0 - (dem.lat0 - dem.h * dem.res_lat)) / (TILE * lv["resLat"])))
    cc = np.arange(TILE) + 0.5
    for ix in range(ix0, ix1 + 1):
        for iy in range(iy0, iy1 + 1):
            w, s, e, n = _tile_bounds(ix, iy, lv)
            lons = LON0 + (ix * TILE + cc) * lv["resLon"]
            lats = LAT0 - (iy * TILE + cc) * lv["resLat"]
            LON, LAT = np.meshgrid(lons, lats)
            col, row = dem.lonlat_to_px(LON, LAT)
            ok = (col >= 0) & (row >= 0) & (col <= dem.w - 1) & (row <= dem.h - 1)
            if not ok.any():
                continue
            t = dem.sample_bilinear(np.clip(col, 0, dem.w - 1),
                                    np.clip(row, 0, dem.h - 1))
            t = np.where(ok, t, 0).astype("<i2")
            if veg is not None:
                c = np.where(ok, veg.canopy_at(LON, LAT), 0)
                c = np.clip(np.round(c), 0, 255).astype(np.uint8)
            else:
                c = np.zeros((TILE, TILE), np.uint8)
            if t.max() <= 0 and c.max() <= 0:
                continue
            d = out / str(lv["m"])
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{ix}_{iy}.gz").write_bytes(
                gzip.compress(t.tobytes() + c.tobytes(), 6))
            written += 1
    return written
