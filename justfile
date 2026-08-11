# Eclipse viewing-site finder — driven entirely through just.
#
# Default origin: south Altrincham (53.369, -2.352). Override any knob via
# environment variables, e.g.:
#   LAT=51.507 LON=-0.128 NAME=london just sun
#   RADIUS=25 GRID=400 just search

LAT    := env("LAT", "53.369")
LON    := env("LON", "-2.352")
DATE   := env("DATE", "2026-08-12")
NAME   := env("NAME", "altrincham")
RADIUS := env("RADIUS", "40")
GRID   := env("GRID", "250")
TOP    := env("TOP", "10")

run := ".venv/bin/python -m eclipse_finder.cli --lat " + LAT + " --lon " + LON + " --date " + DATE + " --name " + NAME

export MPLCONFIGDIR := ".mplcache"

default:
    @just --list

setup:  ## create venv + install package
    uv venv --python 3.12
    uv pip install -e .

sun:  ## eclipse contacts, sun track, azimuth sector to keep clear
    {{run}} sun

dem:  ## download DEM tiles covering the search radius
    {{run}} dem --radius-km {{RADIUS}}

horizon PLON_PT PLAT_PT:  ## horizon profile + sun-track plot for one point
    {{run}} horizon --plat {{PLAT_PT}} --plon {{PLON_PT}} --radius-km {{RADIUS}}

search:  ## full grid search + scoring (writes output/<name>_candidates.csv)
    {{run}} search --radius-km {{RADIUS}} --grid-m {{GRID}} --top {{TOP}}

plot:  ## map + horizon plots for top candidates (run search first)
    {{run}} plot --top {{TOP}}

access:  ## drive/park/walk/PROW analysis + re-rank (run search first)
    {{run}} access --top 20

osm:  ## build local OSM access cache from GB PBF (kills Overpass dependency)
    curl -sL -C - -o data/osm/great-britain.osm.pbf https://download.geofabrik.de/europe/great-britain-latest.osm.pbf
    {{run}} osm --top 20

refine:  ## sub-grid standing-point refinement for top access sites
    {{run}} refine --top 10

report:  ## final REPORT_<name>.md + final plots (run refine first)
    {{run}} report

smoke:  ## fast iteration: small radius, coarse grid
    RADIUS=15 GRID=600 TOP=5 just search
    RADIUS=15 TOP=3 just plot

all: setup sun search plot  ## end-to-end
