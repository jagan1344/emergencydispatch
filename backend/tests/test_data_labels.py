"""Data-quality labels: hospital capabilities (verified / OSM / unknown / simulated), hospital ranking with unknown
capabilities, hospital why-not reasons, and the SIMULATION source labels of traffic and hospital predictions."""
from app.dispatch.scoring import HospitalInput, hospital_why_not, score_hospitals
from app.models import Hospital
from app.services.mission_service import capability_status, unknown_capabilities


def _h(src, icu=0, trauma=False, cardiac=False, stroke=False):
    return Hospital(id="H", name="H", latitude=0, longitude=0, emergency_capacity=20, icu_available=icu,
                    trauma_available=trauma, cardiac_available=cardiac, stroke_available=stroke, data_source=src)


def test_capability_status_never_turns_missing_osm_tags_into_no():
    osm = capability_status(_h("OSM", cardiac=True))
    assert osm == {"icu": "UNKNOWN", "trauma": "UNKNOWN", "cardiac": "OSM_TAG_YES", "stroke": "UNKNOWN"}
    assert unknown_capabilities(_h("OSM", cardiac=True)) == {"icu", "trauma", "stroke"}
    assert capability_status(_h("VERIFIED", icu=2))["icu"] == "VERIFIED_YES"
    assert capability_status(_h("VERIFIED"))["trauma"] == "VERIFIED_NO"
    assert capability_status(_h("SYNTHETIC", stroke=True))["stroke"] == "SIMULATED_YES"
    assert unknown_capabilities(_h("VERIFIED")) == set()


def _in(hid, eta, icu, unknown=frozenset(), load=5):
    return HospitalInput(hid, hid, eta, 0.0, eta * 10, load, 20, icu, False, False, False, unknown=set(unknown))


def test_unknown_capability_ranks_between_confirmed_and_lacking():
    ranked = score_hospitals([_in("NO-ICU", 100, 0), _in("UNKNOWN", 300, 0, {"icu"}), _in("ICU", 600, 1)], ["icu"])
    assert [h["hospital_id"] for h in ranked] == ["ICU", "UNKNOWN", "NO-ICU"]
    assert ranked[1]["unknown_capabilities"] == ["icu"] and ranked[2]["unknown_capabilities"] == []
    # without unknowns the order is exactly the previous one (capability first, then score)
    old = score_hospitals([_in("A", 100, 0), _in("B", 600, 1)], ["icu"])
    assert [h["hospital_id"] for h in old] == ["B", "A"]
    why = hospital_why_not(ranked[0], ranked[2])
    assert any(w.startswith("lacks icu") for w in why)
    assert any(w.startswith("capability unknown: icu") for w in hospital_why_not(ranked[0], ranked[1]))
    busy = score_hospitals([_in("FREE", 300, 1, load=2), _in("BUSY", 280, 1, load=19)], [])
    assert busy[0]["hospital_id"] == "FREE"                         # 20 s slower but far less loaded
    assert any(w.startswith("higher (predicted) load") for w in hospital_why_not(busy[0], busy[1]))


def test_prediction_sources_are_labelled(client, viewer_headers):
    t = client.get("/api/traffic/predictions", headers=viewer_headers).json()["model"]
    assert t["traffic_source"] == "SIMULATION" and t["label"] in ("LEARNED", "FALLBACK", None)
    if t["label"]:
        assert t["reason"] and t["confidence_meaning"]
    hp = client.get("/api/hospitals/predictions", headers=viewer_headers).json()["predictions"][0]
    assert hp["source"] == "SIMULATION" and hp["estimated"] is True
    h = client.get("/api/hospitals", headers=viewer_headers).json()[0]
    assert h["load_source"] == "SIMULATION" and set(h["capability_status"]) == {"icu", "trauma", "cardiac", "stroke"}
