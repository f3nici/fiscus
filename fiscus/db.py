"""Engine, sessions and migrations."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

MIGRATIONS_DIR = Path(__file__).parent / 'migrations'


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    engine = create_engine(url, connect_args={'check_same_thread': False, 'timeout': 30})

    @event.listens_for(engine, 'connect')
    def _pragmas(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute('PRAGMA foreign_keys=ON')
        cur.execute('PRAGMA journal_mode=WAL')
        cur.execute('PRAGMA synchronous=NORMAL')
        cur.execute('PRAGMA busy_timeout=30000')
        cur.close()

    return engine


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=True)


def alembic_config(url: str) -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option('script_location', str(MIGRATIONS_DIR))
    cfg.set_main_option('sqlalchemy.url', url)
    return cfg


def run_migrations(url: str) -> None:
    command.upgrade(alembic_config(url), 'head')


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    db = factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
