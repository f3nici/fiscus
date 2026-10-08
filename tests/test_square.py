"""Square sync against a fake Square API (httpx.MockTransport)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from fiscus import jobs
from fiscus.domain import square as dom
from fiscus.models import AuditLog, Category, Import, SquareConnection, Transaction
from fiscus.services import square as svc
from tests.conftest import CsrfClient, Env
from tests.test_bills import make_account

PERTH = ZoneInfo('Australia/Perth')
TOKEN = 'EAAAtest-token'


def aud(cents: int) -> dict[str, Any]:
    return {'amount': cents, 'currency': 'AUD'}


PAYMENTS: list[dict[str, Any]] = [
    {
        'id': 'pay-invoice-1',
        'status': 'COMPLETED',
        'created_at': '2026-09-20T23:30:00Z',  # 21 Sep in Perth
        'total_money': aud(55000),
        'processing_fee': [{'amount_money': aud(990), 'type': 'INITIAL'}],
        'order_id': 'order-1',
        'receipt_number': 'R1',
    },
    {
        'id': 'pay-tap-2',
        'status': 'COMPLETED',
        'created_at': '2026-09-25T02:00:00Z',
        'total_money': aud(8000),
        'processing_fee': [{'amount_money': aud(128)}],
        'note': 'Lesson',
        'receipt_number': 'R2',
    },
    {'id': 'pay-failed', 'status': 'FAILED', 'created_at': '2026-09-25T02:00:00Z'},
    {
        'id': 'pay-usd',
        'status': 'COMPLETED',
        'created_at': '2026-09-26T02:00:00Z',
        'total_money': {'amount': 100, 'currency': 'USD'},
    },
]
REFUNDS: list[dict[str, Any]] = [
    {
        'id': 'refund-1',
        'status': 'COMPLETED',
        'created_at': '2026-09-27T02:00:00Z',
        'amount_money': aud(2000),
        'processing_fee': [{'amount_money': aud(-36)}],
    }
]
PAYOUTS: list[dict[str, Any]] = [
    {'id': 'po-1', 'status': 'PAID', 'amount_money': aud(40000), 'arrival_date': '2026-09-23'},
    {'id': 'po-2', 'status': 'SENT', 'amount_money': aud(100), 'arrival_date': '2026-09-30'},
]
INVOICES: list[dict[str, Any]] = [
    {
        'id': 'inv-1',
        'invoice_number': '000001',
        'order_id': 'order-1',
        'status': 'PAID',
        'primary_recipient': {'given_name': 'Jo', 'family_name': 'Citizen'},
        'payment_requests': [
            {
                'due_date': '2026-09-21',
                'computed_amount_money': aud(55000),
                'total_completed_amount_money': aud(55000),
            }
        ],
    },
    {
        'id': 'inv-2',
        'invoice_number': '000002',
        'status': 'UNPAID',
        'primary_recipient': {'company_name': 'Acme Pty Ltd'},
        'payment_requests': [{'due_date': '2026-10-15', 'computed_amount_money': aud(120000)}],
    },
]


class FakeSquare:
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.url.host == 'connect.squareup.com'
        assert request.headers['Authorization'] == f'Bearer {TOKEN}'
        assert request.headers['Square-Version'] == svc.VERSION
        if self.status != 200:
            return httpx.Response(self.status, json={'errors': []})
        path, q = request.url.path, request.url.params
        if path == '/v2/locations':
            return httpx.Response(200, json={'locations': [{'id': 'L1', 'status': 'ACTIVE'}]})
        if path == '/v2/invoices':
            assert q['location_id'] == 'L1'
            return httpx.Response(200, json={'invoices': INVOICES})
        if path == '/v2/payments':
            # Two pages, to prove the cursor is followed.
            if q.get('cursor') == 'page2':
                return httpx.Response(200, json={'payments': PAYMENTS[2:]})
            return httpx.Response(200, json={'payments': PAYMENTS[:2], 'cursor': 'page2'})
        if path == '/v2/refunds':
            return httpx.Response(200, json={'refunds': REFUNDS})
        if path == '/v2/payouts':
            return httpx.Response(200, json={'payouts': PAYOUTS})
        return httpx.Response(404)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeSquare]:
    sq = FakeSquare()

    def factory(token: str) -> httpx.Client:
        return svc.make_client(token, httpx.MockTransport(sq.handler))

    monkeypatch.setattr(svc, 'client_factory', factory)
    yield sq


def _category(env: Env, user_id: int, name: str, **flags: bool) -> int:
    with env.db() as db:
        cat = Category(user_id=user_id, name=name, **flags)
        db.add(cat)
        db.commit()
        return cat.id


def _connect(client: CsrfClient, acct: int, **extra: str) -> None:
    resp = client.post(
        '/square',
        data={
            'enabled': 'true',
            'token': TOKEN,
            'account_id': str(acct),
            'start_date': '2026-09-01',
            **extra,
        },
    )
    assert resp.status_code == 303, resp.text


def test_payment_rows_and_fees() -> None:
    labels = dom.invoice_labels(INVOICES)
    rows = dom.payment_rows(PAYMENTS[0], PERTH, labels)
    assert rows == [
        dom.Row(
            'payment:pay-invoice-1',
            'income',
            date(2026, 9, 21),
            55000,
            'Square payment R1 · Invoice 000001 · Jo Citizen',
        ),
        dom.Row('fee:pay-invoice-1', 'fee', date(2026, 9, 21), -990, 'Square fee R1'),
    ]
    assert dom.payment_rows(PAYMENTS[2], PERTH, labels) == []
    assert dom.payment_rows(PAYMENTS[3], PERTH, labels) == []  # not AUD
    refund = dom.refund_rows(REFUNDS[0], PERTH)
    assert [(r.kind, r.amount_cents) for r in refund] == [('refund', -2000), ('fee', 36)]
    assert [r.amount_cents for p in PAYOUTS for r in dom.payout_rows(p, PERTH)] == [-40000]


def test_open_invoices_only_what_is_owed() -> None:
    assert dom.open_invoices(INVOICES) == [
        dom.OpenInvoice('inv-2:0', '000002', 'Acme Pty Ltd', date(2026, 10, 15), 120000, 'UNPAID')
    ]


def test_sync_end_to_end(env: Env, fake: FakeSquare) -> None:
    alice = env.user('alice')
    a = env.login(alice)
    acct = make_account(a, name='Square balance', opening='0')
    income = _category(env, alice.id, 'Lessons', is_income=True, is_business=True)
    fees = _category(env, alice.id, 'Square fees', is_business=True)
    _connect(a, acct, income_category_id=str(income), fee_category_id=str(fees))

    resp = a.post('/square/sync')
    assert resp.status_code == 303
    assert '7 new, 0 already here, 1 unpaid invoice' in a.get('/square').text
    with env.db() as db:
        txns = db.execute(select(Transaction).order_by(Transaction.date)).scalars().all()
        got = [(t.date.isoformat(), t.amount_cents, t.category_id) for t in txns]
        imp = db.execute(select(Import)).scalar_one()
        assert imp.format == 'square' and imp.imported_count == 7
        actions = set(db.execute(select(AuditLog.action)).scalars())
    assert sorted(got, key=str) == sorted(
        [
            ('2026-09-21', 55000, income),
            ('2026-09-21', -990, fees),
            ('2026-09-23', -40000, None),
            ('2026-09-25', 8000, income),
            ('2026-09-25', -128, fees),
            ('2026-09-27', -2000, income),
            ('2026-09-27', 36, fees),
        ],
        key=str,
    )
    assert 'square.token_set' in actions

    # The page lists the unpaid invoice; the calendar shows it on its due date.
    page = a.get('/square').text
    assert 'Acme Pty Ltd' in page and '$1,200.00' in page and TOKEN not in page
    assert 'Invoice 000002' in a.get('/calendar?view=agenda&date=2026-10-15').text

    # Syncing again adds nothing.
    a.post('/square/sync')
    with env.db() as db:
        assert len(db.execute(select(Transaction.id)).all()) == 7
        assert len(db.execute(select(Import.id)).all()) == 1


def test_sync_errors_are_recorded(env: Env, fake: FakeSquare) -> None:
    a = env.login(env.user('alice'))
    _connect(a, make_account(a))
    fake.status = 401
    a.post('/square/sync')
    assert 'Square refused the access token' in a.get('/square').text
    with env.db() as db:
        conn = db.execute(select(SquareConnection)).scalar_one()
        assert conn.last_error and conn.synced_until is None


def test_job_syncs_every_six_hours(env: Env, fake: FakeSquare) -> None:
    a = env.login(env.user('alice'))
    _connect(a, make_account(a))
    assert jobs.run_square(env.ctx) == 1
    assert jobs.run_square(env.ctx) == 0
    env.clock.set(env.clock.now() + timedelta(hours=6))
    assert jobs.run_square(env.ctx) == 1
    # The second sync started a week before the first one ended.
    begins = [c.url.params['begin_time'] for c in fake.calls if c.url.path == '/v2/payments']
    assert begins[0] == '2026-09-01T00:00:00Z'
    assert begins[-1] == '2026-09-24T02:00:00Z'


def test_disconnect_forgets_the_token(env: Env, fake: FakeSquare) -> None:
    a = env.login(env.user('alice'))
    _connect(a, make_account(a))
    a.post('/square/sync')
    a.post('/square/disconnect')
    with env.db() as db:
        conn = db.execute(select(SquareConnection)).scalar_one()
        assert conn.access_token == '' and not conn.enabled
        assert len(db.execute(select(Transaction.id)).all()) == 7
    assert 'Acme Pty Ltd' not in a.get('/square').text
    assert jobs.run_square(env.ctx) == 0


def test_enabling_needs_a_token_and_account(env: Env, fake: FakeSquare) -> None:
    a = env.login(env.user('alice'))
    acct = make_account(a)
    for data in ({'account_id': str(acct)}, {'token': TOKEN}):
        a.post('/square', data={'enabled': 'true', 'start_date': '2026-09-01', **data})
    with env.db() as db:
        conn = db.execute(select(SquareConnection)).scalar_one_or_none()
        assert conn is None or not conn.enabled
