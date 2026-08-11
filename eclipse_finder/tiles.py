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
    dems: dict[tuple[int, int], Dem | None] = {}
    vegs: dict[tuple[int, int], Veg | None] = {}

    def get_dem(cell):
        if cell not in dems:
            lat, lon = cell
            dest = DEMO_DIR / f"cop30_{tile_name(lat, lon)}.tif"
            if not dest.exists():
                url = cop30_tile_url(lat, lon)
                import requests
                if requests.head(url, timeout=30).status_code != 200:
                    dems[cell] = None
                    return None
                _download(url, dest)
            dems[cell] = Dem(dest)
            if len(dems) > 6:
                dems.pop(next(iter(dems)))
        return dems[cell]

    def get_veg(cell):
        if cell not in vegs:
            lat, lon = cell
            try:
                v = Veg.for_bbox((lon, lat, lon + 1, lat + 1), verbose=False)
                vegs[cell] = v if v.h_can.size else None
            except Exception:  # noqa: BLE001
                vegs[cell] = None
            if len(vegs) > 6:
                vegs.pop(next(iter(vegs)))
        return vegs[cell]

    written = 0
    cc = np.arange(TILE) + 0.5
    for lv in levels():
        ix0 = int(np.floor((w0 - LON0) / (TILE * lv["resLon"])))
        ix1 = int(np.floor((e0 - LON0) / (TILE * lv["resLon"])))
        iy0 = int(np.floor((LAT0 - n0) / (TILE * lv["resLat"])))
        iy1 = int(np.floor((LAT0 - s0) / (TILE * lv["resLat"])))
        for iy in range(iy0, iy1 + 1):
            for ix in range(ix0, ix1 + 1):
                w, s, e, n = _tile_bounds(ix, iy, lv)
                lons = LON0 + (ix * TILE + cc) * lv["resLon"]
                lats = LAT0 - (iy * TILE + cc) * lv["resLat"]
                LON, LAT = np.meshgrid(lons, lats)
                t = np.zeros((TILE, TILE), np.float64)
                c = np.zeros((TILE, TILE), np.float64)
                hit = np.zeros((TILE, TILE), bool)
                for cla in (int(np.floor(s)), int(np.floor(n))):
                    for clo in (int(np.floor(w)), int(np.floor(e))):
                        dem = get_dem((cla, clo))
                        if dem is None:
                            continue
                        col, row = dem.lonlat_to_px(LON, LAT)
                        ok = (col >= 0) & (row >= 0) & (col <= dem.w - 1) \
                            & (row <= dem.h - 1) & ~hit
                        if not ok.any():
                            continue
                        t[ok] = dem.sample_bilinear(
                            np.clip(col, 0, dem.w - 1)[ok],
                            np.clip(row, 0, dem.h - 1)[ok])
                        veg = get_veg((cla, clo))
                        if veg is not None:
                            c[ok] = veg.canopy_at(LON[ok], LAT[ok])
                        hit |= ok
                t = np.where(hit, t, 0).astype("<i2")
                c = np.clip(np.round(np.where(hit, c, 0)), 0, 255).astype(np.uint8)
                if t.max() <= 0 and c.max() <= 0:
                    continue
                d = out / str(lv["m"])
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{ix}_{iy}.gz").write_bytes(
                    gzip.compress(t.tobytes() + c.tobytes(), 6))
                written += 1
            if verbose and iy % 5 == 0:
                print(f"[tiles] {lv['m']} m row {iy}/{iy1} ({written} tiles)")
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    if verbose:
        total = sum(p.stat().st_size for p in out.rglob("*.gz"))
        print(f"[tiles] wrote {written} tiles, {total/1e6:.1f} MB gzipped")
    return written
