"""goals

Savings goals held inside accounts, and the money put into or taken out of them.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-05 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0011'
down_revision: str | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'goals',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=True),
        sa.Column('target_cents', sa.Integer(), nullable=True),
        sa.Column('target_date', sa.Date(), nullable=True),
        sa.Column('keep_aside', sa.Boolean(), nullable=False),
        sa.Column('archived', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_goals_user_id', 'goals', ['user_id'])
    op.create_table(
        'goal_moves',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('goal_id', sa.Integer(), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('amount_cents', sa.Integer(), nullable=False),
        sa.Column('note', sa.String(length=120), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['goal_id'], ['goals.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_goal_moves_user_date', 'goal_moves', ['user_id', 'date'])
    op.create_index('ix_goal_moves_goal_id', 'goal_moves', ['goal_id'])


def downgrade() -> None:
    op.drop_index('ix_goal_moves_goal_id', table_name='goal_moves')
    op.drop_index('ix_goal_moves_user_date', table_name='goal_moves')
    op.drop_table('goal_moves')
    op.drop_index('ix_goals_user_id', table_name='goals')
    op.drop_table('goals')
