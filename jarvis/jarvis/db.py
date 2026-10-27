"""Database engine, sessions, and versioned schema migrations.

SQLite for single-user local use; the models use only portable SQLAlchemy types
so JARVIS_DATABASE_URL can point at PostgreSQL later.
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

SCHEMA_VERSION = 2


class Base(DeclarativeBase):
    pass


def utcnow() -> dt.datetime:
    """Naive UTC timestamp (stored consistently across SQLite and Postgres)."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Database:
    def __init__(self, url: str):
        if url.startswith("sqlite:///") and url != "sqlite:///:memory:":
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        kwargs = {}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
            if url == "sqlite:///:memory:":
                from sqlalchemy.pool import StaticPool
                kwargs["poolclass"] = StaticPool
        self.url = url
        self.engine: Engine = create_engine(url, future=True, **kwargs)
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", _sqlite_pragmas)
        self._sessionmaker = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def migrate(self) -> int:
        """Apply pending migrations; returns the resulting schema version."""
        from . import models  # noqa: F401  (register tables)

        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"))
            row = conn.execute(text("SELECT MAX(version) FROM schema_version")).scalar()
            current = row or 0
        for version in range(current + 1, SCHEMA_VERSION + 1):
            MIGRATIONS[version](self.engine)
            with self.engine.begin() as conn:
                conn.execute(text("INSERT INTO schema_version (version) VALUES (:v)"), {"v": version})
        return max(current, SCHEMA_VERSION)

    def schema_version(self) -> int:
        with self.engine.connect() as conn:
            try:
                return conn.execute(text("SELECT MAX(version) FROM schema_version")).scalar() or 0
            except Exception:
                return 0


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()


def _migration_1(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def _add_columns(engine: Engine, table: str, columns: dict[str, str]) -> None:
    """Portable ADD COLUMN that skips columns create_all already made on fresh databases."""
    existing = {c["name"] for c in inspect(engine).get_columns(table)}
    with engine.begin() as conn:
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def _migration_2(engine: Engine) -> None:
    """Phase 3: Gmail threading fields and send errors."""
    _add_columns(engine, "inbound_messages", {
        "subject": "VARCHAR(500) NOT NULL DEFAULT ''",
        "thread_id": "VARCHAR(200) NOT NULL DEFAULT ''",
        "rfc_message_id": "VARCHAR(500) NOT NULL DEFAULT ''",
    })
    _add_columns(engine, "drafts", {"send_error": "TEXT NOT NULL DEFAULT ''",
                                   "edited_by_owner": "BOOLEAN NOT NULL DEFAULT FALSE"})
    with engine.begin() as conn:
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_inbound_messages_thread_id ON inbound_messages (thread_id)"))


MIGRATIONS = {1: _migration_1, 2: _migration_2}
