"""Real-data pipelines: KTAS/MIMIC loaders + training, OSM hospital import, verified-capability CSV.

The KTAS test uses a small FORMAT FIXTURE generated below (same columns, separators and quirks as the
published data.csv). It checks the parsing/training code path; the real file is downloaded by the user.
"""
import csv
import random

import pytest

from app.hospital_data import export_csv, import_csv, osm_to_rows, read_osm_hospitals
from app.ml.predict import SeverityModel
from app.ml.real_datasets import load_ktas, load_mimic_ed
from tests.conftest import CRITICAL_CASE, MILD_CASE

PROJECT = __import__("pathlib").Path(__file__).resolve().parents[2]


def write_ktas_fixture(path, n=400):
    rng = random.Random(3)
    cols = ["Group", "Sex", "Age", "Patients number per hour", "Arrival mode", "Injury", "Chief_complain", "Mental",
            "Pain", "NRS_pain", "SBP", "DBP", "HR", "RR", "BT", "Saturation", "KTAS_RN", "Diagnosis in ED",
            "Disposition", "KTAS_expert", "Error_group", "Length of stay_min", "KTAS duration_min", "mistriage"]
    rows = []
    for _ in range(n):
        k = rng.choice([1, 2, 2, 3, 3, 3, 4, 4, 5])
        mental = 4 if k == 1 and rng.random() < 0.7 else 2 if k == 2 and rng.random() < 0.3 else 1
        hr = 80 + (5 - k) * 12 + rng.randint(-8, 8)
        sat = "??" if rng.random() < 0.3 else str(99 - (5 - k) * 2 + rng.randint(-1, 1))
        rows.append([2, 1, rng.randint(18, 90), 5, 3, rng.choice([1, 2]),
                     rng.choice(["chest pain", "dyspnea", "abd pain", "headache", "fever"]), mental, 1, "3",
                     str(140 - (5 - k) * 10), "80", str(hr), str(16 + (5 - k) * 3), f"36,{rng.randint(4, 9)}", sat,
                     k, "x", 1, k, 0, "100", "2,5", 0])
    with open(path, "w", newline="", encoding="latin-1") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(cols)
        w.writerows(rows)


def test_ktas_loader_parses_quirks(tmp_path):
    p = tmp_path / "data.csv"
    write_ktas_fixture(p, 60)
    df = load_ktas(p)
    assert len(df) == 60
    assert set(df["severity"]) <= {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
    assert df["temperature_c"].between(36, 37).all()          # decimal commas parsed
    assert df["oxygen_saturation"].isna().any()                # '??' -> NaN
    assert df["consciousness"].between(0, 3).all()
    assert df["chest_pain"].sum() > 0 and df["breathing_difficulty"].sum() > 0


def test_mimic_loader(tmp_path):
    p = tmp_path / "triage.csv"
    p.write_text("subject_id,stay_id,temperature,heartrate,resprate,o2sat,sbp,dbp,pain,acuity,chiefcomplaint\n"
                 "1,1,98.6,80,16,99,120,80,0,4,Headache\n2,2,101.2,130,28,88,90,60,8,1,Chest pain\n"
                 "3,3,,95,20,95,130,85,5,3,Dyspnea\n4,4,99,70,14,98,125,80,0,,Fall\n")
    df = load_mimic_ed(p)
    assert list(df["severity"]) == ["LOW", "CRITICAL", "MEDIUM"]  # row without acuity dropped
    assert df.loc[0, "temperature_c"] == pytest.approx(37.0)
    with pytest.raises(ValueError):
        load_ktas(p)


def test_train_on_ktas_format_and_predict(tmp_path):
    from app.ml.train import main as train
    p = tmp_path / "data.csv"
    write_ktas_fixture(p, 400)
    report = train(dataset="ktas", data_path=str(p), quiet=True, model_path=tmp_path / "m.joblib",
                   metrics_path=tmp_path / "metrics.json")
    assert report["dataset"]["name"] == "ktas" and "REAL" in report["dataset"]["description"]
    assert report["models"]["random_forest"]["accuracy"] > 0.5
    m = SeverityModel(tmp_path / "m.joblib")
    assert m.load() and m.dataset == "ktas"
    crit, mild = m.predict(CRITICAL_CASE), m.predict(MILD_CASE)
    order = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    assert order.index(crit.severity) > order.index(mild.severity)
    assert m.predict({**MILD_CASE, "oxygen_saturation": None}).severity in order   # missing vitals imputed


def test_osm_hospital_import_from_real_extract():
    hs = read_osm_hospitals(str(PROJECT / "data" / "maps" / "monaco.osm.pbf"), 43.7384, 7.4246, 5000)
    names = [h["name"] for h in hs]
    assert "Centre Cardio Thoracique de Monaco" in names
    rows = osm_to_rows(hs, 10)
    cardio = next(r for r in rows if r["name"].startswith("Centre Cardio"))
    assert cardio["cardiac_available"] is True and cardio["icu_available"] == 0 and cardio["data_source"] == "OSM"


def test_verified_hospital_csv_roundtrip(tmp_path):
    from app.database import session_scope
    from app.models import Hospital
    path = tmp_path / "h.csv"
    with session_scope() as db:
        n = export_csv(db, path)
    assert n >= 1
    rows = list(csv.DictReader(path.open()))
    rows[0]["icu_available"] = "7"
    rows[0]["stroke_available"] = "yes"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    with session_scope() as db:
        assert import_csv(db, path)["updated"] == n
    with session_scope() as db:
        h = db.get(Hospital, rows[0]["id"])
        assert h.icu_available == 7 and h.stroke_available and h.data_source == "VERIFIED"
