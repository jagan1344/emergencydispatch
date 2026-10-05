#!/usr/bin/env bash
# Linux/macOS equivalent of setup_osrm.ps1:  ./scripts/setup_osrm.sh data/maps/monaco.osm.pbf
set -euo pipefail
pbf="$(realpath "$1")"; dir="$(dirname "$pbf")"; name="$(basename "$pbf")"; base="${name%.osm.pbf}"
docker run --rm -v "$dir:/data" osrm/osrm-backend osrm-extract -p /opt/car.lua "/data/$name"
docker run --rm -v "$dir:/data" osrm/osrm-backend osrm-partition "/data/$base.osrm"
docker run --rm -v "$dir:/data" osrm/osrm-backend osrm-customize "/data/$base.osrm"
echo "Start: docker run -d --name osrm -p 5000:5000 -v $dir:/data osrm/osrm-backend osrm-routed --algorithm mld /data/$base.osrm"
