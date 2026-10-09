"""balance follows payments

A bill marked paid by hand and a payslip each write a transaction in their
account, so its balance moves until the next one is typed in (D42). The
transaction is deleted with the payment or payslip.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-09 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0020'
down_revision: str | None = '0019'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('transactions') as batch:
        batch.add_column(sa.Column('bill_payment_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('payslip_id', sa.Integer(), nullable=True))
        batch.create_foreign_key(
            'fk_transactions_bill_payment_id',
            'bill_payments',
            ['bill_payment_id'],
            ['id'],
            ondelete='CASCADE',
        )
        batch.create_foreign_key(
            'fk_transactions_payslip_id', 'payslips', ['payslip_id'], ['id'], ondelete='CASCADE'
        )
        batch.create_unique_constraint('uq_transactions_bill_payment_id', ['bill_payment_id'])
        batch.create_unique_constraint('uq_transactions_payslip_id', ['payslip_id'])


def downgrade() -> None:
    op.execute('DELETE FROM transactions WHERE bill_payment_id IS NOT NULL')
    op.execute('DELETE FROM transactions WHERE payslip_id IS NOT NULL')
    with op.batch_alter_table('transactions') as batch:
        batch.drop_constraint('uq_transactions_payslip_id', type_='unique')
        batch.drop_constraint('uq_transactions_bill_payment_id', type_='unique')
        batch.drop_constraint('fk_transactions_payslip_id', type_='foreignkey')
        batch.drop_constraint('fk_transactions_bill_payment_id', type_='foreignkey')
        batch.drop_column('payslip_id')
        batch.drop_column('bill_payment_id')
