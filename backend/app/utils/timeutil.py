from __future__ import annotations

from datetime import datetime, timezone

from app.config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sim_seconds(start: datetime | None, end: datetime | None) -> float | None:
    """Wall-clock interval converted to simulated seconds (wall × SIM_TIME_SCALE)."""
    if start is None or end is None:
        return None
    return (end - start).total_seconds() * get_settings().sim_time_scale
