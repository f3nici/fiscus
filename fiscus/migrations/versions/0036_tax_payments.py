"""tax payments

Income tax paid ahead with a BAS (PAYG instalments) counts as tax paid in the
financial year of its quarter (D75). One paid from an account writes a
transaction linked to it, deleted with it; one taken out of the tax goal is
that goal move.

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-02 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0036'
down_revision: str | None = '0035'
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
        'tax_payments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False
        ),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('quarter', sa.Date(), nullable=False),
        sa.Column('amount_cents', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column(
            'goal_move_id',
            sa.Integer(),
            sa.ForeignKey('goal_moves.id', ondelete='SET NULL'),
            nullable=True,
            unique=True,
        ),
    )
    op.create_index('ix_tax_payments_user_quarter', 'tax_payments', ['user_id', 'quarter'])
    with op.batch_alter_table('transactions') as batch:
        batch.add_column(sa.Column('tax_payment_id', sa.Integer(), nullable=True))
        batch.create_foreign_key(
            'fk_transactions_tax_payment_id',
            'tax_payments',
            ['tax_payment_id'],
            ['id'],
            ondelete='CASCADE',
        )
        batch.create_unique_constraint('uq_transactions_tax_payment_id', ['tax_payment_id'])


def downgrade() -> None:
    _rebuild_safe()
    op.execute('DELETE FROM transactions WHERE tax_payment_id IS NOT NULL')
    with op.batch_alter_table('transactions') as batch:
        batch.drop_constraint('uq_transactions_tax_payment_id', type_='unique')
        batch.drop_constraint('fk_transactions_tax_payment_id', type_='foreignkey')
        batch.drop_column('tax_payment_id')
    op.drop_index('ix_tax_payments_user_quarter', table_name='tax_payments')
    op.drop_table('tax_payments')
