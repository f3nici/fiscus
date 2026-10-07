"""The service worker, the manifest, ntfy reminders, backups and health checks."""

from __future__ import annotations

import re
import sqlite3
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from fiscus import jobs
from fiscus.models import NotificationLog, UserSettings
from fiscus.routes.pwa import shell_assets
from fiscus.services import backup, ntfy, reminders
from tests.conftest import Env
from tests.test_imports_bills import create_bill, make_account

# ---------------------------------------------------------------- the worker


def test_service_worker_caches_only_the_shell(env: Env) -> None:
    sw = env.client().get('/sw.js')
    assert sw.status_code == 200
    assert 'no-store' in sw.headers['cache-control']
    shell = re.search(r'const SHELL = (\[.*?\]);', sw.text)
    assert shell is not None
    import json

    urls = json.loads(shell.group(1))
    assert urls[0] == '/offline'
    assert all(u == '/offline' or u.startswith('/static/') for u in urls)
    for forbidden in (
        '/accounts',
        '/transactions',
        '/api',
        '/forecast',
        '/calendar',
        '/bills',
        '/',
    ):
        assert forbidden not in urls
    # The fetch handler answers from cache only for the shell.
    assert "url.pathname.startsWith('/static/') || url.pathname === '/offline'" in sw.text
    assert 'skipWaiting' in sw.text and "'SKIP_WAITING'" in sw.text


def test_service_worker_version_changes_with_assets() -> None:
    urls, digest = shell_assets()
    assert '/static/js/app.js' in urls and '/static/vendor/htmx-2.0.11.min.js' in urls
    assert '/static/js/sw.src.js' not in urls
    assert len(digest) == 12


def test_data_responses_are_never_cacheable(env: Env) -> None:
    client = env.login(env.user('alex'))
    acct = make_account(client)
    for path in [
        '/',
        f'/accounts/{acct}',
        '/forecast',
        '/calendar',
        '/transactions/export.csv',
        '/login',
    ]:
        resp = client.get(path)
        assert resp.headers['cache-control'] == 'no-store', path
    redirect = client.post('/accounts', data={'name': 'x', 'type': 'savings'})
    assert redirect.headers['cache-control'] == 'no-store'
    assert client.get('/static/css/app.css').headers['cache-control'] == 'no-cache'


def test_offline_screen_holds_no_data(env: Env) -> None:
    client = env.login(env.user('alex'))
    make_account(client, 'Secret savings', opening='99999')
    page = client.get('/offline').text
    assert 'You are offline' in page
    assert 'Secret' not in page and '99,999' not in page and 'Alex' not in page
    assert 'csrf' not in page


def test_manifest(env: Env) -> None:
    m = env.client().get('/manifest.webmanifest').json()
    assert m['name'] == 'Fiscus' and m['display'] == 'standalone' and m['theme_color']
    assert {i['purpose'] for i in m['icons']} == {'any', 'maskable'}
    assert m['share_target']['action'] == '/share-target'


def test_security_headers(env: Env) -> None:
    resp = env.client().get('/login')
    csp = resp.headers['content-security-policy']
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert 'unsafe' not in csp
    assert resp.headers['x-frame-options'] == 'DENY'
    assert resp.headers['strict-transport-security']


def test_share_target_only_stages(env: Env) -> None:
    client = env.login(env.user('alex'))
    make_account(client)
    from fastapi.testclient import TestClient

    raw = TestClient(env.app, base_url='https://fiscus.test', follow_redirects=False)
    raw.cookies = client.cookies
    resp = raw.post(
        '/share-target', files={'file': ('anz.csv', b'01/10/2026,"-1.00","X"\n', 'text/csv')}
    )
    assert resp.status_code == 303 and resp.headers['location'].startswith('/import/')
    assert 'Preview' in client.get(resp.headers['location']).text
    anon = TestClient(env.app, base_url='https://fiscus.test', follow_redirects=False)
    assert (
        anon.post('/share-target', files={'file': ('a.csv', b'x', 'text/csv')})
        .headers['location']
        .startswith('/login')
    )


def test_no_external_assets_in_pages(env: Env) -> None:
    client = env.login(env.user('alex'))
    for path in ['/', '/login', '/forecast', '/calendar', '/settings']:
        html = client.get(path).text
        for src in re.findall(r'(?:src|href)="([^"]+)"', html):
            assert not src.startswith(('http://', 'https://', '//')), f'{path} loads {src}'


# ---------------------------------------------------------------------- ntfy


@pytest.mark.parametrize(
    'url',
    [
        'http://127.0.0.1',
        'http://localhost:80',
        'http://169.254.169.254',
        'http://[::1]',
        'ftp://ntfy.lan',
        'http://user:pw@10.0.0.5',
        'http://10.0.0.5/topic',
    ],
)
def test_ntfy_target_refused(url: str) -> None:
    with pytest.raises(ntfy.NtfyError):
        ntfy.validate_target(url, 'fiscus', resolve=False)


def test_ntfy_private_lan_allowed() -> None:
    ntfy.validate_target('http://192.168.1.20:8080', 'fiscus-alex', resolve=False)
    ntfy.validate_target('https://10.0.0.5', 'x', resolve=False)
    with pytest.raises(ntfy.NtfyError):
        ntfy.validate_target('https://10.0.0.5', 'bad topic!', resolve=False)


def test_ntfy_off_by_default_and_settings_validate(env: Env) -> None:
    alex = env.user('alex')
    with env.db() as db:
        assert db.get(UserSettings, alex.id).ntfy_enabled is False  # type: ignore[union-attr]
    client = env.login(alex)
    bad = client.post(
        '/settings/ntfy', data={'enabled': 'true', 'url': 'http://127.0.0.1', 'topic': 'x'}
    )
    assert bad.status_code == 400
    ok = client.post(
        '/settings/ntfy',
        data={
            'enabled': 'true',
            'url': 'http://192.168.1.20',
            'topic': 'fiscus',
            'bills_due': 'true',
            'overdue': 'true',
            'low_balance': 'true',
        },
    )
    assert ok.status_code == 303


def test_reminders_sent_once_each(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    alex = env.user('alex')
    client = env.login(alex)
    acct = make_account(client, opening='100')
    create_bill(client, name='Rent', amount='500', next_due='2026-10-03', account_id=str(acct))
    create_bill(client, name='Phone', amount='50', next_due='2026-09-20', account_id=str(acct))
    client.post(
        '/settings/ntfy',
        data={
            'enabled': 'true',
            'url': 'http://192.168.1.20',
            'topic': 'fiscus',
            'hour': '10',
            'bill_days': '3',
            'bills_due': 'true',
            'overdue': 'true',
            'low_balance': 'true',
        },
    )
    sent: list[dict[str, Any]] = []

    def fake_post(_client: httpx.Client, url: str, **kw: Any) -> httpx.Response:
        sent.append({'url': url, **kw})
        return httpx.Response(200)

    monkeypatch.setattr(httpx.Client, 'post', fake_post)
    assert jobs.run_reminders(env.ctx) == 3
    titles = sorted(s['headers']['Title'] for s in sent)
    assert titles == ['Low balance ahead', 'Phone is overdue', 'Rent due on Sat 03 Oct']
    assert all(s['url'] == 'http://192.168.1.20/fiscus' for s in sent)
    # Nothing sensitive: no account numbers or transaction descriptions.
    assert all(b'BPAY' not in s['content'] for s in sent)
    assert jobs.run_reminders(env.ctx) == 0  # already sent
    with env.db() as db:
        assert len(db.execute(select(NotificationLog)).scalars().all()) == 3


def test_reminders_wait_for_the_chosen_hour(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    alex = env.user('alex')
    client = env.login(alex)
    client.post(
        '/settings/ntfy',
        data={'enabled': 'true', 'url': 'http://192.168.1.20', 'topic': 'f', 'hour': '7'},
    )
    monkeypatch.setattr(httpx.Client, 'post', lambda *a, **k: pytest.fail('sent at the wrong hour'))
    with env.db() as db:
        from fiscus.models import User

        assert reminders.send_for_user(db, env.clock, db.get(User, alex.id)) == 0  # type: ignore[arg-type]


# ------------------------------------------------------------------- backups


def test_nightly_backup_and_retention(env: Env) -> None:
    env.user('alex')
    s = env.ctx.settings
    path = backup.nightly(s, env.ctx.engine, env.clock)
    assert path.exists() and path.parent == s.backup_dir
    con = sqlite3.connect(path)
    assert con.execute('SELECT username FROM users').fetchone() == ('alex',)
    con.close()
    old = s.backup_dir / 'fiscus-20200101-000000.db'
    old.write_bytes(b'x')
    import os

    os.utime(old, (0, 0))
    backup.prune(s, env.clock)
    assert not old.exists() and path.exists()


def test_backup_job_runs_at_the_backup_hour(env: Env) -> None:
    from datetime import UTC, datetime

    env.clock.set(datetime(2026, 10, 1, 18, 5, tzinfo=UTC))  # 02:05 in Perth
    state: dict[str, str] = {}
    jobs.tick(env.ctx, state)
    assert len(backup.list_backups(env.ctx.settings)) == 1
    jobs.tick(env.ctx, state)
    assert len(backup.list_backups(env.ctx.settings)) == 1


def test_admin_downloads_backup(env: Env) -> None:
    admin = env.login(env.user('alex', admin=True))
    resp = admin.post('/admin/backups/download')
    assert resp.status_code == 200
    assert resp.content.startswith(b'SQLite format 3')
    assert 'attachment' in resp.headers['content-disposition']
    sam = env.login(env.user('sam'))
    assert sam.post('/admin/backups/download').status_code == 404


def test_restore_round_trip(env: Env, tmp_path: Any) -> None:
    env.user('alex')
    snap = backup.nightly(env.ctx.settings, env.ctx.engine, env.clock)
    env.user('sam')
    env.ctx.engine.dispose()
    backup.restore(env.ctx.settings, snap)
    con = sqlite3.connect(env.ctx.settings.db_path)
    assert [r[0] for r in con.execute('SELECT username FROM users ORDER BY 1')] == ['alex']
    con.close()
    bad = tmp_path / 'bad.db'
    sqlite3.connect(bad).execute('CREATE TABLE x (y)').connection.commit()
    with pytest.raises(RuntimeError):
        backup.restore(env.ctx.settings, bad)


# -------------------------------------------------------------------- health


def test_health_and_ready(env: Env) -> None:
    c = env.client()
    assert c.get('/healthz').json() == {'status': 'ok'}
    ready = c.get('/readyz').json()
    assert ready['status'] == 'ready' and all(ready['checks'].values())


def test_housekeeping_removes_stale_uploads(env: Env) -> None:
    client = env.login(env.user('alex'))
    acct = make_account(client)
    resp = client.post(
        '/import',
        data={'account_id': str(acct)},
        files={'file': ('a.csv', b'01/10/2026,"-1.00","X"\n', 'text/csv')},
    )
    assert resp.status_code == 303
    assert any(env.ctx.settings.upload_dir.iterdir())
    env.clock.set(env.clock.now() + timedelta(days=2))
    jobs.housekeeping(env.ctx)
    assert not any(env.ctx.settings.upload_dir.iterdir())


def test_head_is_answered_like_get(env: Env) -> None:
    c = env.client()
    assert c.head('/healthz').status_code == 200
    resp = c.head('/')
    assert resp.status_code == 303 and resp.headers['location'] == '/login'
    assert resp.content == b''


def test_config_refuses_bad_values(tmp_path: Any) -> None:
    from fiscus.config import ConfigError, load_settings

    base = {'FISCUS_DATA_DIR': str(tmp_path), 'FISCUS_BASE_URL': 'https://fiscus.example.com'}
    with pytest.raises(ConfigError, match='SECRET_KEY'):
        load_settings(base)
    ok = load_settings({**base, 'FISCUS_SECRET_KEY': 's' * 64})
    assert ok.rp_id == 'fiscus.example.com' and ok.origin == 'https://fiscus.example.com'
    assert ok.session_cookie == '__Host-fiscus'
    with pytest.raises(ConfigError, match='RP_ID'):
        load_settings({**base, 'FISCUS_SECRET_KEY': 's' * 64, 'FISCUS_RP_ID': 'evil.com'})
    with pytest.raises(ConfigError, match='TIMEZONE'):
        load_settings({**base, 'FISCUS_SECRET_KEY': 's' * 64, 'FISCUS_TIMEZONE': 'Mars/Olympus'})
