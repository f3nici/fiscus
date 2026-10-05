"""budget cycle

A budget line can be spent every few months; its monthly share is saved up.
Existing lines stay monthly.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-05 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('budgets', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('cycle_months', sa.Integer(), nullable=False, server_default='1')
        )
        batch_op.add_column(sa.Column('saved_since', sa.Date(), nullable=True))
        batch_op.add_column(
            sa.Column('saved_opening_cents', sa.Integer(), nullable=False, server_default='0')
        )


def downgrade() -> None:
    with op.batch_alter_table('budgets', schema=None) as batch_op:
        batch_op.drop_column('saved_opening_cents')
        batch_op.drop_column('saved_since')
        batch_op.drop_column('cycle_months')
