"""System event bus.

emit() records the event in system_events inside the caller's transaction and queues the WebSocket
broadcast; the broadcast happens only after the transaction commits, so clients never see state that
was rolled back.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models import SystemEvent
from app.services.state import STATE

log = logging.getLogger("app.events")
# high-frequency events are broadcast but not written to system_events (locations live in ambulance_locations)
NOT_PERSISTED = {"AMBULANCE_LOCATION_UPDATED"}


def emit(db: Session, event_type: str, data: dict, incident_id=None, ambulance_id: str | None = None,
         persist: bool = True) -> None:
    if persist and event_type not in NOT_PERSISTED:
        db.add(SystemEvent(event_type=event_type,
                           incident_id=uuid.UUID(str(incident_id)) if incident_id else None,
                           ambulance_id=ambulance_id, payload=_jsonable(data)))
    db.info.setdefault("pending_ws", []).append((event_type, data))
    db.info.setdefault("pending_after_commit", [])


def after_commit(db: Session, fn) -> None:
    """Run fn() once the session's current transaction commits (e.g. MQTT publishes)."""
    db.info.setdefault("pending_after_commit", []).append(fn)


def broadcast_now(event_type: str, data: dict) -> None:
    STATE.ws.broadcast_threadsafe(event_type, _jsonable(data))


def _jsonable(data):
    if isinstance(data, dict):
        return {k: _jsonable(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [_jsonable(v) for v in data]
    if isinstance(data, uuid.UUID):
        return str(data)
    if isinstance(data, float) and (data != data or data in (float("inf"), float("-inf"))):
        return None
    if hasattr(data, "isoformat"):
        return data.isoformat()
    if type(data).__module__ == "numpy":
        return data.item()
    return data


@event.listens_for(Session, "after_commit")
def _flush_pending(session: Session) -> None:
    pending = session.info.pop("pending_ws", [])
    callbacks = session.info.pop("pending_after_commit", [])
    for event_type, data in pending:
        STATE.ws.broadcast_threadsafe(event_type, _jsonable(data))
    for fn in callbacks:
        try:
            fn()
        except Exception:
            log.exception("after-commit callback failed")


@event.listens_for(Session, "after_rollback")
def _drop_pending(session: Session) -> None:
    session.info.pop("pending_ws", None)
    session.info.pop("pending_after_commit", None)
