"""Real hospital data: OpenStreetMap import + verified-capability CSV round-trip.

OpenStreetMap gives real names and locations of hospitals (amenity=hospital). It rarely tells us
whether a hospital has an ICU, a trauma centre, a cath-lab (cardiac) or a stroke unit, so those are only
set when the OSM tags say so. Everything else must be VERIFIED by you in a CSV:

    python -m app.hospital_data export ../data/hospitals_bengaluru.csv     # write current hospitals
    (edit icu_available / trauma / cardiac / stroke / emergency_capacity columns, from hospital websites
     or the state health department directory)
    python -m app.hospital_data import ../data/hospitals_bengaluru.csv     # apply -> data_source=VERIFIED
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
from pathlib import Path

from sqlalchemy import select

from app.utils.geo import haversine_m

log = logging.getLogger("app.hospital_data")
CSV_FIELDS = ["id", "name", "latitude", "longitude", "emergency_capacity", "icu_available", "trauma_available",
              "cardiac_available", "stroke_available", "status", "phone", "osm_id", "data_source"]


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y", "t")


def read_osm_hospitals(path: str, center_lat: float, center_lon: float, radius_m: float) -> list[dict]:
    """Hospitals (nodes and building outlines) tagged amenity=hospital inside the service radius."""
    import osmium

    found: list[dict] = []

    def add(kind, oid, tags, lat, lon):
        if tags.get("amenity") != "hospital" or tags.get("emergency") == "no":
            return
        if haversine_m(center_lat, center_lon, lat, lon) > radius_m:
            return
        spec = (tags.get("healthcare:speciality") or "").lower()
        nm = (tags.get("name:en") or tags.get("name") or "").lower()
        beds = tags.get("beds") or tags.get("capacity:beds")
        try:
            beds_n = int(str(beds).split(";")[0])
        except (TypeError, ValueError):
            beds_n = None
        found.append({
            "osm_id": f"{kind}{oid}",
            "name": tags.get("name:en") or tags.get("name") or f"Unnamed hospital ({kind}{oid})",
            "latitude": lat, "longitude": lon, "beds": beds_n,
            "emergency": tags.get("emergency") == "yes",
            # only explicit evidence: OSM speciality tags, or the speciality spelled out in the hospital's name
            "trauma": "trauma" in spec or tags.get("emergency:trauma") == "yes" or "trauma" in nm,
            "cardiac": any(k in spec or k in nm for k in ("cardiolog", "cardiac", "cardio", "heart")),
            "stroke": any(k in spec or k in nm for k in ("neurolog", "stroke", "neuro")),
            "phone": tags.get("phone") or tags.get("contact:phone"),
        })

    class H(osmium.SimpleHandler):
        def node(self, n):  # noqa: N802
            if n.tags.get("amenity") == "hospital":
                add("N", n.id, dict(n.tags), n.location.lat, n.location.lon)

        def area(self, a):  # building outlines: closed ways and multipolygon relations
            if a.tags.get("amenity") != "hospital":
                return
            pts = [(n.location.lat, n.location.lon) for ring in a.outer_rings() for n in ring if n.location.valid()]
            if pts:
                kind = "W" if a.from_way() else "R"
                add(kind, a.orig_id(), dict(a.tags), sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))

    H().apply_file(path, locations=True)
    # de-duplicate (same hospital mapped as node and outline / several outlines): same name within 300 m
    unique: list[dict] = []
    for h in sorted(found, key=lambda x: (not x["emergency"], x["beds"] is None, x["name"])):
        if any(u["name"] == h["name"] and haversine_m(u["latitude"], u["longitude"], h["latitude"], h["longitude"]) < 300
               for u in unique):
            continue
        unique.append(h)
    return unique


def osm_to_rows(hospitals: list[dict], limit: int) -> list[dict]:
    """Map OSM hospitals onto hospital rows. Unknown capabilities are left False/0 (not invented)."""
    rows = []
    # prefer hospitals tagged as having an emergency department, then larger ones
    ordered = sorted(hospitals, key=lambda h: (not h["emergency"], -(h["beds"] or 0)))
    for k, h in enumerate(ordered[:limit]):
        beds = h["beds"]
        rows.append({
            "id": f"HSP-{k + 1:03d}", "name": h["name"][:255], "latitude": h["latitude"], "longitude": h["longitude"],
            "emergency_capacity": max(5, min(200, beds // 10)) if beds else 20,
            "icu_available": 0, "trauma_available": h["trauma"], "cardiac_available": h["cardiac"],
            "stroke_available": h["stroke"], "status": "ACTIVE", "phone": h["phone"], "osm_id": h["osm_id"],
            "data_source": "OSM",
        })
    return rows


def export_csv(db, path: Path) -> int:
    from app.models import Hospital
    hs = db.scalars(select(Hospital).order_by(Hospital.id)).all()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for h in hs:
            w.writerow({k: getattr(h, k) for k in CSV_FIELDS})
    return len(hs)


def import_csv(db, path: Path) -> dict:
    """Apply a verified-capability CSV. Rows are matched by id (or osm_id). Rows with an unknown id and
    coordinates are inserted as new hospitals."""
    from app.models import Hospital
    from app.models.entities import point_wkt
    updated = inserted = 0
    with path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            row = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
            h = db.get(Hospital, row.get("id")) if row.get("id") else None
            if h is None and row.get("osm_id"):
                h = db.scalar(select(Hospital).where(Hospital.osm_id == row["osm_id"]))
            if h is None:
                if not (row.get("id") and row.get("latitude") and row.get("longitude") and row.get("name")):
                    continue
                lat, lon = float(row["latitude"]), float(row["longitude"])
                h = Hospital(id=row["id"], name=row["name"], latitude=lat, longitude=lon, location=point_wkt(lat, lon),
                             emergency_capacity=20, current_load=0, status="ACTIVE")
                db.add(h)
                inserted += 1
            else:
                updated += 1
            if row.get("name"):
                h.name = row["name"][:255]
            for f_int in ("emergency_capacity", "icu_available"):
                if row.get(f_int) not in (None, ""):
                    setattr(h, f_int, int(float(row[f_int])))
            for f_bool in ("trauma_available", "cardiac_available", "stroke_available"):
                if row.get(f_bool) not in (None, ""):
                    setattr(h, f_bool, _truthy(row[f_bool]))
            if row.get("status") in ("ACTIVE", "DIVERT", "CLOSED"):
                h.status = row["status"]
            if row.get("phone"):
                h.phone = row["phone"][:64]
            h.data_source = "VERIFIED"
    return {"updated": updated, "inserted": inserted}


if __name__ == "__main__":
    from app.database import session_scope
    from app.utils.logging import configure_logging
    configure_logging()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["export", "import"])
    ap.add_argument("csv")
    a = ap.parse_args()
    with session_scope() as db:
        if a.action == "export":
            print(f"exported {export_csv(db, Path(a.csv))} hospitals to {a.csv}")
        else:
            print(json.dumps(import_csv(db, Path(a.csv))))
