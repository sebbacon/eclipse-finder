"""Solar / eclipse geometry: sun track, eclipse contacts, azimuth sector.

Generic for any (lat, lon) and date. Uses Skyfield + DE421.
"""
from __future__ import annotations

import dataclasses
import math
from datetime import datetime, timedelta

import numpy as np
from skyfield.api import load, Loader, Topos
from zoneinfo import ZoneInfo

SUN_RADIUS_KM = 695_700.0
MOON_RADIUS_KM = 1_737.4
EARTH_RADIUS_KM = 6_371.0


@dataclasses.dataclass
class SunSample:
    t_utc: datetime
    alt_deg: float
    az_deg: float
    magnitude: float  # fraction of sun diameter covered by moon
    obscuration: float  # fraction of sun *area* covered


@dataclasses.dataclass
class EclipseGeometry:
    lat: float
    lon: float
    date: str
    samples: list[SunSample]
    contacts: dict  # name -> datetime UTC or None
    max_magnitude: float
    sunset_utc: datetime | None
    tz: str

    @property
    def useful_samples(self) -> list[SunSample]:
        """Samples during useful eclipse window (sun up, magnitude > 0)."""
        return [s for s in self.samples if s.alt_deg > -0.5 and s.magnitude > 0]


_loader = Loader(str(__import__("pathlib").Path(__file__).parent.parent / "data" / "skyfield"), expire=False)


def _planets():
    return _loader("de421.bsp")


def _angular_radius_km_over_au(km: float, au_km: float) -> float:
    return math.degrees(math.atan(km / au_km))


def compute_eclipse_geometry(
    lat: float,
    lon: float,
    date: str,  # YYYY-MM-DD (local calendar date of interest)
    tz: str = "Europe/London",
    step_minutes: float = 1.0,
    elevation_m: float = 0.0,
) -> EclipseGeometry:
    """Compute the sun's track and lunar-eclipse-of-the-sun geometry.

    Returns samples every `step_minutes` across the whole local day.
    """
    eph = _planets()
    ts = _loader.timescale()
    earth, sun, moon = eph["earth"], eph["sun"], eph["moon"]

    zone = ZoneInfo(tz)
    day_start_local = datetime.fromisoformat(date).replace(tzinfo=zone)
    t0 = day_start_local.astimezone(ZoneInfo("UTC"))
    times = ts.utc(
        [t0 + timedelta(minutes=i * step_minutes) for i in range(int(24 * 60 / step_minutes) + 1)]
    )

    observer = earth + Topos(latitude_degrees=lat, longitude_degrees=lon, elevation_m=elevation_m)

    sun_astrom = observer.at(times).observe(sun).apparent()
    sun_altaz = sun_astrom.altaz(pressure_mbar=0)  # geometric (no refraction): what the terrain blocks
    sun_alt = sun_altaz[0].degrees
    sun_az = sun_altaz[1].degrees

    moon_astrom = observer.at(times).observe(moon).apparent()
    sep = sun_astrom.separation_from(moon_astrom).radians  # (N,)
    sun_dist_km = sun_astrom.distance().km  # (N,)
    moon_dist_km = moon_astrom.distance().km

    r_sun = np.degrees(np.arctan(SUN_RADIUS_KM / sun_dist_km))
    r_moon = np.degrees(np.arctan(MOON_RADIUS_KM / moon_dist_km))
    sep_d = np.degrees(sep)

    mag = np.clip((r_sun + r_moon - sep_d) / (2 * r_sun), 0.0, None)
    # obscuration (area fraction) for a partial eclipse
    # standard formula using m = (rs+rm-d)/(2 rs), u = rm/rs
    u = r_moon / r_sun
    obsc = np.zeros_like(mag)
    m = mag
    valid = (m > 0) & (m < 1)
    if valid.any():
        mv = m[valid]
        uv = u[valid]
        # angular formula
        cosc = (1 - uv**2 * mv**2 - (1 - mv) ** 2) / (2 * (1 - mv))
        coss = (1 + uv**2 * mv**2 - (1 - mv) ** 2) / (2 * uv * mv)
        cosc = np.clip(cosc, -1, 1)
        coss = np.clip(coss, -1, 1)
        area_sun = math.pi
        area_overlap = mv**2 * np.arccos(cosc) + np.arccos(coss) - 0.5 * np.sqrt(
            np.clip(4 * mv**2 - (mv**2 - 1 + uv * mv) ** 2, 0, None)
        )
        obsc[valid] = area_overlap / area_sun * uv**2
    obsc[m >= 1] = 1.0

    # sunset: last time sun alt crosses 0 going down (geometric + a bit)
    sunset_utc = None
    for i in range(len(sun_alt) - 1):
        if sun_alt[i] > -0.83 and sun_alt[i + 1] <= -0.83:
            sunset_utc = t0 + timedelta(minutes=i * step_minutes)

    samples = []
    for i in range(len(sun_alt)):
        samples.append(
            SunSample(
                t_utc=t0 + timedelta(minutes=i * step_minutes),
                alt_deg=float(sun_alt[i]),
                az_deg=float(sun_az[i]),
                magnitude=float(mag[i]),
                obscuration=float(obsc[i]),
            )
        )

    # contacts: magnitude > 0 while sun above horizon-ish
    up_mag = [s for s in samples if s.alt_deg > -1.0 and s.magnitude > 0]
    contacts = {}
    if up_mag:
        contacts["first_contact"] = up_mag[0].t_utc
        contacts["maximum"] = max(up_mag, key=lambda s: s.magnitude).t_utc
        ends_in_sky = any(s.magnitude > 0 and s.alt_deg > 0 for s in samples[-3:])
        contacts["last_contact"] = None if ends_in_sky else up_mag[-1].t_utc
        contacts["ends_at_sunset"] = ends_in_sky
    else:
        contacts = {"first_contact": None, "maximum": None, "last_contact": None, "ends_at_sunset": False}

    return EclipseGeometry(
        lat=lat,
        lon=lon,
        date=date,
        samples=samples,
        contacts=contacts,
        max_magnitude=max(s.magnitude for s in samples),
        sunset_utc=sunset_utc,
        tz=tz,
    )


def azimuth_sector(geo: EclipseGeometry, margin_deg: float = 12.0) -> tuple[float, float]:
    """Azimuth sector that must be clear: span of sun azimuths during the
    useful eclipse window, plus a margin either side."""
    us = geo.useful_samples
    if not us:
        raise ValueError("no useful eclipse samples")
    azs = sorted(s.az_deg for s in us)
    return azs[0] - margin_deg, azs[-1] + margin_deg


def format_report(geo: EclipseGeometry, tz: str = "Europe/London") -> str:
    zone = ZoneInfo(tz)

    def fmt(t: datetime | None) -> str:
        return t.astimezone(zone).strftime("%H:%M:%S %Z") if t else "-"

    lines = [
        f"Eclipse geometry for lat={geo.lat:.4f} lon={geo.lon:.4f} on {geo.date}",
        f"  max magnitude (diameter): {geo.max_magnitude*100:.1f}%",
        f"  first contact: {fmt(geo.contacts.get('first_contact'))}",
        f"  maximum:       {fmt(geo.contacts.get('maximum'))}",
        f"  last contact:  {fmt(geo.contacts.get('last_contact'))}"
        + ("  [eclipse still in progress at sunset]" if geo.contacts.get("ends_at_sunset") else ""),
        f"  sunset (geometric): {fmt(geo.sunset_utc)}",
        "",
        "  time(UTC)  sun_alt  sun_az  magnitude",
    ]
    for s in geo.samples:
        if s.magnitude > 0 and s.alt_deg > -1.0 and s.t_utc.minute % 5 == 0:
            lines.append(
                f"  {s.t_utc.strftime('%H:%M')}     {s.alt_deg:6.2f}  {s.az_deg:6.2f}   {s.magnitude:6.3f}"
            )
    return "\n".join(lines)
