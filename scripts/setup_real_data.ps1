# Switch the system from demo data to REAL data for your city (Windows PowerShell).
#
#   .\scripts\setup_real_data.ps1                                   # Bengaluru, 6 km radius
#   .\scripts\setup_real_data.ps1 -City Mysuru -Lat 12.2958 -Lon 76.6394 -Radius 5000
#   .\scripts\setup_real_data.ps1 -KtasCsv C:\Users\me\Downloads\data.csv   # also train on real triage data
#   .\scripts\setup_real_data.ps1 -OsmFile D:\maps\bengaluru.osm.pbf        # use an extract you already have
#
# Steps: download OSM roads + hospitals (Overpass, free) -> update .env -> import road graph into PostGIS ->
# real hospitals -> hospitals CSV for verified capabilities -> OSRM preprocessing + server (Docker) ->
# optional ML training on the KTAS dataset. PostgreSQL/PostGIS must be running; Docker Desktop for OSRM.
param(
  [string]$City = "Bengaluru",
  [double]$Lat = 12.9716,
  [double]$Lon = 77.5946,
  [int]$Radius = 6000,
  [string]$OsmFile = "",
  [string]$KtasCsv = "",
  [switch]$SkipOsrm
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = if (Test-Path .\.venv\Scripts\python.exe) { (Resolve-Path .\.venv\Scripts\python.exe).Path } else { "python" }
$slug = $City.ToLower() -replace "[^a-z0-9]+", "_"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }

# 1) real map data
if ($OsmFile) { $osm = (Resolve-Path $OsmFile).Path }
else {
  $osm = Join-Path $root "data\maps\$slug.osm"
  if (-not (Test-Path $osm)) { & $py scripts\fetch_osm.py --city $City --lat $Lat --lon $Lon --radius $Radius --out $osm }
  else { Write-Host "using existing $osm" }
}

# 2) .env
function Set-EnvVar($name, $value) {
  $lines = Get-Content .env
  if ($lines -match "^$name=") { $lines = $lines -replace "^$name=.*", "$name=$value" } else { $lines += "$name=$value" }
  Set-Content .env $lines
}
$hospCsv = Join-Path $root "data\hospitals_$slug.csv"
Set-EnvVar CITY_NAME $City; Set-EnvVar CITY_LAT $Lat; Set-EnvVar CITY_LON $Lon; Set-EnvVar CITY_RADIUS_M $Radius
Set-EnvVar OSM_PBF_PATH $osm; Set-EnvVar HOSPITALS_CSV $hospCsv
if (-not $SkipOsrm) { Set-EnvVar OSRM_URL "http://localhost:5000" }

# 3) road graph + real hospitals (+ verified CSV if it already exists)
Push-Location backend
& $py -m app.seed --reset-network
if (-not (Test-Path $hospCsv)) {
  & $py -m app.hospital_data export $hospCsv
  Write-Host "Edit $hospCsv to record VERIFIED ICU / trauma / cardiac / stroke capabilities, then run:"
  Write-Host "  cd backend; python -m app.hospital_data import $hospCsv"
}
# 4) optional: real triage dataset
if ($KtasCsv) { & $py -m app.ml.train --dataset ktas --data (Resolve-Path $KtasCsv).Path }
Pop-Location

# 5) OSRM on the same extract
if (-not $SkipOsrm) {
  & "$PSScriptRoot\setup_osrm.ps1" -Pbf $osm
  $dir = Split-Path $osm; $base = [IO.Path]::GetFileName($osm) -replace "\.osm(\.pbf)?$", ""
  docker rm -f osrm 2>$null | Out-Null
  docker run -d --name osrm -p 5000:5000 -v "${dir}:/data" osrm/osrm-backend osrm-routed --algorithm mld "/data/$base.osrm"
}
Write-Host "`nDone. Restart the backend; /api/health should report routing.mode = osm+osrm and the Hospitals page shows OSM hospitals."
