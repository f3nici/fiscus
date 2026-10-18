"""monthly line saves for its bills

A monthly line holding a bill paid less often (a phone plan and its yearly
device cover) started again each month, so the yearly bill's share never
built up and the month it fell due was over (D63). Such a line is now saved
up. One that holds such a bill now starts saving from the budget month this
runs in, from what it has left in it, so nothing it shows this month moves.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-18 12:00:00
"""

import os
from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from alembic import op

from fiscus.domain import recurrence

revision: str = '0030'
down_revision: str | None = '0029'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Bills paid less often than monthly, as `budgeting.BILL_CYCLES` had them.
LONGER = ('bimonthly', 'quarterly', 'yearly')


def upgrade() -> None:
    bind = op.get_bind()
    tz = ZoneInfo(os.environ.get('FISCUS_TIMEZONE') or 'Australia/Perth')
    today = datetime.now(tz).date()
    start_days: dict[int, int] = dict(
        bind.execute(sa.text('SELECT user_id, budget_start_day FROM user_settings')).all()
    )
    lines = bind.execute(
        sa.text(
            'SELECT b.id, b.user_id, b.category_id FROM budgets b'
            ' JOIN categories c ON c.id = b.category_id'
            ' WHERE b.cycle_months = 1 AND b.saved_since IS NULL AND NOT c.is_income'
        )
    ).all()
    holds = sa.text(
        'SELECT 1 FROM bills WHERE user_id = :uid AND category_id = :cid AND active'
        ' AND frequency IN :longer AND (end_date IS NULL OR end_date >= :start)'
    ).bindparams(sa.bindparam('longer', expanding=True))
    for bid, uid, cid in lines:
        start = recurrence.month_period(today, start_days.get(uid) or 1)[0].isoformat()
        found = bind.execute(
            holds, {'uid': uid, 'cid': cid, 'longer': list(LONGER), 'start': start}
        ).first()
        if found is not None:
            bind.execute(
                sa.text(
                    'UPDATE budgets SET saved_since = :start, saved_opening_cents = 0'
                    ' WHERE id = :id'
                ),
                {'start': start, 'id': bid},
            )


def downgrade() -> None:
    # Before this a monthly line kept no saving.
    op.execute('UPDATE budgets SET saved_since = NULL, saved_opening_cents = 0 WHERE cycle_months = 1')
