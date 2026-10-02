"""square

A Square seller account synced into one of the person's accounts, and the
unpaid invoices from the last sync.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-02 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_index('ix_square_invoices_user_id', table_name='square_invoices')
    op.drop_table('square_invoices')
    op.drop_table('square_connections')
