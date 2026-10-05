"""Database engine/session management and a tiny ordered SQL-migration runner."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import PROJECT_DIR, get_settings

log = logging.getLogger("app.database")
MIGRATIONS_DIR = PROJECT_DIR / "database" / "migrations"


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def init_engine(url: str | None = None) -> Engine:
    global _engine, _SessionLocal
    url = url or get_settings().database_url
    if _engine is not None:
        _engine.dispose()
    _engine = create_engine(url, pool_size=10, max_overflow=20, pool_pre_ping=True, future=True)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def SessionLocal() -> Session:
    if _SessionLocal is None:
        init_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def run_migrations(engine: Engine | None = None, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every *.sql file in migrations_dir (sorted) that is not yet recorded in schema_migrations."""
    engine = engine or get_engine()
    applied_now: list[str] = []
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        ))
        done = {r[0] for r in conn.execute(text("SELECT version FROM schema_migrations"))}
    for path in sorted(migrations_dir.glob("*.sql")):
        if path.name in done:
            continue
        sql = path.read_text(encoding="utf-8")
        with engine.begin() as conn:
            conn.exec_driver_sql(sql)
            conn.execute(text("INSERT INTO schema_migrations(version) VALUES (:v)"), {"v": path.name})
        applied_now.append(path.name)
        log.info("migration applied", extra={"event": "MIGRATION_APPLIED", "fields": {"version": path.name}})
    return applied_now


def check_database() -> dict:
    try:
        with get_engine().connect() as conn:
            version = conn.execute(text("SELECT postgis_lib_version()")).scalar()
        return {"ok": True, "postgis": version}
    except Exception as exc:  # pragma: no cover - reported by /api/health
        return {"ok": False, "error": str(exc)[:300]}
