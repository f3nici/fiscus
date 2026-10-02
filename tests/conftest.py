"""Test harness.

Each test gets its own data directory holding a copy of one database that was
migrated once per run, so every test exercises the real migrations without
paying for them each time. The clock is fixed: "today" is Thursday 1 October
2026 in Perth unless a test moves it.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from fiscus.app import create_app
from fiscus.clock import Clock
from fiscus.config import Settings, load_settings
from fiscus.db import run_migrations
from fiscus.models import User
from fiscus.services import auth as auth_service
from fiscus.services import square as square_service
from fiscus.services.users import create_user
from fiscus.web import AppContext
from tests.authenticator import SoftAuthenticator

ORIGIN = 'https://fiscus.test'
NOW = datetime(2026, 10, 1, 2, 0, tzinfo=UTC)  # 10:00 in Perth


def settings_for(data_dir: Path, **extra: str) -> Settings:
    env = {
        'FISCUS_DATA_DIR': str(data_dir),
        'FISCUS_BASE_URL': ORIGIN,
        'FISCUS_ORIGIN': ORIGIN,
        'FISCUS_RP_ID': 'fiscus.test',
        'FISCUS_SECRET_KEY': 'x' * 64,
        'FISCUS_TIMEZONE': 'Australia/Perth',
        'FISCUS_RUN_JOBS': 'false',
        **extra,
    }
    return load_settings(env)


@pytest.fixture(autouse=True)
def no_square(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never reach the real Square; test_square.py swaps in a fake."""

    def refuse(token: str) -> Any:
        raise AssertionError('a test tried to call the real Square API')

    monkeypatch.setattr(square_service, 'client_factory', refuse)


@pytest.fixture(scope='session')
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    folder = tmp_path_factory.mktemp('template')
    run_migrations(f'sqlite:///{folder / "fiscus.db"}')
    return folder / 'fiscus.db'


class CsrfClient(TestClient):
    """A TestClient that sends the CSRF cookie back as a header, as the pages do."""

    def request(self, method: str, url: Any, **kwargs: Any) -> Any:
        if method.upper() in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            token = self.cookies.get('__Host-fiscus-csrf')
            if token is None:
                super().request('GET', '/healthz')
                token = self.cookies.get('__Host-fiscus-csrf')
            headers = dict(kwargs.pop('headers', None) or {})
            headers.setdefault('X-CSRF-Token', token or '')
            kwargs['headers'] = headers
        return super().request(method, url, **kwargs)


@dataclass
class Env:
    app: FastAPI
    ctx: AppContext
    clock: Clock
    data_dir: Path

    def db(self) -> Session:
        return self.ctx.sessions()

    def client(self) -> CsrfClient:
        return CsrfClient(self.app, base_url=ORIGIN, follow_redirects=False)

    def user(self, username: str, *, admin: bool = False) -> User:
        with self.db() as db:
            u = create_user(db, self.clock, username, username.title(), is_admin=admin)
            db.commit()
            db.refresh(u)
            return u

    def login(self, user: User) -> CsrfClient:
        """A client signed in as `user`, without the ceremony (tested separately)."""
        c = self.client()
        with self.db() as db:
            fresh = db.get(User, user.id)
            assert fresh is not None
            token = auth_service.start_session(
                db, self.ctx.settings, self.clock, fresh, 'test', '127.0.0.1', 'pytest'
            )
            db.commit()
        c.cookies.set(self.ctx.settings.session_cookie, token, domain='fiscus.test')
        return c


@pytest.fixture
def env(tmp_path: Path, migrated_template: Path) -> Iterator[Env]:
    data = tmp_path / 'data'
    data.mkdir()
    shutil.copy(migrated_template, data / 'fiscus.db')
    settings = settings_for(data)
    clock = Clock(settings.tz, NOW)
    app = create_app(settings, clock=clock, migrate=False)
    yield Env(app, app.state.ctx, clock, data)
    app.state.ctx.engine.dispose()


@pytest.fixture
def authenticator() -> SoftAuthenticator:
    return SoftAuthenticator(ORIGIN)
