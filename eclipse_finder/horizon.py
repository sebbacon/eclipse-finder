"""Horizon profiles and eclipse-clearance scoring.

For an observer at height h0, the apparent altitude of terrain at distance d
and height h is

    horizon_angle = atan2(h - h0 - drop(d), d)

where drop(d) accounts for Earth curvature and (optionally) refraction:

    drop = d^2 / (2 * Re) * (1 - k)     Re = 6371 km, k ≈ 0.13 (visible band)

All heavy loops are numpy-vectorised; a polar offset table (metres east/north
per azimuth/range bin) is shared by every candidate observer.
"""
from __future__ import annotations

import dataclasses
import math

import numpy as np

EARTH_R_M = 6_371_000.0
REFRACTION_K = 0.13


def _meters_per_deg(lat_deg: float) -> tuple[float, float]:
    lat = math.radians(lat_deg)
    m_lat = 111_132.954 - 559.822 * math.cos(2 * lat) + 1.175 * math.cos(4 * lat)
    m_lon = 111_412.84 * math.cos(lat) - 93.5 * math.cos(3 * lat)
    return m_lon, m_lat


@dataclasses.dataclass
class HorizonGrid:
    """Polar sampling table around an observer, in pixel space of a Dem."""

    az_deg: np.ndarray  # (A,) azimuths, degrees clockwise from north
    range_m: np.ndarray  # (R,) ground ranges, metres (increasing)
    dcol: np.ndarray  # (A, R) column offsets (east)
    drow: np.ndarray  # (A, R) row offsets (north -> negative rows)
    ref_az: float  # latitude used for metre->pixel conversion

    @classmethod
    def build(cls, dem, az_deg: np.ndarray, range_m: np.ndarray, ref_lat: float):
        m_lon, m_lat = _meters_per_deg(ref_lat)
        A, R = np.meshgrid(az_deg, range_m, indexing="ij")  # (A, R)
        az_rad = np.radians(A)
        east = np.sin(az_rad) * R
        north = np.cos(az_rad) * R
        dcol = east / m_lon / dem.res
        drow = -north / m_lat / dem.res_lat
        return cls(az_deg=az_deg, range_m=range_m, dcol=dcol, drow=drow, ref_az=ref_lat)


def default_ranges(max_km: float = 55.0) -> np.ndarray:
    """Adaptive range bins: 30 m steps near, coarser far."""
    parts = [
        np.arange(30, 8_000, 30),
        np.arange(8_000, 24_000, 60),
        np.arange(24_000, max_km * 1000 + 1, 120),
    ]
    return np.concatenate(parts)


def _drop_m(range_m: np.ndarray) -> np.ndarray:
    return range_m**2 / (2 * EARTH_R_M) * (1 - REFRACTION_K)


def horizon_profile(dem, lon: float, lat: float, grid: HorizonGrid, veg=None, veg_alpha: float = 0.5) -> np.ndarray:
    """Return apparent horizon altitude (deg) per azimuth for one observer."""
    col0, row0 = dem.lonlat_to_px(np.asarray(lon), np.asarray(lat))
    h_obs = dem.elevation_at(lon, lat)
    cols = col0 + grid.dcol
    rows = row0 + grid.drow
    h = dem.sample_bilinear(cols.ravel(), rows.ravel()).reshape(grid.dcol.shape)
    if veg is not None:
        slons, slats = dem.px_to_lonlat(cols.ravel(), rows.ravel())
        h = h + veg_alpha * veg.canopy_at(slons, slats).reshape(grid.dcol.shape)
        # observer standing height is above local ground; local canopy handled by mask
    d = grid.range_m[None, :]
    apparent = np.degrees(np.arctan2((h - h_obs - _drop_m(d)), d))
    return np.max(apparent, axis=1)


def horizon_profile_batch(dem, lons: np.ndarray, lats: np.ndarray, grid: HorizonGrid,
                          obs_h: np.ndarray | None = None, batch: int = 128,
                          veg=None, veg_alpha: float = 0.5) -> np.ndarray:
    """Horizon altitude (deg) for N observers: returns (N, A) array.

    Vectorised over both observers and azimuth/range bins.
    """
    cols0, rows0 = dem.lonlat_to_px(lons, lats)
    if obs_h is None:
        obs_h = dem.sample_bilinear(cols0, rows0)
    n = len(lons)
    A = len(grid.az_deg)
    out = np.empty((n, A), dtype=np.float32)
    d = grid.range_m[None, None, :]  # (1, 1, R)
    drop = _drop_m(grid.range_m)[None, None, :]
    for i in range(0, n, batch):
        j = min(i + batch, n)
        cols = cols0[i:j, None, None] + grid.dcol[None]
        rows = rows0[i:j, None, None] + grid.drow[None]
        h = dem.sample_bilinear(cols.ravel(), rows.ravel()).reshape(j - i, A, -1)
        if veg is not None:
            slons, slats = dem.px_to_lonlat(cols.ravel(), rows.ravel())
            h = h + veg_alpha * veg.canopy_at(slons, slats).reshape(j - i, A, -1)
        apparent = np.degrees(np.arctan2(h - obs_h[i:j, None, None] - drop, d))
        out[i:j] = apparent.max(axis=2)
    return out


def _interp_along_axis(values: np.ndarray, axis: np.ndarray, xs: np.ndarray, wrap: bool = True) -> np.ndarray:
    """Vectorised interpolation of values (N, A) at positions xs (T,) along a
    uniform axis. Returns (N, T)."""
    d = float(axis[1] - axis[0])
    t = (xs - axis[0]) / d
    if wrap:
        A = values.shape[1]
        i0 = np.floor(t).astype(np.int64) % A
        i1 = (i0 + 1) % A
        f = t - np.floor(t)
        return values[:, i0] * (1 - f)[None, :] + values[:, i1] * f[None, :]
    i0 = np.clip(np.floor(t).astype(np.int64), 0, len(axis) - 2)
    f = np.clip(t - i0, 0, 1)
    return values[:, i0] * (1 - f)[None, :] + values[:, i0 + 1] * f[None, :]


def clearance_profile(horizon: np.ndarray, az_axis: np.ndarray,
                      sun_alt: float, sun_az: float) -> float:
    """eclipse_clearance = sun_alt - horizon_alt(at sun azimuth)."""
    h = float(_interp_along_axis(horizon[None, :], az_axis, np.array([sun_az]))[0, 0])
    return float(sun_alt - h)


@dataclasses.dataclass
class SiteScore:
    lon: float
    lat: float
    elev: float
    min_clearance: float  # deg, over the eclipse window
    mean_clearance: float
    low_horizon_frac: float  # fraction of sector where horizon < 1.5 deg
    west_breadth: float  # azimuth span (deg) with horizon < 1.5 deg, within sector
    score: float


def score_candidates(
    lons: np.ndarray,
    lats: np.ndarray,
    horizon: np.ndarray,  # (N, A)
    az_axis: np.ndarray,
    sun_track: list,  # list of (alt_deg, az_deg) during useful eclipse window
    elevs: np.ndarray,
) -> np.ndarray:
    """Return per-candidate composite score (higher = better)."""
    n = len(lons)
    sun_az = np.array([s[1] for s in sun_track])
    sun_alt = np.array([s[0] for s in sun_track])
    h_at_sun = _interp_along_axis(horizon, az_axis, sun_az, wrap=False)
    clearance = sun_alt[None, :] - h_at_sun  # (N, T)
    min_c = clearance.min(axis=1)
    mean_c = clearance.mean(axis=1)

    low = horizon < 1.5
    low_frac = low.mean(axis=1)
    # breadth: longest cyclic run of low-horizon azimuths.
    # Duplicate the azimuth axis so cyclic runs become linear, then scan columns
    # (vectorised over candidates, loop over azimuth bins).
    daz = float(np.median(np.diff(az_axis))) if len(az_axis) > 1 else 1.0
    A = low.shape[1]
    low2 = np.concatenate([low, low], axis=1)
    cur = np.zeros(n, dtype=np.int64)
    mx = np.zeros(n, dtype=np.int64)
    for c in range(low2.shape[1]):
        col = low2[:, c]
        cur = np.where(col, cur + 1, 0)
        mx = np.maximum(mx, cur)
    breadth = np.minimum(mx, A) * daz

    elev_bonus = np.clip((elevs - 50) / 300, 0, 1.5)  # capped, secondary
    score = (
        1.0 * np.clip(min_c, -5, 20)
        + 0.35 * np.clip(mean_c, -5, 20)
        + 0.05 * breadth
        + 0.4 * elev_bonus
    )
    return np.column_stack([min_c, mean_c, low_frac, breadth, score])
