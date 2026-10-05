from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import any_user, dispatcher
from app.models import User
from app.schemas.schemas import DemoScenarioRequest, SimulationStart
from app.services import simulation_service as sim
from app.services.state import STATE

router = APIRouter(prefix="/api/simulation", tags=["simulation"])


@router.post("/start")
async def start(body: SimulationStart, _: User = Depends(dispatcher)):
    """START SIMULATION: seeded, reproducible schedule of incidents and traffic events."""
    if STATE.router is None:
        raise HTTPException(503, "routing engine not ready")
    sim.start_task(sim.run_simulation(body.seed, body.ambulances, body.hospitals, body.incidents,
                                      body.traffic_events, body.duration_s))
    return {"started": True, **body.model_dump()}


@router.post("/demo-scenario")
async def demo(body: DemoScenarioRequest | None = None, _: User = Depends(dispatcher)):
    if STATE.router is None:
        raise HTTPException(503, "routing engine not ready")
    sim.start_task(sim.run_demo_scenario((body or DemoScenarioRequest()).seed))
    return {"started": True}


@router.post("/stop")
async def stop(_: User = Depends(dispatcher)):
    return {"stopped": sim.stop_task()}


@router.post("/reset")
async def reset(_: User = Depends(dispatcher)):
    import asyncio
    sim.stop_task()
    return await asyncio.to_thread(sim.reset_operations)


@router.get("/status")
async def status(_: User = Depends(any_user)):
    return sim.RUN.as_dict()


@router.get("/schedule-preview")
async def schedule_preview(seed: int = 42, incidents: int = 5, traffic_events: int = 5, duration_s: float = 60,
                           _: User = Depends(any_user)):
    """Shows the deterministic schedule for a seed (same seed → same events)."""
    import asyncio
    return await asyncio.to_thread(sim.build_schedule, seed, incidents, traffic_events, duration_s)
