"""no square

Square sync is gone (D55), and with it what only it used: its connection
(holding the access token) and invoices, the categorisation rules it applied,
and the three columns bills were matched to its payments by. Transactions it
synced stay, as do bill payments matched to them: account history points at
both. Dropping columns from `bills` rebuilds it, and `bill_payments` points at
it with ON DELETE CASCADE, so this runs only with foreign keys off, as
Alembic's connection has them.

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-16 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0028'
down_revision: str | None = '0027'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _refuse_with_foreign_keys_on() -> None:
    if op.get_bind().execute(sa.text('PRAGMA foreign_keys')).scalar():
        raise RuntimeError(
            'Rebuilding bills with foreign keys on would delete every payment that points at it'
        )


def upgrade() -> None:
    _refuse_with_foreign_keys_on()
    op.drop_index('ix_square_invoices_user_id', 'square_invoices')
    op.drop_table('square_invoices')
    op.drop_table('square_connections')
    op.drop_index('ix_rules_user_id', 'rules')
    op.drop_table('rules')
    with op.batch_alter_table('bills') as batch:
        batch.drop_column('description_hint')
        batch.drop_column('match_window_days')
        batch.drop_column('match_tolerance_pct')


def downgrade() -> None:
    _refuse_with_foreign_keys_on()
    with op.batch_alter_table('bills') as batch:
        batch.add_column(
            sa.Column('match_tolerance_pct', sa.Integer(), nullable=False, server_default='5')
        )
        batch.add_column(
            sa.Column('match_window_days', sa.Integer(), nullable=False, server_default='3')
        )
        batch.add_column(
            sa.Column('description_hint', sa.String(length=120), nullable=False, server_default='')
        )
    op.create_table(
        'rules',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('pattern', sa.String(length=200), nullable=False),
        sa.Column('is_regex', sa.Boolean(), nullable=False),
        sa.Column('category_id', sa.Integer(), nullable=False),
        sa.Column('priority', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_rules_user_id', 'rules', ['user_id'], unique=False)
    op.create_table(
        'square_connections',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('access_token', sa.String(length=255), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=True),
        sa.Column('income_category_id', sa.Integer(), nullable=True),
        sa.Column('fee_category_id', sa.Integer(), nullable=True),
        sa.Column('payout_category_id', sa.Integer(), nullable=True),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('synced_until', sa.DateTime(), nullable=True),
        sa.Column('last_sync_at', sa.DateTime(), nullable=True),
        sa.Column('last_result', sa.String(length=255), nullable=False),
        sa.Column('last_error', sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['income_category_id'], ['categories.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['fee_category_id'], ['categories.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['payout_category_id'], ['categories.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('user_id'),
    )
    op.create_table(
        'square_invoices',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('square_id', sa.String(length=120), nullable=False),
        sa.Column('number', sa.String(length=120), nullable=False),
        sa.Column('customer', sa.String(length=255), nullable=False),
        sa.Column('due_date', sa.Date(), nullable=False),
        sa.Column('amount_due_cents', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_square_invoices_user_id', 'square_invoices', ['user_id'])
