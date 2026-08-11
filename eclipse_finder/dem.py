"""DEM acquisition and sampling.

Default global source: Copernicus GLO-30 (30 m) from the public AWS bucket
  s3://copernicus-dem-30m (no auth needed).

Pluggable sources (UK): Environment Agency 1 m LiDAR DTM/DSM, OS Terrain 5.
`HorizonGeometry` only needs a numpy grid + affine, so sources are swappable.
"""
from __future__ import annotations

import math
import os
import pathlib

import numpy as np
import requests

DATA_DIR = pathlib.Path(__file__).parent.parent / "data"
DEMO_DIR = DATA_DIR / "dem"
DEMO_DIR.mkdir(parents=True, exist_ok=True)

COP30_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"


def tile_name(lat: int, lon: int) -> str:
    """SW-corner naming: N53_00_W003_00 for lat 53..54, lon -3..-2."""
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00"


def cop30_tile_url(lat: int, lon: int) -> str:
    name = tile_name(lat, lon)
    return f"{COP30_BASE}/Copernicus_DSM_COG_10_{name}_DEM/Copernicus_DSM_COG_10_{name}_DEM.tif"


def _download(url: str, dest: pathlib.Path) -> pathlib.Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    tmp = dest.with_suffix(".part")
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    tmp.rename(dest)
    return dest


def tiles_for_bbox(w: float, s: float, e: float, n: float) -> list[tuple[int, int]]:
    out = []
    for lat in range(math.floor(s), math.floor(n) + 1):
        for lon in range(math.floor(w), math.floor(e) + 1):
            out.append((lat, lon))
    return out


def fetch_dem(
    bbox: tuple[float, float, float, float],  # (west, south, east, north)
    name: str,
    source: str = "cop30",
    verbose: bool = True,
) -> pathlib.Path:
    """Download tiles covering bbox and mosaic to data/dem/{name}.tif."""
    import rasterio
    from rasterio.merge import merge

    w, s, e, n = bbox
    out_path = DEMO_DIR / f"{name}.tif"
    if out_path.exists():
        return out_path

    if source != "cop30":
        raise NotImplementedError(f"source {source!r} not wired up yet")

    tiles = tiles_for_bbox(w, s, e, n)
    srcs = []
    for lat, lon in tiles:
        url = cop30_tile_url(lat, lon)
        dest = DEMO_DIR / f"cop30_{tile_name(lat, lon)}.tif"
        if not dest.exists():
            head = requests.head(url, timeout=30)
            if head.status_code != 200:
                raise RuntimeError(f"tile {tile_name(lat, lon)} missing ({head.status_code})")
            size_mb = int(head.headers.get("content-length", 0)) >> 20
            if verbose:
                print(f"downloading {tile_name(lat, lon)} (~{size_mb} MB) …")
            _download(url, dest)
        srcs.append(rasterio.open(dest))

    mosaic, transform = merge(srcs, bounds=(w, s, e, n))
    profile = dict(srcs[0].profile)
    profile.update(
        driver="GTiff",
        height=mosaic.shape[1],
        width=mosaic.shape[2],
        transform=transform,
        count=1,
        dtype="float32",
        nodata=None,
        compress="deflate",
    )
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(mosaic.astype("float32"))
    for src in srcs:
        src.close()
    if verbose:
        print(f"wrote {out_path} ({mosaic.shape[2]}x{mosaic.shape[1]} px)")
    return out_path


class Dem:
    """In-memory DEM with fast bilinear sampling (lon/lat, WGS84)."""

    def __init__(self, path: str | pathlib.Path):
        import rasterio

        with rasterio.open(path) as src:
            self.a = src.read(1).astype(np.float64)
            self.transform = src.transform
            self.crs = str(src.crs)
        if self.crs != "EPSG:4326":
            raise NotImplementedError(f"DEM must be EPSG:4326, got {self.crs}")
        self.h, self.w = self.a.shape
        # affine: lon = c0 + col*res ; lat = r0 + row*(-res_lat)
        # (Copernicus COGs are anisotropic in degrees: ~1.5" lon x 1" lat at 53N)
        self.res = self.transform.a
        self.res_lat = abs(self.transform.e)
        self.lon0 = self.transform.c
        self.lat0 = self.transform.f

    def lonlat_to_px(self, lon: np.ndarray, lat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        col = (lon - self.lon0) / self.res
        row = (self.lat0 - lat) / self.res_lat
        return col, row

    def px_to_lonlat(self, col: np.ndarray, row: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return self.lon0 + col * self.res, self.lat0 - row * self.res_lat

    def sample_bilinear(self, col: np.ndarray, row: np.ndarray, out_of_range=np.nan) -> np.ndarray:
        c0 = np.floor(col).astype(np.int64)
        r0 = np.floor(row).astype(np.int64)
        fc = (col - c0)[..., None] if col.ndim else col - c0
        fr = row - r0
        # handle scalar vs array uniformly by broadcasting later; keep array path
        ok = (c0 >= 0) & (c0 < self.w - 1) & (r0 >= 0) & (r0 < self.h - 1)
        cc = np.clip(c0, 0, self.w - 2)
        rr = np.clip(r0, 0, self.h - 2)
        v00 = self.a[rr, cc]
        v01 = self.a[rr, cc + 1]
        v10 = self.a[rr + 1, cc]
        v11 = self.a[rr + 1, cc + 1]
        fc = col - cc
        fr = row - rr
        val = (
            v00 * (1 - fc) * (1 - fr)
            + v01 * fc * (1 - fr)
            + v10 * (1 - fc) * fr
            + v11 * fc * fr
        )
        if out_of_range is not None:
            val = np.where(ok, val, out_of_range)
        return val

    def elevation_at(self, lon: float, lat: float) -> float:
        col, row = self.lonlat_to_px(np.asarray(lon), np.asarray(lat))
        return float(self.sample_bilinear(col, row))
