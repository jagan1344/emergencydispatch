"""Explanations and counterfactuals for dispatch / hospital decisions.

Everything here is derived from the decision's own data (the candidate list with its score components,
ETAs, distances, fuel and workload that the scoring function produced). Nothing is invented: every
sentence is generated from a numeric difference between two candidates.

    contribution_k(c) = weight_k * component_k(c)                       (DispatchScore = sum_k contribution_k)
    counterfactual(sel, alt) = { delta ETA, delta distance, delta score, per-factor contribution deltas }
"""
from __future__ import annotations

from app.dispatch.scoring import DISPATCH_WEIGHTS, HOSPITAL_WEIGHTS, traffic_label

FACTOR_LABEL = {"eta": "ETA", "capability": "capability match", "traffic": "traffic delay", "workload": "workload",
                "fuel": "fuel", "distance": "distance", "load": "hospital load"}


def _min(s: float) -> str:
    return f"{s / 60:.1f} min"


def factor_ratings(c: dict) -> list[dict]:
    """Per-factor value + status (ok / warn / bad) for one ambulance candidate."""
    match = c.get("capability_match", 0)
    fuel = c.get("fuel_level")
    missions = c.get("missions_today")
    t = traffic_label(c.get("traffic_delay_s", 0), c.get("eta_s", 0))
    out = [
        {"factor": "eta", "label": "ETA", "value": round(c["eta_s"], 1), "display": _min(c["eta_s"]), "status": "info"},
        {"factor": "distance", "label": "Distance", "value": round(c["distance_m"], 1),
         "display": f"{c['distance_m'] / 1000:.2f} km", "status": "info"},
        {"factor": "capability", "label": "Capability", "value": match,
         "display": f"{c.get('equipment_level')} ({'perfect' if match == 1 else 'acceptable' if match > 0 else 'unsuitable'})",
         "status": "ok" if match == 1 else "warn" if match > 0 else "bad"},
        {"factor": "traffic", "label": "Traffic", "value": round(c.get("traffic_delay_s", 0), 1),
         "display": f"{t} (+{c.get('traffic_delay_s', 0):.0f} s)", "status": {"Low": "ok", "Moderate": "warn"}.get(t, "bad")},
    ]
    if missions is not None:
        lvl = "LOW" if missions <= 1 else "MEDIUM" if missions <= 3 else "HIGH"
        out.append({"factor": "workload", "label": "Workload", "value": missions,
                    "display": f"{lvl} ({missions} missions today)", "status": {"LOW": "ok", "MEDIUM": "warn"}.get(lvl, "bad")})
    if fuel is not None:
        out.append({"factor": "fuel", "label": "Fuel", "value": fuel, "display": f"{fuel:.0f}%",
                    "status": "ok" if fuel >= 50 else "warn" if fuel >= 25 else "bad"})
    return out


def contributions(c: dict, weights: dict[str, float] = DISPATCH_WEIGHTS) -> dict[str, float]:
    return {k: round(weights[k] * c["components"][k], 4) for k in weights if k in c["components"]}


def _advantages(sel: dict, alt: dict) -> list[str]:
    """Statements where `sel` is better than `alt`, from actual values (only material differences)."""
    out = []
    d_eta = alt["eta_s"] - sel["eta_s"]
    if d_eta >= 15:
        out.append(f"{_min(d_eta)} lower ETA ({_min(sel['eta_s'])} vs {_min(alt['eta_s'])})")
    if sel.get("capability_match", 0) > alt.get("capability_match", 0):
        out.append(f"better-matched equipment ({sel.get('equipment_level')} vs {alt.get('equipment_level')}"
                   f"{' - unsuitable' if alt.get('capability_match', 0) == 0 else ''})")
    if (alt.get("missions_today") or 0) - (sel.get("missions_today") or 0) >= 1:
        out.append(f"lower workload ({sel.get('missions_today')} vs {alt.get('missions_today')} missions today)")
    if sel.get("fuel_level") is not None and alt.get("fuel_level") is not None and sel["fuel_level"] - alt["fuel_level"] >= 10:
        out.append(f"more fuel ({sel['fuel_level']:.0f}% vs {alt['fuel_level']:.0f}%)")
    if alt.get("traffic_delay_s", 0) - sel.get("traffic_delay_s", 0) >= 15:
        out.append(f"less traffic delay ({sel['traffic_delay_s']:.0f} s vs {alt['traffic_delay_s']:.0f} s)")
    if alt["distance_m"] - sel["distance_m"] >= 300:
        out.append(f"shorter distance ({sel['distance_m'] / 1000:.2f} vs {alt['distance_m'] / 1000:.2f} km)")
    return out


def counterfactual(sel: dict, alt: dict, method: str = "WEIGHTED_SCORE",
                   weights: dict[str, float] = DISPATCH_WEIGHTS, id_key: str = "ambulance_id") -> dict:
    """What would have happened if `alt` had been chosen instead of `sel`?"""
    cs, ca = contributions(sel, weights), contributions(alt, weights)
    deltas = {k: round(ca[k] - cs[k], 4) for k in cs}           # + : alternative is worse on this factor
    worse = sorted([k for k, v in deltas.items() if v > 0.001], key=lambda k: -deltas[k])
    better = sorted([k for k, v in deltas.items() if v < -0.001], key=lambda k: deltas[k])
    d_eta = round(alt["eta_s"] - sel["eta_s"], 1)
    d_score = round(alt["score"] - sel["score"], 4)
    aid, sid = alt[id_key], sel[id_key]
    if not alt.get("suitable", True):
        why_not = f"{aid} is unsuitable for this patient ({alt.get('equipment_level')} equipment)."
    elif d_score < 0:
        why_not = (f"{aid} had a better individual score ({alt['score']:.3f} vs {sel['score']:.3f}) but was not used: "
                   + ("the global OR-Tools assignment gave it to a higher-priority incident." if method == "ORTOOLS_ASSIGNMENT"
                      else "the dispatcher manually chose another unit." if method == "MANUAL"
                      else "it was not available when the decision was committed."))
    else:
        main = ", ".join(f"{FACTOR_LABEL[k]} (+{deltas[k]:.3f})" for k in worse[:2]) or "overall score"
        why_not = (f"{aid} scores {d_score:.3f} worse ({alt['score']:.3f} vs {sel['score']:.3f}), mainly because of {main}"
                   + (f"; it would arrive {_min(abs(d_eta))} {'later' if d_eta > 0 else 'earlier'}" if abs(d_eta) >= 6 else "")
                   + ".")
    tradeoffs = [f"{aid} is better on {FACTOR_LABEL[k]} ({deltas[k]:+.3f})" for k in better]
    codes = rejection_codes(sel, alt, deltas, method) if id_key == "ambulance_id" else None
    return {
        "rejection_codes": codes,
        id_key: aid, "versus": sid, "score": alt["score"], "score_delta": d_score, "eta_s": alt["eta_s"],
        "eta_delta_s": d_eta, "distance_delta_m": round(alt["distance_m"] - sel["distance_m"], 1),
        "factor_deltas": deltas, "worse_on": worse, "better_on": better, "why_not": why_not, "tradeoffs": tradeoffs,
        "outcome": (f"If {aid} had been chosen: arrival in {_min(alt['eta_s'])} "
                    f"({'+' if d_eta >= 0 else '-'}{_min(abs(d_eta))} vs {sid}).") if "eta_s" in alt else None,
    }


def rejection_codes(sel: dict, alt: dict, deltas: dict, method: str) -> list[str]:
    """Structured reasons an alternative ambulance was not selected (derived from the stored candidate values)."""
    if not alt.get("suitable", True):
        return ["UNSUITABLE_CAPABILITY"]
    if alt["score"] < sel["score"]:
        return [{"ORTOOLS_ASSIGNMENT": "ASSIGNED_TO_HIGHER_PRIORITY", "MANUAL": "MANUAL_OVERRIDE"}.get(
            method, "UNAVAILABLE_AT_COMMIT")]
    codes = []
    if alt["eta_s"] - sel["eta_s"] >= 15:
        codes.append("TOO_SLOW")
    for factor, code in (("traffic", "HEAVY_TRAFFIC"), ("capability", "LOWER_CAPABILITY_MATCH"),
                         ("workload", "HIGHER_WORKLOAD"), ("fuel", "LOW_FUEL"), ("distance", "LONGER_DISTANCE")):
        if deltas.get(factor, 0) > 0.005:
            codes.append(code)
    return codes or ["HIGHER_SCORE"]


def explain_selection(ranked: list[dict], selected_id: str, method: str = "WEIGHTED_SCORE",
                      weights: dict[str, float] = DISPATCH_WEIGHTS, id_key: str = "ambulance_id") -> dict:
    sel = next(c for c in ranked if c[id_key] == selected_id)
    alts = [c for c in ranked if c[id_key] != selected_id]
    cfs = [counterfactual(sel, a, method, weights, id_key) for a in alts]
    runner = next((a for a in alts if a.get("suitable", True)), alts[0] if alts else None)
    reasons = _advantages(sel, runner) if runner else []
    if not reasons and runner is not None:
        reasons = [f"lowest overall score ({sel['score']:.3f} vs {runner['score']:.3f})"]
    if method == "REALLOCATION":
        summary = (f"{selected_id} was reallocated from a lower-priority incident: no free compatible unit could arrive "
                   f"in time (see Resource conflicts for the policy check, ETAs and impact).")
    elif method == "REALLOCATION_REPLACEMENT":
        summary = f"{selected_id} replaces the unit that was reallocated to a higher-priority incident."
    else:
        summary = (f"{selected_id} was selected because it had: " + "; ".join(reasons) + "."
                   if runner is not None else f"{selected_id} was the only candidate.")
    if runner is not None and runner["eta_s"] < sel["eta_s"] - 15:      # state the trade-off, never hide it
        summary += (f" Trade-off: {runner[id_key]} would have arrived {_min(sel['eta_s'] - runner['eta_s'])} earlier "
                    f"but scores worse overall ({runner['score']:.3f} vs {sel['score']:.3f}).")
    return {"selected": selected_id, "score": sel["score"], "contributions": contributions(sel, weights),
            "factors": factor_ratings(sel) if id_key == "ambulance_id" else None,
            "reasons": reasons, "summary": summary, "runner_up": runner[id_key] if runner else None,
            "candidates_considered": len(ranked), "counterfactuals": cfs, "weights": weights}


def explain_hospital(ranked: list[dict], selected_id: str) -> dict:
    """Hospital counterfactuals (time-to-treatment = ETA + expected wait)."""
    rows = [{**h, "eta_s": h.get("time_to_treatment_s", h["eta_s"]), "suitable": not h["missing_capabilities"]}
            for h in ranked]
    out = explain_selection(rows, selected_id, weights=HOSPITAL_WEIGHTS, id_key="hospital_id")
    out["factors"] = None
    from app.dispatch.scoring import hospital_why_not
    best = next(h for h in ranked if h["hospital_id"] == selected_id)
    from app.dispatch.scoring import hospital_rejection_codes
    out["why_not"] = [{"hospital_id": h["hospital_id"], "name": h.get("name"),
                       "reasons": hospital_why_not(best, {**h, "unknown_capabilities": h.get("unknown_capabilities") or []}),
                       "rejection_codes": hospital_rejection_codes(best, {**h, "unknown_capabilities": h.get("unknown_capabilities") or []})}
                      for h in ranked if h["hospital_id"] != selected_id]
    return out
