"""Feature 6: configurable re-route rules - margin, cooldown, oscillation guard."""
import math

from app.config import get_settings
from app.services.routes_service import reroute_decision


def d(old, new, since=None, sim=0.0, forced=False):
    return reroute_decision(old, new, since_last_s=since, similarity=sim, forced=forced, st=get_settings())


def test_margin_rules():
    st = get_settings()
    assert st.reroute_min_eta_savings_s == 20 and st.reroute_min_improvement_percent == 5
    assert d(726, 498)[0]                       # 12.1 min -> 8.3 min: accepted
    ok, why = d(600, 590)                       # 10 s saving < max(20 s, 30 s)
    assert not ok and "below the required margin" in why
    assert not d(600, 575)[0] and d(600, 565)[0]   # margin = 30 s (5 % of 600)


def test_cooldown_and_blocked_override():
    st = get_settings()
    ok, why = d(726, 498, since=st.reroute_cooldown_s / 2)
    assert not ok and "cooldown" in why
    assert d(726, 498, since=st.reroute_cooldown_s + 1)[0]
    assert d(math.inf, 900, since=1)[0]                                   # blocked route: always re-route
    assert d(726, 498, since=1, forced=True)[0]


def test_oscillation_guard():
    ok, why = d(726, 498, since=10_000, sim=0.9)
    assert not ok and "oscillation" in why
    assert d(726, 498, since=10_000, sim=0.3)[0]
    assert not d(500, math.inf)[0]
