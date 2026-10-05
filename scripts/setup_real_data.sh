#!/usr/bin/env bash
# Linux/macOS twin of setup_real_data.ps1
#   ./scripts/setup_real_data.sh [--city Bengaluru --lat 12.9716 --lon 77.5946 --radius 6000] [--osm-file F] [--ktas F] [--skip-osrm]
set -euo pipefail
CITY=Bengaluru LAT=12.9716 LON=77.5946 RADIUS=6000 OSM_FILE="" KTAS="" SKIP_OSRM=0
while [ $# -gt 0 ]; do case "$1" in
  --city) CITY=$2; shift 2;; --lat) LAT=$2; shift 2;; --lon) LON=$2; shift 2;; --radius) RADIUS=$2; shift 2;;
  --osm-file) OSM_FILE=$2; shift 2;; --ktas) KTAS=$2; shift 2;; --skip-osrm) SKIP_OSRM=1; shift;;
  *) echo "unknown option $1"; exit 1;; esac; done
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
PY=${PYTHON:-python3}
SLUG=$(echo "$CITY" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]\+/_/g')
[ -f .env ] || cp .env.example .env
if [ -n "$OSM_FILE" ]; then OSM="$(realpath "$OSM_FILE")"; else
  OSM="$ROOT/data/maps/$SLUG.osm"; [ -f "$OSM" ] || $PY scripts/fetch_osm.py --city "$CITY" --lat "$LAT" --lon "$LON" --radius "$RADIUS" --out "$OSM"; fi
setenv() { if grep -q "^$1=" .env; then sed -i "s#^$1=.*#$1=$2#" .env; else echo "$1=$2" >> .env; fi; }
HCSV="$ROOT/data/hospitals_$SLUG.csv"
setenv CITY_NAME "$CITY"; setenv CITY_LAT "$LAT"; setenv CITY_LON "$LON"; setenv CITY_RADIUS_M "$RADIUS"
setenv OSM_PBF_PATH "$OSM"; setenv HOSPITALS_CSV "$HCSV"
[ "$SKIP_OSRM" = 1 ] || setenv OSRM_URL "http://localhost:5000"
(cd backend && $PY -m app.seed --reset-network && { [ -f "$HCSV" ] || $PY -m app.hospital_data export "$HCSV"; } \
  && { [ -z "$KTAS" ] || $PY -m app.ml.train --dataset ktas --data "$(realpath "$KTAS")"; })
if [ "$SKIP_OSRM" != 1 ]; then
  "$ROOT/scripts/setup_osrm.sh" "$OSM"
  BASE=$(basename "$OSM"); BASE=${BASE%.pbf}; BASE=${BASE%.osm}
  docker rm -f osrm >/dev/null 2>&1 || true
  docker run -d --name osrm -p 5000:5000 -v "$(dirname "$OSM"):/data" osrm/osrm-backend osrm-routed --algorithm mld "/data/$BASE.osrm"
fi
echo "Done. Restart the backend; /api/health should report routing.mode = osm+osrm."
