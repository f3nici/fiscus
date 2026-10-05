"""budget is the allocation

A budget's amount becomes the whole allocation for its category; bills in it
are paid out of it rather than added on top. Each stored amount (what was on
top of the bills) gets the person's recurring bills in the category added, and
a category budgeted only by its bills gets a budget of that, so the budget a
person sees is unchanged.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-05 12:00:00
"""

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from fiscus.domain import recurrence, splits

revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _bills_by_category(conn: Any) -> dict[tuple[int, int], int]:
    bills = {
        r.id: r
        for r in conn.execute(
            sa.text(
                'SELECT id, owner_id, amount_cents, frequency, split_mode FROM bills'
                " WHERE active AND frequency != 'one_off'"
                " AND (end_date IS NULL OR end_date >= date('now'))"
            )
        )
    }
    shares: dict[int, list[Any]] = defaultdict(list)
    for s in conn.execute(
        sa.text('SELECT bill_id, user_id, split_value, category_id FROM bill_shares ORDER BY id')
    ):
        if s.bill_id in bills:
            shares[s.bill_id].append(s)
    out: dict[tuple[int, int], int] = defaultdict(int)
    for bill_id, rows in shares.items():
        b = bills[bill_id]
        try:
            amounts = splits.compute(
                b.amount_cents,
                b.split_mode,
                [splits.ShareSpec(s.user_id, s.split_value) for s in rows],
                b.owner_id,
            )
        except splits.SplitError:
            continue
        for s in rows:
            if s.category_id is not None:
                out[(s.user_id, s.category_id)] += recurrence.monthly_equivalent(
                    amounts[s.user_id], b.frequency
                )
    return out


def upgrade() -> None:
    conn = op.get_bind()
    from_bills = _bills_by_category(conn)
    for bud in conn.execute(
        sa.text('SELECT id, user_id, category_id, monthly_cents FROM budgets')
    ).fetchall():
        key = (bud.user_id, bud.category_id)
        if from_bills.get(key):
            conn.execute(
                sa.text('UPDATE budgets SET monthly_cents = :c WHERE id = :id'),
                {'c': bud.monthly_cents + from_bills[key], 'id': bud.id},
            )
        from_bills.pop(key, None)
    for (user_id, category_id), cents in from_bills.items():
        if cents > 0:
            conn.execute(
                sa.text(
                    'INSERT INTO budgets (user_id, category_id, monthly_cents, use_in_forecast)'
                    ' VALUES (:u, :c, :m, 0)'
                ),
                {'u': user_id, 'c': category_id, 'm': cents},
            )


def downgrade() -> None:
    conn = op.get_bind()
    from_bills = _bills_by_category(conn)
    for bud in conn.execute(
        sa.text('SELECT id, user_id, category_id, monthly_cents, use_in_forecast FROM budgets')
    ).fetchall():
        extra = max(0, bud.monthly_cents - from_bills.get((bud.user_id, bud.category_id), 0))
        if extra == 0 and not bud.use_in_forecast:
            conn.execute(sa.text('DELETE FROM budgets WHERE id = :id'), {'id': bud.id})
        else:
            conn.execute(
                sa.text('UPDATE budgets SET monthly_cents = :c WHERE id = :id'),
                {'c': extra, 'id': bud.id},
            )
