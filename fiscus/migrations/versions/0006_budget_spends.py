"""budget spends

Spending logged against a budget by hand, the day each person's budget month
starts, and when an account's balance was last typed in.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-04 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'budget_spends',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('category_id', sa.Integer(), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('amount_cents', sa.Integer(), nullable=False),
        sa.Column('note', sa.String(length=120), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_budget_spends_user_date', 'budget_spends', ['user_id', 'date'])
    with op.batch_alter_table('user_settings') as batch:
        batch.add_column(
            sa.Column('budget_start_day', sa.Integer(), nullable=False, server_default='1')
        )
    with op.batch_alter_table('accounts') as batch:
        batch.add_column(sa.Column('balance_updated_on', sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('accounts') as batch:
        batch.drop_column('balance_updated_on')
    with op.batch_alter_table('user_settings') as batch:
        batch.drop_column('budget_start_day')
    op.drop_index('ix_budget_spends_user_date', table_name='budget_spends')
    op.drop_table('budget_spends')
