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

osm:  ## build local OSM access cache from GB PBF (downloads once)
    @test -f data/osm/great-britain.osm.pbf || curl -sL -o data/osm/great-britain.osm.pbf https://download.geofabrik.de/europe/great-britain-latest.osm.pbf
    {{run}} osm --top 20

refine:  ## sub-grid standing-point refinement for top access sites
    {{run}} refine --top 10

report:  ## final REPORT_<name>.md + final plots (run refine first)
    {{run}} report

webmap:  ## interactive HTML map, all candidates + horizon profiles
    {{run}} webmap

tiles:  ## build UK tile pyramid into pages/tiles (long: downloads + ~20 min)
    {{run}} tiles

ukmap:  ## write pages/index.html (UK-wide explorer)
    {{run}} ukmap

serve-pages:  ## local preview of pages/ at :8000
    python3 -m http.server 8000 -d pages

ghrepo:  ## create the GitHub repo + push main (needs gh auth)
    gh repo create sebbacon/eclipse-finder --public --source=. --remote=origin --push

publish:  ## force-push pages/ as the gh-pages branch (needs gh auth)
    @test -f pages/tiles/meta.json || { echo "run just tiles && just ukmap first"; exit 1; }
    rm -rf /tmp/efpages && mkdir -p /tmp/efpages && cp -R pages/. /tmp/efpages/
    cd /tmp/efpages && git init -q -b gh-pages && git add -A && \
      git -c user.name="$(git config user.name)" -c user.email="$(git config user.email)" \
      commit -q -m "UK eclipse explorer + tile pyramid" && \
      git push -q -f "https://x-access-token:$(gh auth token)@github.com/sebbacon/eclipse-finder.git" gh-pages
    @echo "Pages: https://sebbacon.github.io/eclipse-finder/"

smoke:  ## fast iteration: small radius, coarse grid
    RADIUS=15 GRID=600 TOP=5 just search
    RADIUS=15 TOP=3 just plot

all: setup sun search plot  ## end-to-end
