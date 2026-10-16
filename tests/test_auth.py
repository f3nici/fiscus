"""Passkey registration and sign-in, setup links, recovery, sessions and CSRF."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DatabaseError

from fiscus.models import AuditLog, User, WebAuthnCredential
from fiscus.services import auth as auth_service
from tests.authenticator import SoftAuthenticator, b64u
from tests.conftest import CsrfClient, Env


def bootstrap_link(env: Env) -> str:
    with env.db() as db:
        link = auth_service.ensure_bootstrap(db, env.ctx.settings, env.clock)
        db.commit()
    assert link is not None
    return link.token


def register_via_link(
    client: CsrfClient, token: str, authn: SoftAuthenticator, extra: dict[str, Any] | None = None
) -> Any:
    start = client.post(f'/setup/{token}/options', json=extra or {})
    assert start.status_code == 200, start.text
    cred = authn.create(start.json()['options'])
    return client.post(
        f'/setup/{token}/verify',
        json={'handle': start.json()['handle'], 'credential': cred, 'name': 'Phone'},
    )


def passkey_login(
    client: CsrfClient, authn: SoftAuthenticator, username: str | None = None, **get_kwargs: Any
) -> Any:
    start = client.post('/auth/passkey/options', json={'username': username} if username else {})
    assert start.status_code == 200, start.text
    cred = authn.get(start.json()['options'], **get_kwargs)
    return client.post(
        '/auth/passkey/verify', json={'handle': start.json()['handle'], 'credential': cred}
    )


def issue(env: Env, user: User, purpose: str = 'recovery') -> str:
    """A setup link for `user`, as the recovery-link command issues one."""
    with env.db() as db:
        fresh = db.get(User, user.id)
        assert fresh is not None
        link = auth_service.issue_link(db, env.ctx.settings, env.clock, purpose, fresh, None)
        db.commit()
    return link.token


def bootstrap_admin(env: Env, authn: SoftAuthenticator) -> CsrfClient:
    client = env.client()
    token = bootstrap_link(env)
    resp = register_via_link(client, token, authn, {'username': 'alex', 'display_name': 'Alex'})
    assert resp.status_code == 200, resp.text
    return client


# ------------------------------------------------------------------ bootstrap


def test_bootstrap_registers_first_admin_and_signs_in(
    env: Env, authenticator: SoftAuthenticator
) -> None:
    client = bootstrap_admin(env, authenticator)
    assert client.get('/').status_code == 200
    with env.db() as db:
        user = db.execute(select(User)).scalar_one()
        assert user.is_admin and user.username == 'alex'
        assert len(user.credentials) == 1
        assert user.credentials[0].name == 'Phone'
        actions = {a.action for a in db.execute(select(AuditLog)).scalars()}
    assert {'user.bootstrap', 'passkey.registered', 'login'} <= actions


def test_bootstrap_link_is_single_use(env: Env, authenticator: SoftAuthenticator) -> None:
    client = env.client()
    token = bootstrap_link(env)
    assert register_via_link(client, token, authenticator, {'username': 'alex'}).status_code == 200
    again = env.client().post(f'/setup/{token}/options', json={'username': 'sam'})
    assert again.status_code == 410
    assert env.client().get(f'/setup/{token}').status_code == 410


def test_no_bootstrap_once_users_exist(env: Env) -> None:
    env.user('alex', admin=True)
    with env.db() as db:
        assert auth_service.ensure_bootstrap(db, env.ctx.settings, env.clock) is None


def test_bootstrap_link_expires(env: Env, authenticator: SoftAuthenticator) -> None:
    token = bootstrap_link(env)
    env.clock.set(env.clock.now() + timedelta(hours=25))
    assert env.client().get(f'/setup/{token}').status_code == 410


# -------------------------------------------------------------------- sign in


def test_usernameless_login(env: Env, authenticator: SoftAuthenticator) -> None:
    bootstrap_admin(env, authenticator)
    fresh = env.client()
    resp = passkey_login(fresh, authenticator)
    assert resp.status_code == 200, resp.text
    assert resp.json()['ok'] is True
    assert fresh.get('/').status_code == 200
    with env.db() as db:
        cred = db.execute(select(WebAuthnCredential)).scalar_one()
        assert cred.last_used_at is not None
        assert cred.sign_count == 1


def test_username_first_login_offers_only_that_users_credentials(
    env: Env, authenticator: SoftAuthenticator
) -> None:
    bootstrap_admin(env, authenticator)
    start = env.client().post('/auth/passkey/options', json={'username': 'alex'})
    allow = start.json()['options']['allowCredentials']
    assert [a['id'] for a in allow] == [b64u(authenticator.credentials[0].credential_id)]
    assert passkey_login(env.client(), authenticator, 'alex').status_code == 200


def test_unknown_username_does_not_reveal_itself(
    env: Env, authenticator: SoftAuthenticator
) -> None:
    bootstrap_admin(env, authenticator)
    resp = env.client().post('/auth/passkey/options', json={'username': 'nobody'})
    assert resp.status_code == 200
    assert len(resp.json()['options']['allowCredentials']) == 1


def test_tampered_signature_is_refused(env: Env, authenticator: SoftAuthenticator) -> None:
    bootstrap_admin(env, authenticator)
    fresh = env.client()
    resp = passkey_login(fresh, authenticator, tamper=True)
    assert resp.status_code == 400
    assert fresh.get('/').status_code == 303


def test_wrong_origin_is_refused(env: Env, authenticator: SoftAuthenticator) -> None:
    bootstrap_admin(env, authenticator)
    resp = passkey_login(env.client(), authenticator, origin='https://evil.test')
    assert resp.status_code == 400


def test_challenge_cannot_be_replayed(env: Env, authenticator: SoftAuthenticator) -> None:
    bootstrap_admin(env, authenticator)
    client = env.client()
    start = client.post('/auth/passkey/options', json={}).json()
    cred = authenticator.get(start['options'])
    first = client.post(
        '/auth/passkey/verify', json={'handle': start['handle'], 'credential': cred}
    )
    second = env.client().post(
        '/auth/passkey/verify', json={'handle': start['handle'], 'credential': cred}
    )
    assert first.status_code == 200
    assert second.status_code == 400


def test_expired_challenge_is_refused(env: Env, authenticator: SoftAuthenticator) -> None:
    bootstrap_admin(env, authenticator)
    client = env.client()
    start = client.post('/auth/passkey/options', json={}).json()
    env.clock.set(env.clock.now() + timedelta(minutes=6))
    cred = authenticator.get(start['options'])
    resp = client.post('/auth/passkey/verify', json={'handle': start['handle'], 'credential': cred})
    assert resp.status_code == 400


def test_disabled_user_cannot_sign_in(env: Env, authenticator: SoftAuthenticator) -> None:
    bootstrap_admin(env, authenticator)
    sam = env.user('sam')
    sam_client = env.login(sam)
    assert sam_client.get('/').status_code == 200
    with env.db() as db:
        stored = db.get(User, sam.id)
        assert stored is not None
        stored.is_active = False
        db.commit()
    # The session no longer works.
    assert sam_client.get('/').status_code == 303


# ------------------------------------------------------- onboarding, recovery


def test_a_setup_link_works_once(env: Env, authenticator: SoftAuthenticator) -> None:
    bootstrap_admin(env, authenticator)
    sam = env.user('sam')
    token = issue(env, sam, 'onboard')
    sam_device = SoftAuthenticator(authenticator.origin)
    client = env.client()
    assert client.get(f'/setup/{token}').status_code == 200
    assert register_via_link(client, token, sam_device).status_code == 200
    assert client.get('/').status_code == 200
    # Used once, gone.
    assert env.client().get(f'/setup/{token}').status_code == 410
    # Sam signs in with Sam's own passkey and lands as Sam.
    c2 = env.client()
    assert passkey_login(c2, sam_device).status_code == 200
    assert 'Sam' in c2.get('/').text


def test_a_new_link_revokes_the_previous_one(env: Env, authenticator: SoftAuthenticator) -> None:
    bootstrap_admin(env, authenticator)
    sam = env.user('sam')
    first = issue(env, sam)
    second = issue(env, sam)
    assert env.client().get(f'/setup/{first}').status_code == 410
    assert env.client().get(f'/setup/{second}').status_code == 200


def test_recovery_after_losing_every_passkey(env: Env, authenticator: SoftAuthenticator) -> None:
    bootstrap_admin(env, authenticator)
    sam = env.user('sam')
    lost_phone = SoftAuthenticator(authenticator.origin)
    register_via_link(env.client(), issue(env, sam, 'onboard'), lost_phone)
    rtoken = issue(env, sam)
    new_phone = SoftAuthenticator(authenticator.origin)
    client = env.client()
    resp = register_via_link(client, rtoken, new_phone)
    assert resp.json()['redirect'] == '/settings?recovered=1'
    with env.db() as db:
        creds = (
            db.execute(select(WebAuthnCredential).where(WebAuthnCredential.user_id == sam.id))
            .scalars()
            .all()
        )
        assert len(creds) == 2
        old = next(c for c in creds if c.credential_id == lost_phone.credentials[0].credential_id)
    # Now revoke the lost phone.
    assert client.post(f'/settings/passkeys/{old.id}/revoke').status_code == 303
    assert passkey_login(env.client(), lost_phone).status_code == 400
    assert passkey_login(env.client(), new_phone).status_code == 200


def test_cli_break_glass_recovery_link(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from fiscus import cli

    env.user('alex', admin=True)
    for k, v in {
        'FISCUS_DATA_DIR': str(env.data_dir),
        'FISCUS_ORIGIN': 'https://fiscus.test',
        'FISCUS_BASE_URL': 'https://fiscus.test',
        'FISCUS_SECRET_KEY': 'x' * 64,
    }.items():
        monkeypatch.setenv(k, v)
    assert cli.main(['recovery-link', 'alex']) == 0
    out = capsys.readouterr().out
    assert 'https://fiscus.test/setup/' in out


# ------------------------------------------------------------- managing keys


def test_add_second_passkey_rename_and_revoke(env: Env, authenticator: SoftAuthenticator) -> None:
    client = bootstrap_admin(env, authenticator)
    laptop = SoftAuthenticator(authenticator.origin)
    start = client.post('/settings/passkeys/options').json()
    # The phone is excluded from re-registering.
    assert len(start['options']['excludeCredentials']) == 1
    cred = laptop.create(start['options'])
    done = client.post(
        '/settings/passkeys/verify',
        json={'handle': start['handle'], 'credential': cred, 'name': 'Laptop'},
    )
    assert done.status_code == 200, done.text
    with env.db() as db:
        creds = db.execute(select(WebAuthnCredential)).scalars().all()
        assert sorted(c.name for c in creds) == ['Laptop', 'Phone']
        laptop_id = next(c.id for c in creds if c.name == 'Laptop')
        phone_id = next(c.id for c in creds if c.name == 'Phone')
    assert (
        client.post(
            f'/settings/passkeys/{laptop_id}/rename', data={'name': 'Work laptop'}
        ).status_code
        == 303
    )
    assert client.post(f'/settings/passkeys/{phone_id}/revoke').status_code == 303
    page = client.get('/settings').text
    assert 'Work laptop' in page and 'Phone' not in page


def test_cannot_revoke_last_passkey_without_another_login(
    env: Env, authenticator: SoftAuthenticator
) -> None:
    client = bootstrap_admin(env, authenticator)
    with env.db() as db:
        cred_id = db.execute(select(WebAuthnCredential.id)).scalar_one()
    resp = client.post(f'/settings/passkeys/{cred_id}/revoke')
    assert resp.status_code == 400
    assert 'last passkey' in resp.text


def test_cannot_touch_another_users_passkey(env: Env, authenticator: SoftAuthenticator) -> None:
    bootstrap_admin(env, authenticator)
    with env.db() as db:
        alex_cred = db.execute(select(WebAuthnCredential.id)).scalar_one()
    sam = env.login(env.user('sam'))
    assert sam.post(f'/settings/passkeys/{alex_cred}/revoke').status_code == 404
    assert sam.post(f'/settings/passkeys/{alex_cred}/rename', data={'name': 'x'}).status_code == 404


# ------------------------------------------------------------------ password


def test_there_is_no_password_sign_in(env: Env) -> None:
    """Passkeys are the only way in; a lost one is a recovery link (D52). The
    password columns are gone from users too (0026)."""
    sam = env.user('sam')
    with env.db() as db:
        cols = {c['name'] for c in inspect(db.get_bind()).get_columns('users')}
    assert not {'password_enabled', 'password_hash'} & cols
    client = env.client()
    login = client.get('/login').text
    assert 'type="password"' not in login and 'recovery-link' in login
    resp = client.post('/auth/password', data={'username': 'sam', 'password': 'x' * 12})
    assert resp.status_code in (404, 405)
    signed_in = env.login(sam)
    assert 'Password sign-in' not in signed_in.get('/settings').text
    for path in ('/settings/password', '/settings/password/clear'):
        assert signed_in.post(path, data={'password': 'x' * 12}).status_code in (404, 405)


# ------------------------------------------------------- csrf, limits, audit


def test_csrf_token_is_required(env: Env, authenticator: SoftAuthenticator) -> None:
    client = bootstrap_admin(env, authenticator)
    from fastapi.testclient import TestClient

    raw = TestClient(env.app, base_url='https://fiscus.test', follow_redirects=False)
    raw.cookies = client.cookies
    resp = raw.post('/accounts', data={'name': 'x', 'type': 'transaction'})
    assert resp.status_code == 403


def test_cross_origin_post_is_refused(env: Env, authenticator: SoftAuthenticator) -> None:
    client = bootstrap_admin(env, authenticator)
    resp = client.post(
        '/accounts',
        data={'name': 'x', 'type': 'transaction'},
        headers={'Origin': 'https://evil.test'},
    )
    assert resp.status_code == 403


def test_login_is_rate_limited_by_username(env: Env) -> None:
    client = env.client()
    codes = [
        client.post('/auth/passkey/options', json={'username': 'x'}).status_code for _ in range(12)
    ]
    assert codes[0] == 200 and codes[-1] == 429


def test_passkey_options_are_rate_limited(env: Env) -> None:
    client = env.client()
    codes = [client.post('/auth/passkey/options', json={}).status_code for _ in range(32)]
    assert codes[0] == 200 and codes[-1] == 429


def test_audit_log_is_append_only(env: Env, authenticator: SoftAuthenticator) -> None:
    bootstrap_admin(env, authenticator)
    with env.db() as db, pytest.raises(DatabaseError):
        db.execute(text("UPDATE audit_log SET action = 'x'"))
    with env.db() as db, pytest.raises(DatabaseError):
        db.execute(text('DELETE FROM audit_log'))


def test_logout_ends_the_session(env: Env, authenticator: SoftAuthenticator) -> None:
    client = bootstrap_admin(env, authenticator)
    assert client.post('/auth/logout').status_code == 303
    assert client.get('/').status_code == 303


def test_session_idle_expiry(env: Env) -> None:
    client = env.login(env.user('sam'))
    env.clock.set(env.clock.now() + timedelta(days=31))
    assert client.get('/').status_code == 303


def test_session_cookie_is_secure_httponly(env: Env, authenticator: SoftAuthenticator) -> None:
    client = env.client()
    token = bootstrap_link(env)
    start = client.post(f'/setup/{token}/options', json={'username': 'alex'}).json()
    cred = authenticator.create(start['options'])
    resp = client.post(
        f'/setup/{token}/verify', json={'handle': start['handle'], 'credential': cred}
    )
    cookie = next(
        v
        for k, v in resp.headers.multi_items()
        if k == 'set-cookie' and v.startswith('__Host-fiscus=')
    )
    assert (
        'Secure' in cookie
        and 'HttpOnly' in cookie
        and 'SameSite=lax' in cookie
        and 'Path=/' in cookie
    )


def test_passkeys_refused_without_https(tmp_path: Any, migrated_template: Any) -> None:
    import shutil

    from fiscus.app import create_app
    from tests.conftest import settings_for

    data = tmp_path / 'd'
    data.mkdir()
    shutil.copy(migrated_template, data / 'fiscus.db')
    settings = settings_for(
        data,
        FISCUS_ORIGIN='http://fiscus.lan',
        FISCUS_BASE_URL='http://fiscus.lan',
        FISCUS_RP_ID='fiscus.lan',
    )
    assert not settings.passkeys_allowed
    app = create_app(settings, migrate=False)
    client = CsrfClient(app, base_url='http://fiscus.lan')
    client.cookies.set('fiscus_csrf', 'c' * 43)
    resp = client.post('/auth/passkey/options', json={}, headers={'X-CSRF-Token': 'c' * 43})
    assert resp.status_code == 503


def test_localhost_is_allowed_for_development(tmp_path: Any) -> None:
    from tests.conftest import settings_for

    s = settings_for(
        tmp_path,
        FISCUS_ORIGIN='http://localhost:8000',
        FISCUS_BASE_URL='http://localhost:8000',
        FISCUS_RP_ID='localhost',
    )
    assert s.passkeys_allowed and not s.cookie_secure


def test_setup_link_cannot_be_used_for_a_disabled_user(
    env: Env, authenticator: SoftAuthenticator
) -> None:
    bootstrap_admin(env, authenticator)
    sam = env.user('sam')
    token = issue(env, sam, 'onboard')
    with env.db() as db:
        stored = db.get(User, sam.id)
        assert stored is not None
        stored.is_active = False
        db.commit()
    assert env.client().get(f'/setup/{token}').status_code == 410


def test_the_admin_pages_are_gone(env: Env) -> None:
    """One person keeps Fiscus: no users page, invites or audit view (D52)."""
    admin = env.login(env.user('alex', admin=True))
    for path in ('/admin/users', '/admin/audit', '/admin/backups'):
        assert admin.get(path).status_code == 404
    assert admin.post('/admin/users', data={'username': 'eve'}).status_code == 404
    assert '/admin' not in admin.get('/more').text and '/admin' not in admin.get('/').text
