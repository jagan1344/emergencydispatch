# Evaluation outputs (research evidence)

All outputs in this folder were produced by the commands below in the development container, using the bundled
**Monaco** OpenStreetMap extract (`data/maps/monaco.osm.pbf`) and an OSRM build of that extract. The severity model
was trained on **synthetic** data, and calls, traffic and hospital load are **simulated**. **No Bengaluru map,
OSRM build or Bengaluru data was used.** These numbers do not describe Bengaluru or any real emergency service,
and they are not clinical results. A Bengaluru installation must regenerate them.

| Path | Produced by | Notes |
|---|---|---|
| `severity_calibration.json` | `python -m app.ml.train` (synthetic dataset, seed 42) | raw vs calibrated Brier / ECE on the untouched 20 % test split of model `rf-20261006172011`; matches `backend/app/ml/artifacts/metrics.json` |
| `traffic_prediction.json` | `python -m app.evaluation.traffic_eval` | learned model vs rule fallback vs persistence on the time-ordered hold-out of the stored **simulated** traffic history (Monaco dev database) |
| `results/upgrade-s42-n100/` | `python -m app.evaluation.run_experiment --all --ablation --scenarios 100 --seed 42 --osrm --name upgrade-s42-n100` | **current results** cited in the README (§12b); 100 scenarios × 11 configurations = 1 100 runs |
| `results/final-s42-n100/` | same command, `--name final-s42-n100` | earlier run (calibrated model, before the route-checkpoint / new-metric changes); kept for history |
| `results/research-s42-n100/` | same command, `--name research-s42-n100` | first 100-scenario run: raw RandomForest and walking-pace closure fallback (before the reliability changes) |
| `results/step7-all-s42-n5/`, `results/step9-all-s42-n10/` | `--all --scenarios 5` / `10` | small smoke runs from the initial evaluation work (FALLBACK traffic model) |

`run_metadata.json` in each run records the git commit, branch and whether the working tree had uncommitted changes
(`git_dirty`). The runs `final-s42-n100` and `upgrade-s42-n100` were made from the working tree on top of commit
`a2116ca` **before** the changes were committed. `upgrade-s42-n100` corresponds to the code in the commits that
added this file. Runs started from the Evaluation page (`results/api-*`) are not kept (`.gitignore`).

Re-running with the same seed, data, model and code reproduces the scenarios exactly. The results also depend on the
trained traffic model, which is retrained on the database's simulated traffic history at the start of each run
(its version and hold-out metrics are stored in `run_metadata.json`).
