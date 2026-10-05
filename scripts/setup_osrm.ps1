# Pre-process an OpenStreetMap extract for OSRM (Windows PowerShell + Docker Desktop).
#   .\scripts\setup_osrm.ps1 -Pbf data\maps\bengaluru.osm.pbf
# Then:  docker run -d --name osrm -p 5000:5000 -v "${PWD}\data\maps:/data" osrm/osrm-backend osrm-routed --algorithm mld /data/bengaluru.osrm
param([Parameter(Mandatory = $true)][string]$Pbf)
$ErrorActionPreference = "Stop"
$full = Resolve-Path $Pbf
$dir = Split-Path $full
$name = [IO.Path]::GetFileName($full)
$base = $name -replace "\.osm\.pbf$", ""
Write-Host "Extracting $name (car profile)..."
docker run --rm -t -v "${dir}:/data" osrm/osrm-backend osrm-extract -p /opt/car.lua "/data/$name"
docker run --rm -t -v "${dir}:/data" osrm/osrm-backend osrm-partition "/data/$base.osrm"
docker run --rm -t -v "${dir}:/data" osrm/osrm-backend osrm-customize "/data/$base.osrm"
Write-Host "Done. Start OSRM with:"
Write-Host "  docker run -d --name osrm -p 5000:5000 -v `"${dir}:/data`" osrm/osrm-backend osrm-routed --algorithm mld /data/$base.osrm"
