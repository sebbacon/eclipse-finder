"""Vegetation obstruction layer.

Source: Hansen Global Forest Change v1.12, 30 m, public GCS bucket
  treecover2000 (% canopy cover) and lossyear (disturbance year).

Model: canopy height h_can = H_MAX * (tc_eff/100)**1.3   (H_MAX = 22 m)
Copernicus GLO-30 (TanDEM-X) partially penetrates canopy, so the
vegetation-corrected surface used for horizon rays is

    h_eff = cop30 + alpha * h_can        (alpha = 0.5)

Sites whose clearance collapses between the bare and corrected horizons are
woodland-dependent and get penalised; observers standing inside canopy
(tc_eff > 50% locally) are flagged.
"""
from __future__ import annotations

import pathlib

import numpy as np
import requests

DATA_DIR = pathlib.Path(__file__).parent.parent / "data"
HANSEN_DIR = DATA_DIR / "hansen"
HANSEN_DIR.mkdir(parents=True, exist_ok=True)

HANSEN_BASE = "https://storage.googleapis.com/earthenginepartners-hansen/GFC-2024-v1.12"
H_MAX = 22.0
ALPHA = 0.5  # TanDEM-X partial-canopy-penetration correction

LOSS_PENALTY = 0.2  # tc multiplier where lossyear > 0 (felled / young regrowth)


def _tile_corners(lat: float, lon: float) -> tuple[str, str]:
    """Hansen tiles are 10x10 deg named by TOP-LEFT corner."""
    top = int(np.ceil((lat + 1e-9) / 10) * 10)
    left = int(np.floor((lon - 1e-9) / 10) * 10)
    ns = f"{top}N" if top >= 0 else f"{-top}S"
    ew = f"{left:03d}E" if left >= 0 else f"{-left:03d}W"
    return ns, ew


def _download(url: str, dest: pathlib.Path) -> pathlib.Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    tmp = dest.with_suffix(".part")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    tmp.rename(dest)
    return dest


def _fetch_tile(kind: str, lat: float, lon: float, verbose: bool = True) -> pathlib.Path:
    ns, ew = _tile_corners(lat, lon)
    name = f"Hansen_GFC-2024-v1.12_{kind}_{ns}_{ew}.tif"
    dest = HANSEN_DIR / name
    if not dest.exists():
        if verbose:
            print(f"downloading {name} …")
        _download(f"{HANSEN_BASE}/{name}", dest)
    return dest


class Veg:
    """Canopy-height raster (metres) over a bbox, sampled like a Dem."""

    def __init__(self, tc: np.ndarray, loss: np.ndarray, lon0: float, lat0: float, res: float):
        self.tc_eff = np.where(loss > 0, tc * LOSS_PENALTY, tc)
        self.h_can = H_MAX * (self.tc_eff / 100.0) ** 1.3
        self.res = res
        self.lon0 = lon0
        self.lat0 = lat0
        self.h, self.w = self.h_can.shape

    @classmethod
    def for_bbox(cls, bbox: tuple[float, float, float, float], verbose: bool = True) -> "Veg":
        import rasterio
        from rasterio.windows import from_bounds

        w, s, e, n = bbox
        tc_path = _fetch_tile("treecover2000", n, w, verbose)
        loss_path = _fetch_tile("lossyear", n, w, verbose)
        with rasterio.open(tc_path) as src:
            t = src.transform
            win = from_bounds(w, s, e, n, src.transform)
            tc = src.read(1, window=win, masked=False).astype(np.float32)
            res = t.a
            lon0 = t.c + win.col_off * t.a
            lat0 = t.f + win.row_off * t.e
        with rasterio.open(loss_path) as src:
            loss = src.read(1, window=win, masked=False)
        if verbose:
            print(f"veg layer {tc.shape[1]}x{tc.shape[0]} px @ {res*111320:.0f} m, "
                  f"mean tc {tc[tc > 0].mean() if (tc > 0).any() else 0:.0f}%")
        return cls(tc, loss, lon0, lat0, res)

    def canopy_at(self, lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
        col = (lon - self.lon0) / self.res
        row = (self.lat0 - lat) / self.res
        c0 = np.floor(col).astype(np.int64)
        r0 = np.floor(row).astype(np.int64)
        ok = (c0 >= 0) & (c0 < self.w - 1) & (r0 >= 0) & (r0 < self.h - 1)
        cc = np.clip(c0, 0, self.w - 2)
        rr = np.clip(r0, 0, self.h - 2)
        fc = col - cc
        fr = row - rr
        v = (
            self.h_can[rr, cc] * (1 - fc) * (1 - fr)
            + self.h_can[rr, cc + 1] * fc * (1 - fr)
            + self.h_can[rr + 1, cc] * (1 - fc) * fr
            + self.h_can[rr + 1, cc + 1] * fc * fr
        )
        return np.where(ok, v, 0.0)

    def tc_at(self, lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
        """Nearest-neighbour effective tree cover % (for observer masking)."""
        col = np.clip(np.round((lon - self.lon0) / self.res).astype(np.int64), 0, self.w - 1)
        row = np.clip(np.round((self.lat0 - lat) / self.res).astype(np.int64), 0, self.h - 1)
        return self.tc_eff[row, col]
