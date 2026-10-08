"""account balances

Since balances are typed in (D25), typing one moved the opening balance, so
every past day took today's figure: an offset entered today claimed a year
of savings. Each typed balance is now kept by day. Existing accounts start
from the balance as at the day it was last typed in.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-07 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0019'
down_revision: str | None = '0018'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'account_balances',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False
        ),
        sa.Column(
            'account_id',
            sa.Integer(),
            sa.ForeignKey('accounts.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('balance_cents', sa.Integer(), nullable=False),
        sa.UniqueConstraint('account_id', 'date'),
    )
    op.create_index('ix_account_balances_user_id', 'account_balances', ['user_id'])
    op.execute(
        'INSERT INTO account_balances (user_id, account_id, date, balance_cents) '
        'SELECT a.user_id, a.id, a.balance_updated_on, a.opening_balance_cents + COALESCE('
        '(SELECT SUM(t.amount_cents) FROM transactions t '
        'WHERE t.account_id = a.id AND t.date <= a.balance_updated_on), 0) '
        'FROM accounts a WHERE a.balance_updated_on IS NOT NULL'
    )


def downgrade() -> None:
    op.drop_index('ix_account_balances_user_id', 'account_balances')
    op.drop_table('account_balances')
