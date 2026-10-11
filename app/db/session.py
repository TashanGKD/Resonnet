"""Database engine/session management."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_database_url as get_config_database_url

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_database_url() -> str:
    return get_config_database_url()


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _build_engine() -> Engine:
    url = get_database_url()
    # Keep the declared psycopg2 driver when SQLAlchemy's default changes.
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    kwargs: dict = {
        "pool_pre_ping": True,
        "future": True,
    }
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_size"] = int(os.getenv("DB_POOL_SIZE", "10"))
        kwargs["max_overflow"] = int(os.getenv("DB_MAX_OVERFLOW", "20"))
        kwargs["pool_recycle"] = int(os.getenv("DB_POOL_RECYCLE_SECONDS", "1800"))
    return create_engine(url, **kwargs)


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(),
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
            future=True,
        )
    return _SessionLocal


@contextmanager
def session_scope() -> Session:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_db_state() -> None:
    """Dispose cached engine/sessionmaker so tests can swap TOPICDATABASE_URL."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def get_alembic_ini_path() -> Path:
    return _backend_root() / "alembic.ini"
