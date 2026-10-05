"""goal budget line

A goal can be saved from a budget line: money put aside is a spend on the line
and a move into the goal, linked so they are removed together.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-05 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0013'
down_revision: str | None = '0012'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('goals') as batch:
        batch.add_column(sa.Column('category_id', sa.Integer(), nullable=True))
        batch.create_foreign_key(
            'fk_goals_category_id', 'categories', ['category_id'], ['id'], ondelete='SET NULL'
        )
    with op.batch_alter_table('goal_moves') as batch:
        batch.add_column(sa.Column('spend_id', sa.Integer(), nullable=True))
        batch.create_foreign_key(
            'fk_goal_moves_spend_id', 'budget_spends', ['spend_id'], ['id'], ondelete='SET NULL'
        )


def downgrade() -> None:
    with op.batch_alter_table('goal_moves') as batch:
        batch.drop_constraint('fk_goal_moves_spend_id', type_='foreignkey')
        batch.drop_column('spend_id')
    with op.batch_alter_table('goals') as batch:
        batch.drop_constraint('fk_goals_category_id', type_='foreignkey')
        batch.drop_column('category_id')
