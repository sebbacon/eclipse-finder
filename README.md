# eclipse-finder

Geospatial optimisation for viewing the **12 Aug 2026 partial solar eclipse**
(or any eclipse/sun event, at any lat/lon/date): find observer points whose
*real terrain horizon* stays below the Sun's track for the whole event,
weighted by accessibility.

The key metric is not observer elevation — it is

```
eclipse_clearance(t) = sun_altitude(t) − apparent_horizon_altitude(az_sun(t))
```

evaluated every minute of the useful eclipse window, with Earth curvature and
refraction applied to every horizon ray.

## tl;dr — report for a new origin

```bash
just setup                                    # once: venv + install
export LAT=51.507 LON=-0.128 NAME=london      # any lat/lon/date/tz
just search     # ~5 min: DEM fetch, grid search, horizon scoring
just osm        # optional: 2.2 GB GB PBF once, then OSM is offline
just access     # OSRM drive times + parking/walk/paths, re-rank
just refine     # exact standing points snapped to public paths
just report     # output/REPORT_london.md + map + horizon plots
just webmap     # interactive HTML map: all sites, click -> horizon profile
```

Without `just osm`, `access` falls back to Overpass / the OSM API (slower,
flakier). Override `DATE`, `RADIUS`, `GRID`, `TOP` the same way. Outputs for
each origin are namespaced by `NAME` in `output/`.

## Setup & commands (everything via just)

```
just setup                          # uv venv + editable install
just sun                            # eclipse contacts, sun track, clear-sector
just dem                            # fetch DEM tiles for the search radius
just horizon -- -2.518 53.333       # horizon profile + sun-overlay plot for one point
just search                         # full grid search + scoring
just plot                           # map + per-site horizon plots
just access                         # drive/park/walk/PROW + re-rank
just osm                            # (optional) local GB PBF -> offline OSM cache
just refine                         # sub-grid standing-point refinement
just report                         # REPORT_<name>.md + final plots
just webmap                         # interactive Leaflet map + SVG horizon profiles
just smoke                          # fast iteration (15 km, 600 m grid)
```

Override the origin / date / resolution with env vars (fully generic):

```
LAT=51.507 LON=-0.128 NAME=london just sun
RADIUS=25 GRID=400 TOP=15 just search
```

Outputs land in `output/` (CSV of ranked local maxima, meta JSON, PNG map,
horizon-profile PNGs). DEM tiles are cached in `data/dem/`.

## Method

1. **Eclipse geometry** (`solar.py`): Skyfield + DE421, topocentric, geometric
   altitudes (no refraction, since terrain blocks geometric rays). Contacts,
   max magnitude, sun alt/az track at 1-min steps, and the azimuth sector that
   must be clear (sun track ± margin).
   *Cross-validated against astropy/ERFA — the two ephemeris codes agree to
   0.01° in alt/az and to the same minute for contacts.*
2. **DEM** (`dem.py`): Copernicus GLO-30 (30 m) from the public AWS bucket,
   mosaicked per bounding box. Pluggable for EA 1 m LiDAR DTM/DSM later.
3. **Horizon** (`horizon.py`): for each azimuth ray, sample terrain at
   30 m steps to 8 km, 60 m to 24 km, 120 m to 55 km; apparent altitude
   `atan2(h − h0 − drop, d)` with `drop = d²/2R·(1−k)`, k = 0.13.
   Vectorised over observers (shared polar-offset table).
4. **Search** (`search.py`): circular grid (default 250 m, 40 km radius),
   score = min-clearance over the eclipse window + mean-clearance + breadth of
   low western horizon + capped elevation bonus; local-maxima suppression
   (750 m).
5. **Plots** (`plot.py`): terrain map with candidates and sun-sector wedge;
   per-site horizon profile with the Sun's eclipse track overlaid.

## What is verified vs inferred (status)

- **Verified computationally**: eclipse contacts/magnitude; sun alt/az track;
  azimuth sector; terrain horizon profiles (30 m DEM); vegetation-corrected
  horizons (Hansen 30 m canopy model); clearance scores; observer-canopy mask.
- **Validated behaviour**: Alderley Edge — with correct DEM geometry the
  ridge's bare horizon towards the eclipse is ~0° (it overlooks the flat
  Cheshire Plain) and modelled canopy adds ~1.5°, but the ridge top sits in
  mapped woodland (observer-canopy mask) so it is correctly rejected as a
  standing point. Top moorland sites show vegRisk ≈ 0 (genuinely tree-free
  sightlines).
- **Inferred / not yet modelled**: sub-30 m hedge lines outside the inner
  region; untagged building heights (default 6 m); canopy heights from the
  Hansen model; land-access details. EA 1 m LiDAR was probed but is not
  anonymously accessible.
- **Would need ground-truthing**: exact standing spot, fence/stile access,
  local tree lines not in any dataset.

## Roadmap (next phases)

1. ~~Vegetation/building penalty~~ **done** (Hansen GFC v1.12, loss-corrected
   canopy-height model, α=0.5 TanDEM-X correction; observer woodland mask).
2. **Access** ~~OSRM drive-time~~ **done**: OSRM drive times; parking/walk/
   ascent (Naismith on DEM); path & land-designation proxies from OSM.
   OSM data via local Geofabrik GB PBF (`just osm`, pyosmium two-pass
   extract) with Overpass only as fallback. Re-ranked table in
   `output/<name>_access.csv`.
3. ~~Refine top candidates~~ **done**: 30 m sub-grid optimisation of the
   standing point, snapped towards mapped public paths, canopy-masked;
   full bare+veg horizon profiles stored per refined site.
4. ~~Final report~~ **done**: `output/REPORT_<name>.md` (top-10 table,
   top-5 narratives, verification status) + `final_map_<name>.png` +
   `final_<name>_{1..5}.png` horizon plots.

EA 1 m LiDAR was probed and is **not anonymously accessible** (JS portal,
empty ArcGIS service directory); vegetation therefore stays Hansen-modelled
and the report flags which sites need a ground check.

## Interactive explorer (`just webmap`)

`output/<name>_webmap.html` is a single self-contained file (~9 MB): ranked
site markers plus **click-anywhere** horizon computation in the browser.
A two-ring raster is embedded (gzip+base64): 30 m terrain + 30 m modelled
canopy within 15 km of the origin, ~90 m beyond, out to 55 km. Each click
computes the bare and canopy-corrected horizon (211 azimuths x 792 range
bins, ~167k samples) in ~15 ms and draws it as SVG with the sun track,
plus Google-Maps/OSM links. OSM buildings (319k in the inner region,
10 m raster, tagged heights else 2-storey default) and hedges are
max-combined with the canopy as obstacles; clicking inside a mapped
building shows a warning. Cross-checked against the server pipeline
(bare terrain ~0.1°; urban green line correctly jumps to ~11° in central
Manchester where the server model has no buildings).

## UK-wide explorer on GitHub Pages

`just tiles` builds a country-wide two-level tile pyramid into `pages/tiles/`
(60 m terrain+canopy to 15 km, 180 m beyond; ~110 MB gzipped; ~1-3 MB fetched
per click, cached in the browser). `just ukmap` writes `pages/index.html`:
click anywhere in GB for the same in-browser horizon, with sun tracks
interpolated per lat/lon. Buildings are not in the UK pyramid (use the
per-origin bundles for those). Serve locally with `just serve-pages`;
publish with `just ghrepo` once, then `just publish` (force-pushes `pages/`
to an orphan `gh-pages` branch; site at sebbacon.github.io/eclipse-finder).

## Data sources & attribution

- **Eclipse/sun geometry**: Skyfield + JPL DE421 (cross-checked with
  astropy/ERFA).
- **Terrain**: Copernicus GLO-30 (TanDEM-X), via the public AWS bucket —
  © ESA/Copernicus.
- **Vegetation**: Hansen Global Forest Change v1.12 (NASA/UMD/Google/USGS),
  public GCS bucket.
- **OSM** (parking, paths, land designations): © OpenStreetMap contributors
  (ODbL), via Geofabrik PBF / Overpass / OSM API.
- **Drive times**: OSRM demo server (© OpenStreetMap contributors).

All derived outputs (horizons, scores, reports) are computed by this project.
