"""Research evaluation API: list stored experiments, read their comparison/statistics, start a (bounded) run.

Large experiments should be run with the CLI (python -m app.evaluation.run_experiment); a run started here
executes in one background thread of the backend, one at a time, limited to MAX_API_SCENARIOS scenarios.
"""
from __future__ import annotations

import threading
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import admin, any_user
from app.database import get_db
from app.evaluation.strategies import ABLATIONS, ALL, PROGRESSIVE
from app.models import ExperimentResult, ExperimentRun, ExperimentScenario, User

router = APIRouter(prefix="/api/evaluation", tags=["evaluation"])
MAX_API_SCENARIOS = 200
_JOB: dict = {"state": "IDLE"}
_JOB_LOCK = threading.Lock()


class RunRequest(BaseModel):
    progressive: bool = True
    ablation: bool = False
    strategies: list[str] = Field(default_factory=list)
    scenarios: int = Field(default=20, ge=1, le=MAX_API_SCENARIOS)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)
    name: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")


def run_dict(r: ExperimentRun, full: bool = False) -> dict:
    meta = r.metadata_ or {}
    d = {"id": str(r.id), "run_name": r.run_name, "kind": r.kind, "strategies": r.strategies, "status": r.status,
         "scenario_count": r.scenario_count, "random_seed": r.random_seed, "started_at": r.started_at,
         "completed_at": r.completed_at, "error": r.error, "output_dir": r.output_dir,
         "git_commit": meta.get("git_commit"), "city": (meta.get("city") or {}).get("name"),
         "traffic_model": (meta.get("traffic_model") or {}).get("label"),
         "routing": meta.get("routing")}
    if full:
        d["metadata"] = {k: v for k, v in meta.items() if k != "hospital_templates"}
        d["configuration"] = r.configuration
        d["summary"] = r.summary
    return d


@router.get("/strategies")
def strategies(_: User = Depends(any_user)):
    return {"progressive": [dict(name=s.name, **s.flags()) for s in PROGRESSIVE.values()],
            "ablations": [dict(name=s.name, **s.flags()) for s in ABLATIONS.values()]}


@router.get("/runs")
def list_runs(limit: int = 50, _: User = Depends(any_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(ExperimentRun).order_by(ExperimentRun.started_at.desc()).limit(min(limit, 200)))
    return [run_dict(r) for r in rows]


@router.get("/runs/{run_id}")
def get_run(run_id: str, _: User = Depends(any_user), db: Session = Depends(get_db)):
    r = _get(db, run_id)
    out = run_dict(r, full=True)
    out["results_stored"] = db.query(ExperimentResult).filter(ExperimentResult.experiment_run_id == r.id).count()
    return out


@router.get("/runs/{run_id}/results")
def run_results(run_id: str, strategy: str | None = None, _: User = Depends(any_user), db: Session = Depends(get_db)):
    r = _get(db, run_id)
    q = select(ExperimentResult, ExperimentScenario.scenario_number).join(
        ExperimentScenario, ExperimentScenario.id == ExperimentResult.scenario_id).where(ExperimentResult.experiment_run_id == r.id)
    if strategy:
        q = q.where(ExperimentResult.strategy == strategy.upper())
    return [{"scenario": n, "strategy": x.strategy, "status": x.status, "error": x.error, "metrics": x.metrics,
             "runtime_ms": x.runtime_ms} for x, n in db.execute(q.order_by(ExperimentScenario.scenario_number))]


@router.get("/job")
def job(_: User = Depends(any_user)):
    return dict(_JOB)


@router.post("/runs", status_code=202)
def start_run(body: RunRequest, user: User = Depends(admin)):
    """ADMIN only: an experiment is CPU-intensive and runs inside the backend process."""
    from app.evaluation.runner import default_strategies
    from app.evaluation.strategies import resolve
    try:
        chosen = default_strategies(body.progressive, body.ablation) + resolve(body.strategies)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    seen, uniq = set(), []
    for s in chosen:
        if s.name not in seen:
            seen.add(s.name)
            uniq.append(s)
    if not uniq:
        raise HTTPException(422, f"choose progressive, ablation or strategies from {', '.join(ALL)}")
    with _JOB_LOCK:
        if _JOB.get("state") == "RUNNING":
            raise HTTPException(409, f"experiment {_JOB.get('run_name')} is still running")
        name = body.name or f"api-{uuid.uuid4().hex[:8]}"
        _JOB.clear()
        _JOB.update(state="RUNNING", run_name=name, done=0, total=body.scenarios * len(uniq), started_by=user.username,
                    strategies=[s.name for s in uniq])

    def work():
        from app.evaluation.runner import build_context, run_experiment
        try:
            ctx = build_context()
            res = run_experiment(ctx, uniq, body.scenarios, body.seed, run_name=name,
                                 progress=lambda d, t: _JOB.update(done=d, total=t))
            _JOB.update(state=res["status"], run_id=res["run_id"], output_dir=res["output_dir"])
        except Exception as exc:  # reported to the UI; the run row (if created) is marked FAILED by the runner
            _JOB.update(state="FAILED", error=f"{type(exc).__name__}: {exc}")

    threading.Thread(target=work, name=f"experiment-{name}", daemon=True).start()
    return dict(_JOB)


def _get(db: Session, run_id: str) -> ExperimentRun:
    try:
        rid = uuid.UUID(run_id)
    except ValueError:
        r = db.scalar(select(ExperimentRun).where(ExperimentRun.run_name == run_id))
    else:
        r = db.get(ExperimentRun, rid)
    if r is None:
        raise HTTPException(404, "experiment not found")
    return r
