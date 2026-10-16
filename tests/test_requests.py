"""How a request meets the database: the write lock, and the commit before the answer (D59)."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fiscus.clock import utcnow_naive
from fiscus.models import Category, User, WebAuthnCredential
from fiscus.services.users import passkey_count
from tests.conftest import ORIGIN, CsrfClient, Env


def quiet(env: Env, user: User) -> CsrfClient:
    """A signed-in client that gets the error page a browser would, not the exception."""
    c = CsrfClient(env.app, base_url=ORIGIN, follow_redirects=False, raise_server_exceptions=False)
    c.cookies = env.login(user).cookies
    return c


def test_the_commit_runs_before_the_answer(env: Env) -> None:
    a = env.login(env.user('alex'))
    events: list[str] = []
    real = env.ctx.sessions

    def spied() -> Session:
        s = real()
        commit = s.commit

        def logged() -> None:
            events.append('commit')
            commit()

        s.commit = logged  # type: ignore[method-assign]
        return s

    env.ctx.sessions = spied  # type: ignore[assignment]
    inner = env.app.build_middleware_stack()

    async def app(scope: Any, receive: Any, send: Any) -> None:
        async def spy(message: Any) -> None:
            if message['type'] == 'http.response.start':
                events.append('answer')
            await send(message)

        await inner(scope, receive, spy)

    env.app.middleware_stack = app
    a.get('/healthz')  # the CSRF cookie, before anything is recorded
    events.clear()
    assert a.post('/categories', data={'name': 'Pets'}).status_code == 303
    assert events.index('commit') < events.index('answer'), events


def test_a_failed_commit_is_an_error_not_a_success(env: Env) -> None:
    a = quiet(env, env.user('alex'))
    real = env.ctx.sessions

    def broken() -> Session:
        s = real()

        def commit() -> None:
            raise OSError('disk full')

        s.commit = commit  # type: ignore[method-assign]
        return s

    env.ctx.sessions = broken  # type: ignore[assignment]
    resp = a.post('/categories', data={'name': 'Pets'})
    env.ctx.sessions = real
    assert resp.status_code == 500
    assert 'Nothing was saved' in resp.text
    assert 'Category%20saved' not in resp.headers.get('set-cookie', '')
    with env.db() as db:
        assert db.execute(select(Category).where(Category.name == 'Pets')).first() is None


def test_two_revokes_at_once_leave_a_passkey(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each request holds the write lock from its start, so the second revoke
    counts after the first has gone, and refuses to take the last passkey."""
    alex = env.user('alex')
    with env.db() as db:
        for n in (1, 2):
            db.add(
                WebAuthnCredential(
                    user_id=alex.id,
                    credential_id=bytes([n]) * 16,
                    public_key=b'k',
                    name=f'Key {n}',
                    created_at=utcnow_naive(env.clock),
                )
            )
        db.commit()
        ids = [
            c.id
            for c in db.execute(
                select(WebAuthnCredential).where(WebAuthnCredential.user_id == alex.id)
            ).scalars()
        ]

    def slow_count(db: Session, user: User) -> int:
        n = passkey_count(db, user)
        time.sleep(0.3)  # both would have counted two, without the lock
        return n

    monkeypatch.setattr('fiscus.routes.settings_routes.passkey_count', slow_count)
    clients = [quiet(env, alex), quiet(env, alex)]
    codes: list[int] = []

    def revoke(c: CsrfClient, cred: int) -> None:
        codes.append(c.post(f'/settings/passkeys/{cred}/revoke').status_code)

    threads = [
        threading.Thread(target=revoke, args=(c, cred))
        for c, cred in zip(clients, ids, strict=True)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert 500 not in codes, codes
    with env.db() as db:
        left = db.scalar(
            select(func.count())
            .select_from(WebAuthnCredential)
            .where(WebAuthnCredential.user_id == alex.id)
        )
    assert left == 1


# ---------------------------------------------------------- cookies, redirects


def test_logout_deletes_the_cookie_as_it_was_set(env: Env) -> None:
    a = env.login(env.user('alex'))
    resp = a.post('/auth/logout')
    cookie = next(
        v for k, v in resp.headers.multi_items() if k == 'set-cookie' and v.startswith('__Host-')
    )
    # Without Secure a browser ignores a __Host- cookie, deletion included.
    assert 'Secure' in cookie and 'Max-Age=0' in cookie and 'Path=/' in cookie


def test_sign_in_comes_back_to_the_whole_address(env: Env) -> None:
    c = env.client()
    resp = c.get('/budget?month=2026-09&view=all')
    assert resp.headers['location'] == '/login?next=/budget%3Fmonth%3D2026-09%26view%3Dall'
    page = c.get(resp.headers['location']).text
    assert 'data-next="/budget?month=2026-09&amp;view=all"' in page


# ------------------------------------------------------------------- dates


def test_dates_far_off_are_refused_or_ignored(env: Env) -> None:
    from tests.test_bills import create_bill, make_account

    a = env.login(env.user('alex'))
    bill = create_bill(a, account_id=str(make_account(a)))
    assert a.get('/calendar?date=9999-12-15').status_code == 200
    assert a.get('/calendar?date=0001-01-01&view=month').status_code == 200
    assert a.get('/budget?month=9999-12').status_code == 200
    assert a.get(f'/bills/{bill}/occurrence/9999-12-15').status_code == 404
    resp = a.post(
        '/bills',
        data={'name': 'Old', 'amount': '10', 'frequency': 'monthly', 'next_due': '0001-01-31'},
    )
    assert resp.status_code == 400 and 'from 1900 to 2199' in resp.text


# --------------------------------------------------------------- body size


def test_a_body_too_large_is_refused_before_it_is_read(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr('fiscus.web.MAX_BODY', 1000)
    a = env.login(env.user('alex'))
    resp = a.post('/categories', data={'name': 'x' * 2000})
    assert resp.status_code == 413
    assert 'to send at once' in resp.text
    assert resp.headers['cache-control'] == 'no-store'
    assert "script-src 'self'" in resp.headers['content-security-policy']
    assert a.post('/categories', data={'name': 'Pets'}).status_code == 303


def test_a_streamed_body_too_large_is_refused(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('fiscus.web.MAX_BODY', 1000)
    a = env.login(env.user('alex'))
    token = a.cookies.get('__Host-fiscus-csrf') or a.get('/healthz').cookies['__Host-fiscus-csrf']

    def chunks() -> Any:
        for _ in range(10):
            yield b'name=' + b'x' * 300 + b'&'

    resp = a.post(
        '/categories',
        content=chunks(),
        headers={'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRF-Token': token},
    )
    assert resp.status_code == 413


# ------------------------------------------------------------ forecast cache


def test_turning_a_chart_line_off_keeps_the_forecast_cache(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)

    def version() -> int:
        with env.db() as db:
            return int(db.scalar(select(User.data_version).where(User.id == alex.id)) or 0)

    before = version()
    assert a.post('/forecast/lines', data={'key': 'trend', 'shown': 'false'}).status_code == 204
    a.post('/settings/ntfy', data={'hour': '9'})
    assert version() == before
    a.post('/settings/forecast', data={'low_balance': '100', 'forecast_months': '12'})
    assert version() > before
