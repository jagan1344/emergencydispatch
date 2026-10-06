import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import any_user, dispatcher
from app.database import get_db
from app.models import ResourceConflict, User
from app.services.decision_service import conflict_dict
from app.services.state import STATE

router = APIRouter(prefix="/api/dispatch", tags=["dispatch"])


@router.get("/conflicts")
def conflicts(open_only: bool = False, limit: int = 100, _: User = Depends(any_user), db: Session = Depends(get_db)):
    """Competing emergencies: contested units, automatic reallocations and escalations awaiting a dispatcher."""
    q = select(ResourceConflict)
    if open_only:
        q = q.where(ResourceConflict.decision == "ESCALATED")
    return [conflict_dict(c) for c in db.scalars(q.order_by(ResourceConflict.created_at.desc()).limit(min(limit, 1000)))]


def _get(db: Session, cid: str) -> ResourceConflict:
    try:
        c = db.get(ResourceConflict, uuid.UUID(cid), with_for_update=True)
    except ValueError:
        c = None
    if c is None:
        raise HTTPException(404, "conflict not found")
    if c.decision != "ESCALATED":
        raise HTTPException(409, f"conflict already {c.decision}")
    return c


@router.post("/conflicts/{conflict_id}/approve")
def approve(conflict_id: str, user: User = Depends(dispatcher), db: Session = Depends(get_db)):
    from app.services.reallocation import approve as do_approve
    with STATE.lock:
        c = _get(db, conflict_id)
        try:
            res = do_approve(db, c, user.username)
        except ValueError as exc:
            c.decision, c.resolved_by = "REJECTED", user.username
            db.commit()
            raise HTTPException(409, str(exc))
        db.commit()
    return conflict_dict(res)


@router.post("/conflicts/{conflict_id}/reject")
def reject(conflict_id: str, user: User = Depends(dispatcher), db: Session = Depends(get_db)):
    from app.utils.timeutil import utcnow
    with STATE.lock:
        c = _get(db, conflict_id)
        c.decision, c.resolved_by, c.resolved_at = "REJECTED", user.username, utcnow()
        db.commit()
    return conflict_dict(c)
