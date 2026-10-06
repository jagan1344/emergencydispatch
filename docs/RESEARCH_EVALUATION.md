# Research evaluation: progressive baselines and ablation

## Research question

> Does progressively adding severity intelligence, traffic intelligence, hospital intelligence,
> confidence-aware decision-making, resource reallocation and dynamic re-routing improve emergency
> response compared with simpler dispatch strategies?

**Contribution (what is and is not claimed).** The contribution is not a new standalone ambulance ML
algorithm. It is a confidence-aware, explainable, closed-loop emergency decision-support architecture and
its empirical evaluation through controlled ablation and progressive-baseline experiments. The experiments
are **simulations**:
- the patients, traffic and hospital load are simulated;
- severity is an estimate;
- waiting times are predicted or simulated.

No clinical validity, diagnosis or real-world emergency-service performance is claimed, and the system is
not claimed to be the first of its kind.

## How the evaluation connects to the existing system

The evaluation layer (`backend/app/evaluation/`) is an extension of the running system. It is not a
second application. Every decision in an experiment is taken by the same production code that serves the
live dispatcher:

| Decision | Production code reused |
|---|---|
| Triage: rule score + ML severity + confidence decision mode | `dispatch/triage.py` (also used by live intake `incident_service.classify`), `ml/predict.SeverityModel`, `dispatch/confidence.assess` |
| Queue order | `dispatch/priority.priority_score` |
| Ambulance ranking | `dispatch/scoring.score_candidates` (DispatchScore), `dispatch/optimizer.optimal_assignment` (OR-Tools) |
| Routing | `routing/engine.RoutingEngine` on a clone of the real OSM road graph, plus OSRM alternatives when available |
| Traffic prediction | `ml/traffic_model.TrafficModel`, trained with the live procedure (`services/traffic_prediction.TrafficPredictor`) |
| Hospital choice | `services/hospital_prediction.forecast`, `dispatch/scoring.score_hospitals` |
| Reallocation policy | `services/reallocation.needs_reallocation / donor_eligible / gain_sufficient / reallocation_outcome` (also used by live `consider_reallocation`) |
| Re-routing | `services/routes_service.reroute_trigger` (also used by live `check_routes`), `reroute_decision`, `_similarity` |
| Explanations | `dispatch/explain.explain_selection`, `dispatch/scoring.explain_dispatch / explain_hospital` |

The pure kernels were extracted from the live services without changing their behaviour: all 69
pre-existing tests still pass. The live graph is never mutated, because experiments run on
`RoadGraph.clone()`. The operational tables are never written: a test checks that their row counts are
unchanged. Experiments write only the new `experiment_*` tables.

## Systems A–E

| | Strategy | Adds | Decision logic |
|---|---|---|---|
| A | **BASELINE** | – | FIFO queue; nearest available unit by road distance (static routing: free-flow time, known closures avoided, congestion ignored); nearest hospital. No triage, so no capability matching. |
| B | **SEVERITY** | triage | ML + rule severity drives the PriorityScore queue, the required unit capability (nearest *suitable* unit) and the hospital capability requirements (nearest capable hospital). |
| C | **TRAFFIC** | live traffic + traffic prediction | Traffic-aware routing with predicted traffic; units ranked by the production DispatchScore on traffic-adjusted ETAs; fastest capable hospital. |
| D | **HOSPITAL** | hospital intelligence | Hospital congestion forecast at the arrival time, and HospitalScore (time to treatment, predicted load, capability). |
| E | **FULL** | everything | Confidence-aware decision modes (HUMAN_REVIEW gate), resource reallocation, telemetry-driven re-routing, explanations, and OR-Tools batch assignment. This is the live dispatcher's logic. |

**Ablation.** Each run removes exactly one capability from FULL:
- FULL−CONFIDENCE
- FULL−TRAFFIC (traffic *prediction* off; live traffic stays)
- FULL−HOSPITAL
- FULL−RESOURCE_REALLOCATION
- FULL−DYNAMIC_REROUTING (crews then re-plan only when they physically reach a closed road)
- FULL−EXPLAINABILITY

These are evaluation flags (`app/evaluation/strategies.py`). The live application is unaffected and always
runs FULL.

**Baseline fairness.** The baseline is a reasonable conventional system, not a broken one:
- it uses the same road network;
- it avoids known closures;
- it routes on free-flow travel time;
- it always dispatches the nearest available unit.

It does not do capability matching, because it has no severity estimate. That is the capability under
test, and its cost is measured by the "unsuitable first unit" metric.

## Experimental setup

* **Same scenarios for every strategy.** Scenarios are generated once and replayed under every strategy.
  Only the decision strategy changes.
* **Seeds.** Scenario *k* uses its own RNG seeded with `(seed, k)`. It is identical whether 10 or 500
  scenarios are generated, and it carries a SHA-256 fingerprint that is stored with the run.
* **Scenario contents** (`app/evaluation/scenarios.py`):
  * **Calls:** 3–8 calls within a 30-minute window. Half of the scenarios contain a burst of 3
    near-simultaneous calls (competing emergencies).
  * **Patients:** simulated patients come from the project's synthetic generator, which also provides
    the *simulated label* used for under-triage and hospital-capability checks.
  * **Report quality:** 35 % of calls are reported with observation noise (uncertain call-time
    information). Triage sees only the reported case.
  * **Fleet:** 2–10 ambulances with mixed BASIC / ADVANCED / ICU equipment, fuel and workload. 20 % of
    units are committed elsewhere until a given time.
  * **Hospitals:** the database hospitals with a LOW / MEDIUM / HIGH load regime, ICU beds, background
    arrivals and lengths of stay.
  * **Traffic regime:** FREE / LIGHT / MODERATE / SEVERE on the major roads.
  * **Disruptions:** blockages, accidents and congestion increases placed on the likely approach route of a
    call, plus unrelated disruptions elsewhere.
  * **Other dynamic events:** an ambulance breakdown and hospital surges.
* **Simulation** (`app/evaluation/simulator.py`):
  * deterministic time steps of 2 s;
  * movement along the planned road segments at the *true* current speed of every road;
  * on-scene and handover times;
  * the hospital load evolves with admissions and discharges.
  
  The live dispatcher, route monitor and traffic-prediction intervals are reproduced in simulated time
  (12 s, 20 s and 120 s).
* **Simulated dispatcher (stated assumptions).**
  * A HUMAN_REVIEW call is reviewed after the scenario's review delay (30–150 s). The reviewer is assumed
    to determine the simulated label.
  * If the delay exceeds `HUMAN_REVIEW_TIMEOUT_S`, the live timeout policy applies instead (dispatch with
    the most severe plausible level).
  * An ESCALATED reallocation is **rejected** by default (`--escalation approve` to change).
* **Models and data recorded with every run:**
  * git commit and branch;
  * severity model version and dataset;
  * traffic model version and whether it is **LEARNED** (RandomForest that beat persistence) or
    **FALLBACK**;
  * hospital model (`hospital-queue-v1`, an estimate);
  * routing source (OSM graph and OSRM status);
  * city, road-network fingerprint, all thresholds, and timestamps.

## Metrics

All times are in simulated seconds. `NULL` means not measurable, never zero. Per call *i*: created at
*cᵢ*, first assigned at *dᵢ*, unit arrives at *aᵢ*, patient picked up at *pᵢ*.

| Metric | Definition |
|---|---|
| Response time | aᵢ − cᵢ |
| Patient waiting | pᵢ − cᵢ (response + scenario-defined on-scene time) |
| Dispatch delay | dᵢ − cᵢ (queue + review) |
| Initial / final route ETA | the strategy's own ETA when the unit was assigned / after the last re-route. Static strategies only know free-flow ETAs. |
| Actual travel | aᵢ − assignment time of the arriving unit (ETA ≠ travel time; \|actual − final ETA\| is reported) |
| Re-route improvement | Σ (old ETA − new ETA) over accepted re-routes with finite old ETA; % = mean of 100·(old − new)/old |
| Reactive detours | re-plans forced by reaching a closed road |
| Ambulance utilization | Σ busy time in the call window / (units × window); busy = committed or on a mission. Higher is **not** automatically better. |
| No unit available / reserve | share of the window with zero available units / time-average available units ÷ fleet |
| Hospital wait | *predicted*: the forecast at selection (D, E only); *simulated*: slot·ρ/(1−ρ) at arrival with ρ = simulated load ÷ capacity |
| Critical delay | for calls whose simulated label is CRITICAL: max(0, response − target), target 480 s (`--critical-target-s`); also the share over target and the worst case |
| Review rate / automatic decision rate | share of calls held for HUMAN_REVIEW / 1 − dispatcher interventions ÷ dispatch decisions |
| Potentially inappropriate automatic dispatch | calls with ML confidence < `DISPATCH_CONFIDENCE_LOW` dispatched without review. Operational metric with no clinical ground truth. |
| Under-triage | share of calls whose severity used by the system is below the simulated label (the generator label, not clinical truth) |
| Resource conflicts | detected, automatic reallocations, escalated (approved / rejected), estimated donor delay, estimated requester delay avoided |
| Manual interventions | reviews + reallocation decisions. The simulated dispatcher performs no manual dispatch, re-route or hospital override, so those counts are 0. |

## Statistics

* **Descriptive statistics** per strategy and metric over scenarios: mean, median, SD, min, max, P25, P75.
* **Paired comparisons** (same scenarios): against BASELINE, each system against the previous one
  (incremental contribution), and each ablation against FULL.
* **Test selection.** Shapiro–Wilk on the paired differences decides between a paired t-test (normal) and
  the Wilcoxon signed-rank test (non-normal).
* **Confidence intervals.** 95 % CI of the mean difference: t-interval, or a percentile bootstrap for
  non-normal differences.
* **When no test is run.** No test with fewer than 10 pairs, or when the differences are all zero.
* **Multiple comparisons.** p-values are Holm–Bonferroni adjusted within each comparison family. Only
  adjusted p < 0.05 is called significant.

## Commands

```powershell
cd backend
python -m app.migrate                                                          # adds the experiment_* tables
python -m app.evaluation.run_experiment --strategy BASELINE --scenarios 5 --seed 42
python -m app.evaluation.run_experiment --all --scenarios 100 --seed 42       # systems A-E
python -m app.evaluation.run_experiment --ablation --scenarios 100 --seed 42  # FULL and FULL-minus-one
python -m app.evaluation.run_experiment --all --ablation --scenarios 100 --seed 42 --name my-run
#   options: --osrm / --no-osrm, --traffic-model trained|fallback, --escalation reject|approve,
#            --critical-target-s 480, --duration-min 30, --dt 2, --no-db
```

**Outputs.** Each run writes `evaluation/results/<run>/` containing:
- `REPORT.md`
- `comparison_summary.csv`, `scenario_results.csv`, `incident_results.csv`, `statistics.csv`,
  `paired_tests.csv`, `scenarios_summary.csv`
- `scenarios.json`: complete definitions
- `summary.json`, `run_metadata.json`
- `plots/*.svg`

Runs are also stored in the `experiment_runs`, `experiment_scenarios` and `experiment_results` tables, and
shown on the **Evaluation** page (`/evaluation`). An ADMIN can start a run of up to 200 scenarios there.

## Limitations

* **Simulated calls and traffic.** Emergencies, patient observations and traffic dynamics are simulated.
  Traffic ground truth is the scenario's events.
* **Hospital load and waits** are estimates from a queueing approximation, and the "simulated" wait uses
  the same formula on the simulated load. They are not real ED data.
* **Model confidence** is the RandomForest class probability on synthetic data. It is not clinical
  confidence.
* **Simulated labels.** Under-triage and capability checks use the generator's label, which is not a
  clinical truth.
* **Simulated reviewer.** It is assumed to recover that label; real reviewers are imperfect.
* **Traffic prediction may be the FALLBACK.** The learned model is used only when it beats persistence on
  the stored history; otherwise the transparent FALLBACK is used, and the run records which one.
* **Routing data.** OSM/OSRM represent the road network, not live conditions. OSM speed limits are often
  defaults, and OSRM is free-flow (it is re-costed with the scenario traffic).
* **Fuel** is not consumed during an experiment.
* **Reallocation scope.** One reallocation per dispatcher cycle (live behaviour); escalations are decided
  instantly by the simulated dispatcher.
* **Sample size.** Results depend on the city graph, the hospital list and the scenario distribution. A
  different city (for example Bengaluru) must be re-run, and the numbers must not be transferred.
