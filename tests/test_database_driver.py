"""A PostgreSQL engine must load the driver shipped in the executor image."""
from app.db import session


def test_plain_postgresql_url_uses_installed_psycopg2(monkeypatch):
    monkeypatch.setattr(session, "get_database_url", lambda: "postgresql://test:test@localhost/driver_check")
    engine = session._build_engine()
    try:
        assert engine.dialect.driver == "psycopg2"
        assert engine.dialect.dbapi.__name__ == "psycopg2"
    finally:
        engine.dispose()
