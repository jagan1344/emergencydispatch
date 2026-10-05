# Map data (OpenStreetMap)

The road network and OSRM both use **OpenStreetMap** data (© OpenStreetMap contributors, ODbL 1.0).

`monaco.osm.pbf` (1.7 MB) is bundled as a small real-world sample. It is the test extract published
by the OSRM project (https://github.com/Project-OSRM/osrm-backend/tree/master/test/data) and covers
Monaco plus the neighbouring French communes (about 11 x 6 km).

## Using your own city (small region workflow)

1. Download a **small** extract (city/district, not a whole country):
   * Geofabrik: https://download.geofabrik.de/asia/india.html (e.g. `southern-zone-latest.osm.pbf`)
     then clip it to your city with osmium (pip install osmium-tool is not needed - use the
     Docker image): `docker run --rm -v ${PWD}\data\maps:/data stefda/osmium-tool osmium extract -b 77.50,12.90,77.70,13.05 /data/southern-zone-latest.osm.pbf -o /data/bengaluru.osm.pbf`
   * or BBBike custom extracts: https://extract.bbbike.org/ (draw a rectangle, choose "Protocolbuffer (PBF)").
2. Save it as `data/maps/<city>.osm.pbf` and set in `.env`:
   `OSM_PBF_PATH=../data/maps/<city>.osm.pbf`, `CITY_NAME`, `CITY_LAT`, `CITY_LON`, `CITY_RADIUS_M`.
3. Import the road graph into PostGIS: `python -m app.seed --reset-network` (from `backend/`).
4. Pre-process for OSRM: `.\scripts\setup_osrm.ps1 -Pbf data\maps\<city>.osm.pbf`
5. Start OSRM: `docker run -d --name osrm -p 5000:5000 -v "${PWD}\data\maps:/data" osrm/osrm-backend osrm-routed --algorithm mld /data/<city>.osrm`
6. Set `OSRM_URL=http://localhost:5000` and restart the backend. `/api/health` shows `routing.mode = osm+osrm`.

Keep `CITY_RADIUS_M` around 4-8 km on an 8 GB machine.

## Fallback demo mode

If `OSM_PBF_PATH` does not exist, the seed script generates a **synthetic grid network** around the
configured city centre so the whole system still runs. The dashboard status bar then shows
`Routing: synthetic-fallback`. As soon as an OSM extract is imported, the real network is used.
