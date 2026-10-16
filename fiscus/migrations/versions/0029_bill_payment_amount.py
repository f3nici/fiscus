"""bill payment amount

A paid occurrence counted in the budget at whatever its bill said now, so
changing next quarter's amount, or stopping the bill, rewrote what earlier
months had spent. A payment now keeps what was paid (D57): the transaction it
wrote when marked paid (D42), else the synced transaction it was matched to,
else its bill's amount.

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-16 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0029'
down_revision: str | None = '0028'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'bill_payments',
        sa.Column('amount_cents', sa.Integer(), nullable=False, server_default='0'),
    )
    op.execute(
        'UPDATE bill_payments SET amount_cents = COALESCE('
        ' (SELECT ABS(t.amount_cents) FROM transactions t'
        '  WHERE t.bill_payment_id = bill_payments.id),'
        ' (SELECT ABS(t.amount_cents) FROM transactions t'
        '  WHERE t.id = bill_payments.transaction_id),'
        ' (SELECT b.amount_cents FROM bills b WHERE b.id = bill_payments.bill_id),'
        ' 0)'
    )


def downgrade() -> None:
    # Dropping a column rebuilds the table, and transactions point at it with
    # ON DELETE CASCADE: only with foreign keys off, as Alembic's connection has them.
    if op.get_bind().execute(sa.text('PRAGMA foreign_keys')).scalar():
        raise RuntimeError(
            'Rebuilding bill_payments with foreign keys on would delete their transactions'
        )
    with op.batch_alter_table('bill_payments') as batch:
        batch.drop_column('amount_cents')
