"""loan first repayment

The date of a loan's first repayment, as the bank set it; later ones follow
every period from it (D71). Empty is one period after the start, as before,
so no loan's repayments move on upgrade.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-27 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0034'
down_revision: str | None = '0033'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('loans', sa.Column('first_repayment_date', sa.Date(), nullable=True))


def downgrade() -> None:
    # Dropping a column rebuilds the table, and loan_rates points at it with
    # ON DELETE CASCADE: only with foreign keys off, as Alembic's connection has them.
    if op.get_bind().execute(sa.text('PRAGMA foreign_keys')).scalar():
        raise RuntimeError(
            'Rebuilding loans with foreign keys on would delete every rate that points at it'
        )
    with op.batch_alter_table('loans') as batch:
        batch.drop_column('first_repayment_date')
