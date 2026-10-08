"""budget total per cycle

A line saved up yearly was stored only as a monthly amount, so $2,480 a year
came back as $2,480.04. The amount as typed, for the line's cycle, is kept
beside it. Existing lines take their monthly amount times the cycle.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-07 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0017'
down_revision: str | None = '0016'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('budgets') as batch:
        batch.add_column(sa.Column('total_cents', sa.Integer(), nullable=True))
    op.execute('UPDATE budgets SET total_cents = monthly_cents * cycle_months')


def downgrade() -> None:
    with op.batch_alter_table('budgets') as batch:
        batch.drop_column('total_cents')
