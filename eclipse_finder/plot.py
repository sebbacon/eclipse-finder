"""Maps and horizon-profile plots."""
from __future__ import annotations

import pathlib

import numpy as np

from .dem import Dem
from .solar import EclipseGeometry

OUTPUT_DIR = pathlib.Path(__file__).parent.parent / "output"


def plot_horizon_profile(
    az_axis: np.ndarray,
    horizon: np.ndarray,
    geo: EclipseGeometry,
    title: str,
    out_path: pathlib.Path,
    obs_lon: float | None = None,
    obs_lat: float | None = None,
    elev_m: float | None = None,
    horizon_veg: np.ndarray | None = None,
):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(11, 5))
    # horizon profile over the plotted sector (pad to full circle for wrap)
    az_plot = np.concatenate([az_axis - 360, az_axis, az_axis + 360])
    h_plot = np.concatenate([horizon, horizon, horizon])

    # sun path during eclipse
    us = geo.useful_samples
    if us:
        az0 = az_axis.min()
        saz = []
        for s in us:
            a = s.az_deg
            while a < az0:
                a += 360
            while a > az0 + 360:
                a -= 360
            saz.append(a)
        ax.plot(saz, [s.alt_deg for s in us], "o-", color="tab:orange", ms=3,
                label="Sun track during eclipse (1-min marks)")
        for s in us[::10]:
            a = s.az_deg
            while a < az0:
                a += 360
            while a > az0 + 360:
                a -= 360
            ax.annotate(s.t_utc.strftime("%H:%M"), (a, s.alt_deg),
                        fontsize=6, alpha=0.7, xytext=(0, 5), textcoords="offset points")

    ax.fill_between(az_plot, -3, h_plot, where=h_plot > -3, color="saddlebrown", alpha=0.55,
                    label="Terrain horizon (bare)")
    if horizon_veg is not None:
        hv_plot = np.concatenate([horizon_veg, horizon_veg, horizon_veg])
        ax.plot(az_plot, hv_plot, color="forestgreen", lw=1.2,
                label="Horizon incl. modelled canopy")
    ax.axhline(0, color="k", lw=0.5)
    ax.set_xlabel("Azimuth (° clockwise from N)")
    ax.set_ylabel("Apparent altitude (°)")
    sub = ""
    if obs_lon is not None:
        sub = f"obs {obs_lat:.5f}, {obs_lon:.5f}"
        if elev_m is not None:
            sub += f", {elev_m:.0f} m"
    ax.set_title(f"{title}   {sub}")
    ax.set_xlim(az_axis.min(), az_axis.max())
    ax.set_ylim(-2, max(30, np.max(h_plot) + 5))
    ax.grid(alpha=0.3)
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    return out_path


def plot_map(dem: Dem, origin: tuple[float, float], candidates: list[dict],
             top_n: int, title: str, out_path: pathlib.Path,
             sun_az_range: tuple[float, float] | None = None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    a = dem.a
    step = max(1, min(a.shape) // 1400)
    sub = a[::step, ::step]
    ext = (dem.lon0, dem.lon0 + dem.w * dem.res,
           dem.lat0 - dem.h * dem.res_lat, dem.lat0)

    fig, ax = plt.subplots(figsize=(10, 10))
    im = ax.imshow(sub, extent=ext, cmap="terrain", origin="upper", vmin=0, vmax=700)
    fig.colorbar(im, ax=ax, label="elevation (m)", shrink=0.7)

    lons = np.array([c["lon"] for c in candidates])
    lats = np.array([c["lat"] for c in candidates])
    scores = np.array([c["score"] for c in candidates])
    ax.scatter(lons[top_n:], lats[top_n:], s=18, c="white", edgecolors="k", lw=0.3, zorder=3)
    ax.scatter(lons[:top_n], lats[:top_n], s=70, c="red", marker="*", edgecolors="k",
               lw=0.4, zorder=4)
    for i in range(min(top_n, len(candidates))):
        ax.annotate(f"#{i+1}", (lons[i], lats[i]), fontsize=8,
                    xytext=(4, 4), textcoords="offset points", zorder=5)
    ox, oy = origin
    ax.plot([ox], [oy], "s", color="blue", ms=9, zorder=6, label="origin")
    ax.annotate("origin", origin, xytext=(6, -4), textcoords="offset points", color="blue")

    if sun_az_range:
        import matplotlib.patches as mpatches
        from matplotlib.patches import Wedge
        az0, az1 = sun_az_range
        r = (ext[1] - ext[0]) * 0.55
        w = Wedge(origin, r, 90 - az1, 90 - az0, alpha=0.12, color="gold",
                  transform=ax.transData)
        ax.add_patch(w)

    ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude")
    ax.set_title(title)
    ax.set_aspect("equal")
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    return out_path
