"""Download REAL OpenStreetMap data (roads + hospitals) for your city from the free Overpass API.

    python scripts/fetch_osm.py --city Bengaluru --lat 12.9716 --lon 77.5946 --radius 6000
    -> data/maps/bengaluru.osm   (OSM XML, readable by the importer and by osrm-extract)

No API key, no cost. Please be gentle with the public servers: one download per city is enough.
Data © OpenStreetMap contributors, ODbL 1.0.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
HIGHWAYS = ("motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|"
            "motorway_link|trunk_link|primary_link|secondary_link|tertiary_link")


def build_query(lat: float, lon: float, radius_m: float, timeout_s: int = 300) -> str:
    # roads a little beyond the service radius so routes near the edge still have alternatives
    road_r = int(radius_m * 1.2)
    return f"""[out:xml][timeout:{timeout_s}][maxsize:1073741824];
(
  way["highway"~"^({HIGHWAYS})$"](around:{road_r},{lat},{lon});
  nwr["amenity"="hospital"](around:{int(radius_m)},{lat},{lon});
);
(._;>;);
out body;"""


def download(query: str, out: Path) -> None:
    last = None
    for url in MIRRORS:
        for attempt in range(2):
            try:
                print(f"requesting {url} (attempt {attempt + 1}) ...", flush=True)
                with httpx.stream("POST", url, data={"data": query}, timeout=httpx.Timeout(600, connect=30),
                                  headers={"User-Agent": "ems-dispatch-academic-project/1.0"}) as r:
                    if r.status_code == 429 or r.status_code >= 500:
                        raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
                    r.raise_for_status()
                    tmp = out.with_suffix(".part")
                    size = 0
                    with tmp.open("wb") as f:
                        for chunk in r.iter_bytes(1 << 20):
                            f.write(chunk)
                            size += len(chunk)
                            print(f"\r  {size / 1e6:.1f} MB", end="", flush=True)
                    print()
                head = tmp.read_bytes()[:2000]
                if b"<osm" not in head or b"runtime error" in tmp.read_bytes()[-3000:]:
                    raise RuntimeError("Overpass returned an error document: " + head.decode(errors="ignore")[:300])
                tmp.replace(out)
                return
            except Exception as exc:  # try again / next mirror
                last = exc
                print(f"  failed: {exc}", flush=True)
                time.sleep(10)
    raise SystemExit(f"all Overpass mirrors failed: {last}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--city", required=True)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--radius", type=float, default=6000, help="service radius in metres (keep 4000-8000 on 8 GB RAM)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    out = Path(a.out or Path(__file__).resolve().parent.parent / "data" / "maps" / f"{a.city.lower().replace(' ', '_')}.osm")
    out.parent.mkdir(parents=True, exist_ok=True)
    download(build_query(a.lat, a.lon, a.radius), out)
    print(f"saved {out} ({out.stat().st_size / 1e6:.1f} MB)")
    print("next: set OSM_PBF_PATH in .env to this file and run  python -m app.seed --reset-network  (from backend/)")


if __name__ == "__main__":
    sys.exit(main())
