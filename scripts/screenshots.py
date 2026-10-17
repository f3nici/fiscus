"""Seed a demo household and capture the README screenshots with Chromium.

    python scripts/screenshots.py

Writes docs/screenshots/*.png. Everything in them is made up. The data goes in
through the app's own forms, on a fixed clock, so the figures are what a real
person would see and the shots come out the same each run. Run by hand when a
screen changes.
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from sqlalchemy import select

from fiscus.app import create_app
from fiscus.clock import Clock
from fiscus.config import load_settings
from fiscus.models import BudgetGroup, Category, PaySchedule
from fiscus.services import auth
from fiscus.services.users import create_user

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs' / 'screenshots'
TODAY = datetime(2026, 10, 16, 2, 0, tzinfo=UTC)  # Friday 16 October, 10:00 in Perth

DESKTOP = {'width': 1280, 'height': 800}
PHONE = {'width': 390, 'height': 844}
# (file, path, viewport, element scrolled to the top first)
SHOTS = [
    ('dashboard.png', '/', DESKTOP, ''),
    ('budget.png', '/budget', DESKTOP, '#lines'),
    ('forecast.png', '/forecast', DESKTOP, ''),
    ('calendar.png', '/calendar', PHONE, ''),
    ('bills.png', '/bills', PHONE, ''),
    ('loan.png', '/loans', PHONE, ''),
]
# Balances as typed in from the banking app, so the offset has a history.
BALANCES = [
    (datetime(2026, 7, 1, 2, 0, tzinfo=UTC), ('2950', '27800', '6500')),
    (datetime(2026, 10, 1, 2, 0, tzinfo=UTC), ('3105', '30400', '8700')),
]
TODAYS = ('3412.55', '31250', '8940')


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return int(s.getsockname()[1])


class Seeder:
    """Posts to the app's forms in process, with the CSRF token they ask for."""

    def __init__(self, client: TestClient, csrf_cookie: str) -> None:
        self.client = client
        client.get('/healthz')
        self.token = client.cookies.get(csrf_cookie) or ''

    def post(self, path: str, **data: Any) -> str:
        resp = self.client.post(path, data=data, headers={'X-CSRF-Token': self.token})
        if resp.status_code not in (200, 303):
            raise RuntimeError(f'{path}: {resp.status_code} {resp.text[:200]}')
        return str(resp.headers.get('location', ''))

    def made(self, path: str, **data: Any) -> int:
        return int(self.post(path, **data).split('#')[0].rsplit('/', 1)[1])


def seed(app: Any, clock: Clock, base: str) -> str:
    ctx = app.state.ctx
    s = ctx.settings
    with ctx.sessions() as db:
        user = create_user(db, clock, 'alex', 'Alex', is_admin=True)
        token = auth.start_session(db, s, clock, user, 'demo', '127.0.0.1', 'screenshots')
        db.commit()
        uid = user.id
    client = TestClient(app, base_url=base, follow_redirects=False)
    client.cookies.set(s.session_cookie, token)
    f = Seeder(client, s.csrf_cookie)

    def cat(name: str) -> int:
        with ctx.sessions() as db:
            return int(
                db.execute(
                    select(Category.id).where(Category.user_id == uid, Category.name == name)
                ).scalar_one()
            )

    everyday = f.made('/accounts', name='Everyday', type='transaction', opening_balance='0')
    offset = f.made('/accounts', name='Offset', type='offset', opening_balance='0')
    savings = f.made('/accounts', name='Savings', type='savings', opening_balance='0')

    def balances(figures: tuple[str, str, str]) -> None:
        ids = (everyday, offset, savings)
        f.post(
            '/accounts/balances', **{f'balance_{i}': v for i, v in zip(ids, figures, strict=True)}
        )

    for when, figures in BALANCES:
        clock.set(when)
        balances(figures)
    clock.set(TODAY)

    f.post(
        '/pay/schedules',
        name='Acme Logistics',
        frequency='fortnightly',
        anchor_date='2026-07-02',
        net='3260',
        gross='4200',
        tax_withheld='940',
        account_id=everyday,
    )
    with ctx.sessions() as db:
        sid = db.execute(select(PaySchedule.id)).scalar_one()
    payday = date(2026, 7, 2)
    while payday < TODAY.date():
        f.post(
            '/payslips',
            date=payday.isoformat(),
            source='Acme Logistics',
            gross='4200',
            tax_withheld='940',
            net='3260',
            account_id='0',
            payday=f'{sid}:{payday.isoformat()}',
        )
        payday += timedelta(days=14)

    bills = [
        ('Phone', '55', 'monthly', '2026-10-05', 'Utilities'),
        ('Internet', '89', 'monthly', '2026-10-21', 'Utilities'),
        ('Electricity', '286', 'bimonthly', '2026-10-28', 'Utilities'),
        ('Water', '312', 'quarterly', '2026-12-03', 'Utilities'),
        ('Council rates', '548', 'quarterly', '2026-11-15', 'Housing'),
        ('Health insurance', '312', 'monthly', '2026-10-28', 'Health'),
        ('Car insurance', '1240', 'yearly', '2027-02-14', 'Insurance'),
        ('Streaming', '25', 'monthly', '2026-10-24', 'Subscriptions'),
    ]
    made: dict[str, int] = {}
    for name, amount, freq, due, category in bills:
        made[name] = f.made(
            '/bills',
            name=name,
            amount=amount,
            frequency=freq,
            next_due=due,
            account_id=everyday,
            category_id=cat(category),
        )
    clock.set(datetime(2026, 10, 5, 2, 0, tzinfo=UTC))
    f.post(f'/bills/{made["Phone"]}/occurrence/2026-10-05/paid')
    clock.set(TODAY)

    f.post('/budget/groups', name='Bills')
    f.post('/budget/groups', name='Living')
    with ctx.sessions() as db:
        groups = {g.name: g.id for g in db.execute(select(BudgetGroup)).scalars()}
    lines = [
        ('Utilities', '460', '1', 'Bills'),
        ('Housing', '190', '1', 'Bills'),
        ('Health', '312', '1', 'Bills'),
        ('Insurance', '1240', '12', 'Bills'),
        ('Subscriptions', '40', '1', 'Bills'),
        ('Groceries', '900', '1', 'Living'),
        ('Transport', '280', '1', 'Living'),
        ('Dining out', '220', '1', 'Living'),
        ('Entertainment', '150', '1', 'Living'),
    ]
    for name, amount, cycle, group in lines:
        f.post(
            '/budget',
            category_id=cat(name),
            amount=amount,
            cycle_months=cycle,
            group_id=groups[group],
            saved='830' if cycle == '12' else '',
        )
    spends = [
        ('Groceries', '142.30', 'Coles', '2026-10-03'),
        ('Groceries', '96.80', 'Woolworths', '2026-10-10'),
        ('Groceries', '63.15', 'Aldi', '2026-10-14'),
        ('Transport', '72.40', 'Fuel', '2026-10-12'),
        ('Dining out', '58.00', 'Dinner out', '2026-10-09'),
        ('Entertainment', '34.00', 'Cinema', '2026-10-11'),
    ]
    for name, amount, note, on in spends:
        f.post('/budget/spend', category_id=cat(name), amount=amount, note=note, on=on)

    loan = f.made(
        '/loans',
        name='Home loan',
        principal='620000',
        start_date='2022-03-15',
        term_years='30',
        initial_rate='6.09',
        balance='548300',
        balance_date='2026-10-01',
        repayment_account_id=everyday,
    )
    f.post(f'/loans/{loan}/offsets', account_id=offset)
    f.post(
        '/goals',
        name='Japan trip',
        account_id=savings,
        target='6000',
        target_date='2027-04-01',
        saved='2400',
        keep_aside='true',
    )
    f.post(
        '/goals',
        name='Emergency fund',
        account_id=offset,
        target='15000',
        saved='12000',
        keep_aside='true',
    )
    f.post('/goals', name='Tax', account_id=savings, saved='260', keep_aside='true', is_tax='true')
    f.post('/tax/deductions', amount='1850')
    # Typed in last, as from the banking app today: it holds everything before.
    balances(TODAYS)
    return token


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    port = free_port()
    base = f'http://localhost:{port}'
    data = Path(tempfile.mkdtemp(prefix='fiscus-demo-'))
    settings = load_settings(
        {
            'FISCUS_DATA_DIR': str(data),
            'FISCUS_BASE_URL': base,
            'FISCUS_TIMEZONE': 'Australia/Perth',
            'FISCUS_RUN_JOBS': 'false',
        }
    )
    clock = Clock(settings.tz, TODAY)
    app = create_app(settings, clock=clock)
    token = seed(app, clock, base)

    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    exe = os.environ.get('CHROMIUM_PATH')
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        for name, path, viewport, top in SHOTS:
            phone = viewport is PHONE
            context = browser.new_context(
                viewport=viewport,
                device_scale_factor=2 if phone else 1,
                is_mobile=phone,
                has_touch=phone,
            )
            context.add_cookies([{'name': settings.session_cookie, 'value': token, 'url': base}])
            page = context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(base + path)
            page.wait_for_load_state('networkidle')
            if top:
                page.locator(top).evaluate('el => window.scrollTo(0, el.offsetTop - 16)')
            page.wait_for_timeout(400)  # the forecast chart draws after load
            page.screenshot(path=str(OUT / name))
            print(f'wrote docs/screenshots/{name}')
            context.close()
        browser.close()
    server.should_exit = True
    if errors:
        raise SystemExit(f'page errors: {errors}')


if __name__ == '__main__':
    main()
