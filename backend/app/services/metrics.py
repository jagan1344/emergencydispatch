"""Prometheus metrics (exposed at /metrics)."""
from prometheus_client import Counter, Gauge, Histogram

EMERGENCIES = Counter("emergencies_total", "Emergency incidents created", ["severity"])
DISPATCHES = Counter("dispatches_total", "Ambulance dispatches", ["method"])
REROUTES = Counter("reroutes_total", "Dynamic route recalculations")
COMPLETIONS = Counter("incidents_completed_total", "Incidents completed")
RESPONSE_TIME = Histogram("response_time_seconds", "Simulated response time (creation → arrival at scene)",
                          buckets=(60, 120, 240, 360, 480, 600, 900, 1200, 1800, 3600))
DISPATCH_TIME = Histogram("dispatch_time_seconds", "Simulated time from creation to dispatch",
                          buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1200))
DECISION_LATENCY = Histogram("dispatch_decision_ms", "Wall-clock time to compute a dispatch decision (ms)",
                             buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 2500))
ACTIVE_AMBULANCES = Gauge("active_ambulances", "Ambulances currently on a mission")
AVAILABLE_AMBULANCES = Gauge("available_ambulances", "Ambulances available")
ACTIVE_INCIDENTS = Gauge("active_incidents", "Open incidents (not completed/cancelled)")
AVERAGE_RESPONSE = Gauge("average_response_time", "Average simulated response time of completed live incidents (s)")
AVERAGE_DISPATCH = Gauge("average_dispatch_time", "Average simulated dispatch time of live incidents (s)")
