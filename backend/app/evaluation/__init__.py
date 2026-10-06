"""Research evaluation layer: controlled progressive-baseline and ablation experiments.

The evaluation replays the SAME seeded scenarios (patients, fleet, hospitals, traffic, disruptions) under
different decision strategies in a deterministic discrete-event simulation. Every decision is taken by the
production decision kernels (triage, DispatchScore, OR-Tools assignment, routing engine on the real road graph,
traffic and hospital prediction, reallocation policy, re-route rules, explanations); only the strategy
configuration decides which of them are switched on. The live system, its database tables and its in-memory
road graph are never modified (experiments run on a clone of the graph and write only experiment_* tables).
"""
