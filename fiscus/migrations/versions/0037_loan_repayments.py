"""loan repayments

A home loan repayment marked paid from the budget comes off the repayment
account: one transaction linked to it, deleted with it (D76). Repayments from
before this are not marked; those a balance typed in since already has count
as made.

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-05 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0037'
down_revision: str | None = '0036'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rebuild_safe() -> None:
    # Rebuilding transactions drops the old table, and bill_payments points at
    # it with ON DELETE CASCADE: only with foreign keys off, as Alembic's
    # connection has them.
    if op.get_bind().execute(sa.text('PRAGMA foreign_keys')).scalar():
        raise RuntimeError(
            'Rebuilding transactions with foreign keys on would delete every bill payment'
            ' that points at it'
        )


def upgrade() -> None:
    _rebuild_safe()
    op.create_table(
        'loan_repayments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False
        ),
        sa.Column(
            'loan_id', sa.Integer(), sa.ForeignKey('loans.id', ondelete='CASCADE'), nullable=False
        ),
        sa.Column('due_date', sa.Date(), nullable=False),
        sa.Column('amount_cents', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('loan_id', 'due_date'),
    )
    op.create_index('ix_loan_repayments_user_id', 'loan_repayments', ['user_id'])
    op.create_index('ix_loan_repayments_loan_id', 'loan_repayments', ['loan_id'])
    with op.batch_alter_table('transactions') as batch:
        batch.add_column(sa.Column('loan_repayment_id', sa.Integer(), nullable=True))
        batch.create_foreign_key(
            'fk_transactions_loan_repayment_id',
            'loan_repayments',
            ['loan_repayment_id'],
            ['id'],
            ondelete='CASCADE',
        )
        batch.create_unique_constraint('uq_transactions_loan_repayment_id', ['loan_repayment_id'])


def downgrade() -> None:
    _rebuild_safe()
    op.execute('DELETE FROM transactions WHERE loan_repayment_id IS NOT NULL')
    with op.batch_alter_table('transactions') as batch:
        batch.drop_constraint('uq_transactions_loan_repayment_id', type_='unique')
        batch.drop_constraint('fk_transactions_loan_repayment_id', type_='foreignkey')
        batch.drop_column('loan_repayment_id')
    op.drop_index('ix_loan_repayments_loan_id', table_name='loan_repayments')
    op.drop_index('ix_loan_repayments_user_id', table_name='loan_repayments')
    op.drop_table('loan_repayments')
