"""pay seasonal

Pay that varies, or irregular pay, can expect what it paid the same month
last year rather than its average (D77). Every pay schedule starts on its
average.

Revision ID: 0038
Revises: 0037
Create Date: 2026-11-08 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0038'
down_revision: str | None = '0037'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'pay_schedules',
        sa.Column('seasonal', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    # Dropping a column rebuilds the table, and payslips points at it with
    # ON DELETE SET NULL: only with foreign keys off, as Alembic's connection
    # has them, or every payslip would lose its pay.
    if op.get_bind().execute(sa.text('PRAGMA foreign_keys')).scalar():
        raise RuntimeError(
            'Rebuilding pay_schedules with foreign keys on would unlink every payslip'
            ' that points at it'
        )
    with op.batch_alter_table('pay_schedules') as batch:
        batch.drop_column('seasonal')
