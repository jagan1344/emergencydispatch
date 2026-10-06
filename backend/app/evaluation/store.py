"""Persistence of experiments in experiment_runs / experiment_scenarios / experiment_results (migration 0005).
Each result is committed on its own, so a crash keeps everything measured so far."""
from __future__ import annotations

import uuid

from sqlalchemy import select

from app.database import session_scope
from app.models import ExperimentResult, ExperimentRun, ExperimentScenario
from app.utils.timeutil import utcnow


class ExperimentStore:
    def __init__(self, run_id: uuid.UUID):
        self.run_id = run_id

    @classmethod
    def create(cls, run_name: str, kind: str, strategies: list[str], configuration: dict, n: int, seed: int,
               meta: dict) -> "ExperimentStore":
        from app.evaluation.runner import jsonable
        rid = uuid.uuid4()
        with session_scope() as db:
            if db.scalar(select(ExperimentRun.id).where(ExperimentRun.run_name == run_name)) is not None:
                raise ValueError(f"an experiment named {run_name!r} already exists")
            db.add(ExperimentRun(id=rid, run_name=run_name, kind=kind, strategies=strategies,
                                 configuration=jsonable(configuration), scenario_count=n, random_seed=seed,
                                 status="RUNNING", metadata_=jsonable(meta)))
        return cls(rid)

    def add_scenario(self, sc: dict, summary: dict) -> uuid.UUID:
        from app.evaluation.runner import jsonable
        sid = uuid.uuid4()
        with session_scope() as db:
            db.add(ExperimentScenario(id=sid, experiment_run_id=self.run_id, scenario_number=sc["number"],
                                      random_seed=sc["seed"], fingerprint=sc["fingerprint"], summary=jsonable(summary),
                                      definition=jsonable(sc)))
        return sid

    def add_result(self, scenario_id: uuid.UUID, strategy: str, res: dict) -> None:
        from app.evaluation.runner import jsonable
        m = res.get("metrics") or {}
        with session_scope() as db:
            db.add(ExperimentResult(
                id=uuid.uuid4(), experiment_run_id=self.run_id, scenario_id=scenario_id, strategy=strategy,
                status=res["status"], error=res.get("error"),
                response_time_s=m.get("response_time_s"), patient_wait_s=m.get("patient_wait_s"),
                initial_eta_s=m.get("initial_eta_s"), actual_travel_s=m.get("actual_travel_s"),
                reroute_count=m.get("reroutes"), reroute_improvement_s=m.get("reroute_saved_s"),
                ambulance_utilization=m.get("ambulance_utilization"), hospital_wait_s=m.get("hospital_wait_simulated_s"),
                critical_delay_s=m.get("critical_delay_s"), resource_conflicts=m.get("resource_conflicts"),
                manual_interventions=m.get("manual_interventions"), automatic_decision_rate=m.get("automatic_decision_rate"),
                metrics=jsonable(m) if m else None, incidents=jsonable(res.get("incidents") or []),
                runtime_ms=res.get("runtime_ms")))

    def finish(self, status: str, summary: dict, meta: dict, output_dir: str | None) -> None:
        from app.evaluation.runner import jsonable
        with session_scope() as db:
            run = db.get(ExperimentRun, self.run_id)
            run.status, run.summary, run.metadata_ = status, jsonable(summary), jsonable(meta)
            run.output_dir, run.completed_at = output_dir, utcnow()

    def fail(self, error: str) -> None:
        with session_scope() as db:
            run = db.get(ExperimentRun, self.run_id)
            run.status, run.error, run.completed_at = "FAILED", error[:4000], utcnow()
