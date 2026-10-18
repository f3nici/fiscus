"""budget is bills and an estimate on top

A line's budget was what the person typed, its bills paid out of it (D29), so
a line could be budgeted below its bills and run out. Its budget is now its
bills plus an estimate on top (D64), and only the estimate is stored: what each
line had less its recurring bills, never below nothing. A line below its bills
rises to them. A saved-up line first carries forward what it held at the end
of the last budget month, worked out as before, so the change counts from
this budget month. A spending category holding a bill and no line gets one,
saving up if a bill in it is paid less often than monthly (D63).

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-18 23:00:00
"""

import os
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from alembic import op

from fiscus.domain import recurrence
from fiscus.money import round_div

revision: str = '0031'
down_revision: str | None = '0030'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PER_YEAR = {
    'weekly': 52,
    'fortnightly': 26,
    'monthly': 12,
    'bimonthly': 6,
    'quarterly': 4,
    'yearly': 1,
}
LONGER = ('bimonthly', 'quarterly', 'yearly')


def _day(value: object) -> date | None:
    return date.fromisoformat(str(value)[:10]) if value is not None else None


def _bills(bind: sa.Connection, uid: int, start: date) -> tuple[dict[int, int], set[int], set[int]]:
    """Recurring bills per category over a year, the categories holding any
    bill, and those holding one paid less often than monthly."""
    yearly: dict[int, int] = {}
    held: set[int] = set()
    longer: set[int] = set()
    rows = bind.execute(
        sa.text(
            'SELECT category_id, amount_cents, frequency, next_due, end_date FROM bills'
            ' WHERE user_id = :u AND active AND category_id IS NOT NULL'
        ),
        {'u': uid},
    ).all()
    for cid, amount, freq, due, end in rows:
        ended = _day(end)
        if ended is not None and ended < start:
            continue
        if freq == 'one_off':
            due_day = _day(due)
            if due_day is not None and due_day >= start:
                held.add(cid)
            continue
        yearly[cid] = yearly.get(cid, 0) + amount * PER_YEAR[freq]
        held.add(cid)
        if freq in LONGER:
            longer.add(cid)
    return yearly, held, longer


def upgrade() -> None:
    bind = op.get_bind()
    tz = ZoneInfo(os.environ.get('FISCUS_TIMEZONE') or 'Australia/Perth')
    today = datetime.now(tz).date()
    start_days: dict[int, int] = dict(
        bind.execute(sa.text('SELECT user_id, budget_start_day FROM user_settings')).all()
    )
    users = [u for (u,) in bind.execute(sa.text('SELECT id FROM users')).all()]
    for uid in users:
        first = start_days.get(uid) or 1
        start = recurrence.month_period(today, first)[0]
        before = start - timedelta(days=1)
        yearly, held, longer = _bills(bind, uid, start)
        lines = bind.execute(
            sa.text(
                'SELECT b.id, b.category_id, b.monthly_cents, b.total_cents, b.cycle_months,'
                ' b.saved_since, b.saved_opening_cents, c.is_income FROM budgets b'
                ' JOIN categories c ON c.id = b.category_id WHERE b.user_id = :u'
            ),
            {'u': uid},
        ).all()
        has_line = set()
        for bid, cid, monthly, total, cycle, since_raw, opening, is_income in lines:
            has_line.add(cid)
            if is_income:
                continue
            total = total if total is not None else monthly * cycle
            since = _day(since_raw)
            if since is not None and since < start:
                # What it held at the end of last budget month, as it was worked out.
                months, p = 0, since
                while p <= before:
                    months += 1
                    p = recurrence.add_months(p, 1, first)
                spent = bind.execute(
                    sa.text(
                        'SELECT COALESCE(SUM(amount_cents), 0) FROM budget_spends'
                        ' WHERE user_id = :u AND category_id = :c AND date >= :a AND date <= :b'
                    ),
                    {'u': uid, 'c': cid, 'a': since.isoformat(), 'b': before.isoformat()},
                ).scalar_one()
                paid = bind.execute(
                    sa.text(
                        'SELECT COALESCE(SUM(p.amount_cents), 0) FROM bill_payments p'
                        ' JOIN bills bl ON bl.id = p.bill_id WHERE p.user_id = :u'
                        ' AND bl.category_id = :c AND p.due_date >= :a AND p.due_date <= :b'
                    ),
                    {'u': uid, 'c': cid, 'a': since.isoformat(), 'b': before.isoformat()},
                ).scalar_one()
                opening = opening + round_div(total * months, cycle) - spent - paid
                since = start
            estimate = max(0, total - round_div(yearly.get(cid, 0) * cycle, 12))
            bind.execute(
                sa.text(
                    'UPDATE budgets SET monthly_cents = :m, total_cents = :t,'
                    ' saved_since = :s, saved_opening_cents = :o WHERE id = :id'
                ),
                {
                    'm': round_div(estimate, cycle),
                    't': estimate,
                    's': since.isoformat() if since is not None else None,
                    'o': opening,
                    'id': bid,
                },
            )
        missing = held - has_line
        if not missing:
            continue
        spending = {
            cid
            for (cid,) in bind.execute(
                sa.text(
                    'SELECT id FROM categories WHERE user_id = :u'
                    ' AND NOT is_income AND NOT is_transfer'
                ),
                {'u': uid},
            ).all()
        }
        for cid in sorted(missing & spending):
            bind.execute(
                sa.text(
                    'INSERT INTO budgets (user_id, category_id, monthly_cents, total_cents,'
                    ' use_in_forecast, cycle_months, saved_since, saved_opening_cents)'
                    ' VALUES (:u, :c, 0, 0, 0, 1, :s, 0)'
                ),
                {'u': uid, 'c': cid, 's': start.isoformat() if cid in longer else None},
            )


def downgrade() -> None:
    # The bills go back into each line's amount; what was carried forward stays.
    bind = op.get_bind()
    tz = ZoneInfo(os.environ.get('FISCUS_TIMEZONE') or 'Australia/Perth')
    today = datetime.now(tz).date()
    start_days: dict[int, int] = dict(
        bind.execute(sa.text('SELECT user_id, budget_start_day FROM user_settings')).all()
    )
    users = [u for (u,) in bind.execute(sa.text('SELECT id FROM users')).all()]
    for uid in users:
        start = recurrence.month_period(today, start_days.get(uid) or 1)[0]
        yearly, _held, _longer = _bills(bind, uid, start)
        lines = bind.execute(
            sa.text(
                'SELECT b.id, b.category_id, b.monthly_cents, b.total_cents, b.cycle_months'
                ' FROM budgets b JOIN categories c ON c.id = b.category_id'
                ' WHERE b.user_id = :u AND NOT c.is_income'
            ),
            {'u': uid},
        ).all()
        for bid, cid, monthly, total, cycle in lines:
            total = (total if total is not None else monthly * cycle) + round_div(
                yearly.get(cid, 0) * cycle, 12
            )
            bind.execute(
                sa.text('UPDATE budgets SET monthly_cents = :m, total_cents = :t WHERE id = :id'),
                {'m': round_div(total, cycle), 't': total, 'id': bid},
            )
