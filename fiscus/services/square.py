"""Pulling a person's Square payments, refunds, payouts and invoices.

The second outside service Fiscus talks to, after ntfy. Off by default, per
user, and only ever to Square's fixed API host: no address comes from a form,
so the setting cannot point the container anywhere else. Reads only; Fiscus
never writes to Square.

A sync fetches from a week before the last one (or the start date) to now,
writes the rows it has not seen into the chosen account as one import, applies
the person's rules and bill matching, and replaces the stored unpaid invoices.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from fiscus.clock import Clock, utcnow_naive
from fiscus.domain import square
from fiscus.models import (
    Account,
    Category,
    Import,
    SquareConnection,
    SquareInvoice,
    Transaction,
    User,
)
from fiscus.services import bills, ledger
from fiscus.services.access import owned

API = 'https://connect.squareup.com'
VERSION = '2026-09-16'
TIMEOUT = 20.0
MAX_PAGES = 50
OVERLAP = timedelta(days=7)
SYNC_EVERY = timedelta(hours=6)


class SquareError(ValueError):
    """A sync that failed, with a message fit for a person."""


def make_client(token: str, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        transport=transport,
        base_url=API,
        headers={
            'Authorization': f'Bearer {token}',
            'Square-Version': VERSION,
            'Accept': 'application/json',
        },
        timeout=TIMEOUT,
        follow_redirects=False,
    )


# Tests swap this for a client on httpx.MockTransport.
client_factory: Callable[[str], httpx.Client] = make_client


def connection(db: Session, user: User) -> SquareConnection | None:
    return db.get(SquareConnection, user.id)


def default_start(today: date) -> date:
    return today - timedelta(days=90)


def connection_or_new(db: Session, user: User, today: date) -> SquareConnection:
    conn = connection(db, user)
    if conn is None:
        conn = SquareConnection(
            user_id=user.id,
            enabled=False,
            access_token='',
            start_date=default_start(today),
            last_result='',
            last_error='',
        )
        db.add(conn)
    return conn


def _get(client: httpx.Client, path: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        resp = client.get(path, params={k: v for k, v in params.items() if v is not None})
    except httpx.HTTPError as exc:
        raise SquareError(f'Could not reach Square: {exc.__class__.__name__}') from exc
    if resp.status_code == 401:
        raise SquareError('Square refused the access token. Check it in Settings.')
    if resp.status_code == 403:
        raise SquareError(
            'The access token is missing a permission. It needs PAYMENTS_READ, '
            'PAYOUTS_READ, INVOICES_READ and MERCHANT_PROFILE_READ.'
        )
    if resp.status_code == 429:
        raise SquareError('Square asked Fiscus to slow down. The next sync will try again.')
    if resp.status_code >= 300:
        raise SquareError(f'Square answered {resp.status_code}')
    try:
        body = resp.json()
    except ValueError as exc:
        raise SquareError('Square sent something that is not JSON') from exc
    if not isinstance(body, dict):
        raise SquareError('Square sent an unexpected reply')
    return body


def _pages(client: httpx.Client, path: str, key: str, params: dict[str, Any]) -> Iterator[Any]:
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        body = _get(client, path, {**params, 'cursor': cursor})
        yield from body.get(key) or []
        cursor = body.get('cursor')
        if not cursor:
            return
    raise SquareError('Square returned too many pages; set a later start date')


def _stamp(when: datetime) -> str:
    return when.replace(tzinfo=UTC).isoformat().replace('+00:00', 'Z')


@dataclass
class Fetched:
    rows: list[square.Row]
    invoices: list[square.OpenInvoice]


def fetch(client: httpx.Client, begin: datetime, end: datetime, clock: Clock) -> Fetched:
    """Everything between begin and end (naive UTC), as rows and open invoices."""
    window = {'begin_time': _stamp(begin), 'end_time': _stamp(end), 'sort_order': 'ASC'}
    locations = [
        loc['id']
        for loc in _get(client, '/v2/locations', {}).get('locations') or []
        if loc.get('id') and loc.get('status', 'ACTIVE') == 'ACTIVE'
    ]
    invoices: list[dict[str, Any]] = []
    for loc in locations:
        invoices.extend(_pages(client, '/v2/invoices', 'invoices', {'location_id': loc}))
    labels = square.invoice_labels(invoices)
    tz = clock.tz
    rows: list[square.Row] = []
    for p in _pages(client, '/v2/payments', 'payments', window):
        rows.extend(square.payment_rows(p, tz, labels))
    for r in _pages(client, '/v2/refunds', 'refunds', window):
        rows.extend(square.refund_rows(r, tz))
    for loc in locations:
        for p in _pages(client, '/v2/payouts', 'payouts', {**window, 'location_id': loc}):
            rows.extend(square.payout_rows(p, tz))
    return Fetched(rows, square.open_invoices(invoices))


def row_hash(account_id: int, key: str) -> str:
    return hashlib.sha256(f'square|{account_id}|{key}'.encode()).hexdigest()


def sync(db: Session, clock: Clock, user: User) -> str:
    """Run one sync and return a summary. Records the outcome on the connection."""
    conn = connection(db, user)
    if conn is None or not conn.enabled or not conn.access_token:
        raise SquareError('Square is not connected')
    account = db.get(Account, conn.account_id) if conn.account_id else None
    if account is None or account.user_id != user.id:
        raise SquareError('Choose the account Square payments go into')
    now = utcnow_naive(clock)
    start = datetime.combine(conn.start_date, datetime.min.time())
    begin = max(start, conn.synced_until - OVERLAP) if conn.synced_until else start
    try:
        with client_factory(conn.access_token) as client:
            got = fetch(client, begin, now, clock)
    except SquareError as exc:
        conn.last_error = str(exc)[:255]
        conn.last_sync_at = now
        raise
    summary = _store(db, clock, user, conn, account, got)
    conn.synced_until = now
    conn.last_sync_at = now
    conn.last_error = ''
    conn.last_result = summary[:255]
    return summary


def _category(db: Session, user: User, cid: int | None) -> int | None:
    cat = db.get(Category, cid) if cid else None
    return cat.id if cat is not None and cat.user_id == user.id else None


def _store(
    db: Session,
    clock: Clock,
    user: User,
    conn: SquareConnection,
    account: Account,
    got: Fetched,
) -> str:
    rows = [r for r in got.rows if r.date >= conn.start_date]
    hashes = {r.key: row_hash(account.id, r.key) for r in rows}
    existing = set(
        db.execute(
            select(Transaction.dedup_hash).where(
                Transaction.account_id == account.id,
                Transaction.dedup_hash.in_(list(hashes.values())),
            )
        ).scalars()
    )
    fresh = [r for r in rows if hashes[r.key] not in existing]
    cats = {
        'income': _category(db, user, conn.income_category_id),
        'fee': _category(db, user, conn.fee_category_id),
        'refund': _category(db, user, conn.income_category_id),
        'payout': _category(db, user, conn.payout_category_id),
    }
    created: list[Transaction] = []
    now = utcnow_naive(clock)
    if fresh:
        imp = Import(
            user_id=user.id,
            account_id=account.id,
            file_name=f'Square sync {clock.today().isoformat()}',
            stored_path='',
            status='committed',
            format='square',
            row_count=len(rows),
            imported_count=len(fresh),
            duplicate_count=len(rows) - len(fresh),
            created_at=now,
            committed_at=now,
        )
        db.add(imp)
        db.flush()
        for r in fresh:
            t = Transaction(
                user_id=user.id,
                account_id=account.id,
                date=r.date,
                amount_cents=r.amount_cents,
                description=r.description,
                category_id=cats[r.kind],
                import_id=imp.id,
                dedup_hash=hashes[r.key],
                created_at=now,
            )
            db.add(t)
            created.append(t)
        db.flush()
        ledger.apply_rules(db, user, created)
        bills.auto_match(db, clock, user, created)

    clear_invoices(db, user)
    for inv in got.invoices:
        db.add(
            SquareInvoice(
                user_id=user.id,
                square_id=inv.square_id[:120],
                number=inv.number[:120],
                customer=inv.customer[:255],
                due_date=inv.due,
                amount_due_cents=inv.amount_due_cents,
                status=inv.status[:30],
            )
        )
    # The bulk DELETE skips the ORM; the caller's change to the connection
    # bumps data_version, so the forecast sees the new invoices.
    n = len(got.invoices)
    return (
        f'{len(created)} new, {len(rows) - len(created)} already here, '
        f'{n} unpaid invoice{"" if n == 1 else "s"}'
    )


def clear_invoices(db: Session, user: User) -> None:
    db.execute(delete(SquareInvoice).where(SquareInvoice.user_id == user.id))


def invoices(db: Session, user: User) -> list[SquareInvoice]:
    return list(db.execute(owned(SquareInvoice, user).order_by(SquareInvoice.due_date)).scalars())


def due(conn: SquareConnection, now: datetime) -> bool:
    return (
        conn.enabled
        and bool(conn.access_token)
        and (conn.last_sync_at is None or now - conn.last_sync_at >= SYNC_EVERY)
    )
