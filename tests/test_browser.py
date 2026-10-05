"""End to end in a real Chromium: passkeys through the browser's WebAuthn,
the service worker's cache, the offline screen, and a clean console.

Uses Chrome's virtual authenticator (DevTools protocol), so the page runs the
same navigator.credentials code a phone does. Skipped when no Chromium is
available; CI installs one.
"""

from __future__ import annotations

import os
import shutil
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from fiscus.app import create_app
from fiscus.services import auth as auth_service
from fiscus.services.budget_import import template_csv
from tests.conftest import settings_for
from tests.test_paydays import simple_slip
from tests.test_payslip_pdf import payslip_pdf
from tests.test_pdf_import import statement

pytestmark = pytest.mark.browser

playwright_api = pytest.importorskip('playwright.sync_api')


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return int(s.getsockname()[1])


@pytest.fixture
def server(tmp_path: Path, migrated_template: Path) -> Iterator[tuple[str, Any]]:
    port = free_port()
    base = f'http://localhost:{port}'
    data = tmp_path / 'data'
    data.mkdir()
    shutil.copy(migrated_template, data / 'fiscus.db')
    settings = settings_for(
        data, FISCUS_ORIGIN=base, FISCUS_BASE_URL=base, FISCUS_RP_ID='localhost'
    )
    app = create_app(settings, migrate=False)
    config = uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning')
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield base, app.state.ctx
    srv.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def browser() -> Iterator[Any]:
    exe = os.environ.get('CHROMIUM_PATH')
    if exe is None:
        default = Path('/opt/pw-browsers/chromium-1194/chrome-linux/chrome')
        exe = str(default) if default.exists() else None
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        except Exception as exc:  # pragma: no cover - environment without a browser
            pytest.skip(f'no Chromium: {exc}')
        yield b
        b.close()


def test_passkey_signup_browse_and_offline(server: tuple[str, Any], browser: Any) -> None:
    base, ctx = server
    with ctx.sessions() as db:
        link = auth_service.ensure_bootstrap(db, ctx.settings, ctx.clock)
        db.commit()
    assert link is not None

    context = browser.new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    errors: list[str] = []
    page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
    page.on('pageerror', lambda e: errors.append(str(e)))

    cdp = context.new_cdp_session(page)
    cdp.send('WebAuthn.enable')
    cdp.send(
        'WebAuthn.addVirtualAuthenticator',
        {
            'options': {
                'protocol': 'ctap2',
                'transport': 'internal',
                'hasResidentKey': True,
                'hasUserVerification': True,
                'isUserVerified': True,
                'automaticPresenceSimulation': True,
            }
        },
    )

    # Register the first admin through the real page.
    page.goto(f'{base}/setup/{link.token}')
    page.fill('#username', 'alex')
    page.fill('#display_name', 'Alex')
    page.click('[data-passkey-go]')
    page.wait_for_url(f'{base}/')
    assert "G'day, Alex" in page.content()

    # Sign out and back in, usernameless. The login page starts a conditional
    # (autofill) request on load, and the virtual authenticator answers it
    # without a click, which is the whole flow working end to end.
    page.goto(f'{base}/more')
    page.click('main form[action="/auth/logout"] button')
    page.wait_for_url(f'{base}/')
    assert "G'day, Alex" in page.content()

    # Add an account and walk the main screens.
    page.goto(f'{base}/accounts/new')
    page.fill('#name', 'Everyday')
    page.fill('#balance', '2500')
    page.click('main button[type=submit]')
    page.wait_for_url(f'{base}/accounts/*')
    for path in [
        '/calendar',
        '/forecast',
        '/budget',
        '/bills/new',
        '/settings',
        '/tax',
        '/bills/import',
        '/square',
    ]:
        page.goto(f'{base}{path}')
        page.wait_for_load_state('networkidle')
    # Import is a tab under Transactions; a PDF statement goes through it.
    page.goto(f'{base}/transactions')
    page.click('nav.tabs a[href="/import"]')
    page.wait_for_url(f'{base}/import')
    page.set_input_files(
        '#file',
        files=[{'name': 'statement.pdf', 'mimeType': 'application/pdf', 'buffer': statement()}],
    )
    page.click('#import-form button[type=submit]')
    page.wait_for_url(f'{base}/import/*')
    assert 'PDF statement' in page.content()
    assert 'EFTPOS COFFEE CO PERTH WA' in page.content()
    page.click('form[action$="/commit"] button')
    page.wait_for_url(f'{base}/accounts/*')
    assert 'Imported 4 transactions' in page.content()
    page.goto(f'{base}/pay')
    page.check('input[name=varies]')
    page.fill('#ps-net', '450')
    page.click('form[action="/pay/schedules"] button[type=submit]')
    page.wait_for_url(f'{base}/pay')
    assert 'varies' in page.content()
    page.wait_for_selector('ul.list .title:has-text("Salary")')
    # A payslip PDF fills in the payslip form, then it is added.
    page.set_input_files(
        '#slip-file',
        files=[{'name': 'slip.pdf', 'mimeType': 'application/pdf', 'buffer': payslip_pdf()}],
    )
    page.click('#slip-read button[type=submit]')
    page.wait_for_selector('#slip-read-note')
    assert page.input_value('#slip-gross') == '803.85'
    assert page.input_value('#slip-net') == '618.85'
    page.click('#slip-form button[type=submit]')
    page.wait_for_url(f'{base}/pay')
    page.wait_for_selector('[data-flash]:has-text("Payslip saved")', state='attached')
    # Today's payday asks for its payslip; several PDFs go through a review.
    page.wait_for_selector('#put-in')
    page.set_input_files(
        '#slip-file',
        files=[
            {
                'name': f'{day}.pdf',
                'mimeType': 'application/pdf',
                'buffer': simple_slip(f'{day}/09/2026', '650.00', '150.00', '500.00'),
            }
            for day in ('10', '17')
        ],
    )
    page.click('#slip-read button[type=submit]')
    page.wait_for_selector('#bulk-form')
    page.uncheck('#bulk-form input[name=include] >> nth=1')
    page.click('#bulk-form button[type=submit]')
    page.wait_for_url(f'{base}/pay')
    page.wait_for_selector('[data-flash]:has-text("1 payslip saved")', state='attached')
    page.click('#put-in form[action$="/no-pay"] button')
    page.wait_for_selector('td:has-text("no pay")')

    # The weekly routine: a budget line, money taken off it, balances typed in.
    page.goto(f'{base}/budget')
    page.fill('#new_category', 'Fuel')
    page.fill('#add_amount', '200')
    page.click('form:has(#add_amount) button[type=submit]')
    page.wait_for_selector('#spend_amount')
    page.fill('#spend_amount', '65.50')
    page.fill('#spend_note', 'Ampol')
    page.click('form[action="/budget/spend"] button[type=submit]')
    page.wait_for_selector('text=Ampol')
    assert '$134.50' in page.content()
    # Something paid once a year is saved up a month at a time.
    page.fill('#new_category', 'Car rego')
    page.fill('#add_amount', '900')
    page.select_option('#add_cycle', '12')
    page.click('form:has(#add_amount) button[type=submit]')
    page.wait_for_selector('text=Saving up $900.00 every year')
    # Importing: changing how often a line is spent reworks its monthly amount.
    page.set_input_files(
        'input[name=file]',
        files=[{'name': 'b.csv', 'mimeType': 'text/csv', 'buffer': template_csv().encode()}],
    )
    page.click('#import button[type=submit]')
    page.wait_for_url(f'{base}/budget/import/*')
    rates = 'tr:has(input[value="Council rates"])'
    assert page.input_value(f'{rates} input[name^=monthly_]') == '246.67'
    page.select_option(f'{rates} select', '1')
    assert page.input_value(f'{rates} input[name^=monthly_]') == '2960.00'
    page.select_option(f'{rates} select', '3')
    assert page.input_value(f'{rates} input[name^=monthly_]') == '986.67'
    assert 'Saving up $2,960.01 every 3 months' in page.inner_text(rates)
    page.select_option(f'{rates} select', '12')
    assert page.input_value(f'{rates} input[name^=monthly_]') == '246.67'
    # Weekly back to monthly returns the file's own monthly figure.
    groceries = 'tr:has(input[value="Groceries"])'
    page.select_option(f'{groceries} select', '12')
    assert page.input_value(f'{groceries} input[name^=monthly_]') == '20.00'
    page.select_option(f'{groceries} select', '1')
    assert page.input_value(f'{groceries} input[name^=monthly_]') == '1040.00'
    page.check('input[name=mode][value=clear]')
    page.click('form[action$="/apply"] button[type=submit]')
    page.wait_for_url(f'{base}/budget')
    page.wait_for_selector(
        '[data-flash]:has-text("Old budget cleared (2 lines)")', state='attached'
    )
    # Car rego came back from the file at its own amount, not the old $900.
    table = page.inner_text('main table')
    assert 'Saving up $1,109.04 every year' in table and '$900.00' not in table
    page.goto(f'{base}/accounts')
    page.fill('input[name^=balance_]', '2400')
    page.click('form[action="/accounts/balances"] button[type=submit]')
    page.wait_for_selector('tfoot th:has-text("$2,400.00")')

    # Narrow screen: the calendar opens on the agenda.
    page.goto(f'{base}/calendar')
    page.wait_for_function(
        "document.querySelector('#cal') && document.querySelector('#cal').dataset.view === 'agenda'"
    )
    # The forecast chart drew.
    page.goto(f'{base}/forecast')
    page.wait_for_selector('.uplot')
    # And after a boosted (htmx) navigation, which drops <script> tags.
    page.goto(f'{base}/')
    page.click('main a[href="/forecast"]')
    page.wait_for_selector('.uplot')

    # The service worker is active and has cached the shell and nothing else.
    page.goto(f'{base}/')
    page.evaluate('navigator.serviceWorker.ready.then(() => true)')
    page.wait_for_function('navigator.serviceWorker.controller !== null', timeout=10000)
    cached = page.evaluate("""async () => {
        const out = [];
        for (const name of await caches.keys()) {
            const c = await caches.open(name);
            for (const req of await c.keys()) out.push(new URL(req.url).pathname);
        }
        return out;
    }""")
    assert cached, 'the service worker cached nothing'
    assert all(p == '/offline' or p.startswith('/static/') for p in cached), cached

    # Browse a few data pages, then check again: still nothing but the shell.
    for path in ['/accounts', '/calendar', '/forecast']:
        page.goto(f'{base}{path}')
    cached_after = page.evaluate("""async () => {
        const out = [];
        for (const name of await caches.keys()) {
            const c = await caches.open(name);
            for (const req of await c.keys()) out.push(new URL(req.url).pathname);
        }
        return out;
    }""")
    assert sorted(cached_after) == sorted(cached)

    # Offline: the offline screen, never yesterday's figures.
    context.set_offline(True)
    page.goto(f'{base}/accounts')
    assert page.locator('#offline-screen').is_visible()
    assert 'Everyday' not in page.content() and '2,500' not in page.content()
    context.set_offline(False)

    benign = ('favicon', 'Failed to load resource: net::ERR_INTERNET_DISCONNECTED')
    real = [e for e in errors if not any(b in e for b in benign)]
    assert real == [], real
    context.close()
