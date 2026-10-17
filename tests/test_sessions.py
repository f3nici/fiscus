"""Where a person is signed in, and ending it (D56)."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select, update

from fiscus.models import AuthSession, WebAuthnCredential
from fiscus.routes.settings_routes import device
from fiscus.security.tokens import token_hash
from tests.authenticator import SoftAuthenticator
from tests.conftest import CsrfClient, Env
from tests.test_auth import bootstrap_admin, issue, passkey_login, register_via_link


def signed_in(client: CsrfClient) -> bool:
    return bool(client.get('/').status_code == 200)


def test_revoking_a_passkey_signs_out_the_other_devices(
    env: Env, authenticator: SoftAuthenticator
) -> None:
    phone = bootstrap_admin(env, authenticator)
    laptop_key = SoftAuthenticator(authenticator.origin)
    start = phone.post('/settings/passkeys/options').json()
    cred = laptop_key.create(start['options'])
    phone.post(
        '/settings/passkeys/verify',
        json={'handle': start['handle'], 'credential': cred, 'name': 'Laptop'},
    )
    laptop = env.client()
    assert passkey_login(laptop, laptop_key).status_code == 200
    assert signed_in(laptop)
    with env.db() as db:
        laptop_id = db.execute(
            select(WebAuthnCredential.id).where(WebAuthnCredential.name == 'Laptop')
        ).scalar_one()
    resp = phone.post(f'/settings/passkeys/{laptop_id}/revoke')
    assert resp.status_code == 303
    assert 'Signed%20out%201%20other%20session' in resp.headers['set-cookie']
    assert not signed_in(laptop)
    assert signed_in(phone)


def test_a_recovery_link_signs_out_everywhere(env: Env, authenticator: SoftAuthenticator) -> None:
    bootstrap_admin(env, authenticator)
    sam = env.user('sam')
    lost = env.login(sam)
    assert signed_in(lost)
    found = env.client()
    resp = register_via_link(found, issue(env, sam), SoftAuthenticator(authenticator.origin))
    assert resp.status_code == 200, resp.text
    assert not signed_in(lost)
    assert signed_in(found)


def test_sign_out_everywhere_else(env: Env) -> None:
    alex = env.user('alex')
    here, there = env.login(alex), env.login(alex)
    page = here.get('/settings').text
    assert 'This device' in page and 'Sign out everywhere else' in page
    assert here.post('/settings/sessions/end-others').status_code == 303
    assert not signed_in(there)
    assert signed_in(here)
    assert 'Sign out everywhere else' not in here.get('/settings').text


def test_a_session_expired_is_signed_in_nowhere(env: Env) -> None:
    alex = env.user('alex')
    here, there = env.login(alex), env.login(alex)
    mine = token_hash(here.cookies.get(env.ctx.settings.session_cookie) or '')
    with env.db() as db:
        db.execute(
            update(AuthSession)
            .where(AuthSession.token_hash != mine)
            .values(expires_at=datetime(2026, 9, 1))
        )
        db.commit()
    page = here.get('/settings').text
    assert 'This device' in page and 'Sign out everywhere else' not in page
    assert not signed_in(there)


def test_the_cli_signs_a_person_out(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from fiscus import cli

    alex = env.user('alex', admin=True)
    a, b = env.login(alex), env.login(alex)
    for k, v in {
        'FISCUS_DATA_DIR': str(env.data_dir),
        'FISCUS_BASE_URL': 'https://fiscus.test',
    }.items():
        monkeypatch.setenv(k, v)
    assert cli.main(['sign-out', 'alex']) == 0
    assert not signed_in(a) and not signed_in(b)
    assert cli.main(['sign-out', 'nobody']) == 1


@pytest.mark.parametrize(
    ('agent', 'expected'),
    [
        (
            'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 '
            '(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1',
            'Safari on iPhone',
        ),
        (
            'Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/140.0 Mobile Safari/537.36',
            'Chrome on Android',
        ),
        (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/140.0 Safari/537.36 Edg/140.0',
            'Edge on Windows',
        ),
        ('pytest', 'A browser on a device'),
    ],
)
def test_sessions_are_named_by_device(agent: str, expected: str) -> None:
    assert device(agent) == expected
