"""goal many lines

A goal can be saved from several budget lines: the link moves from the goal to
the line, so each line saves into at most one goal.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-06 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0016'
down_revision: str | None = '0015'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('categories') as batch:
        batch.add_column(sa.Column('goal_id', sa.Integer(), nullable=True))
        batch.create_foreign_key(
            'fk_categories_goal_id', 'goals', ['goal_id'], ['id'], ondelete='SET NULL'
        )
    # A line saved into more than one goal keeps the open one made first.
    op.execute(
        'UPDATE categories SET goal_id = ('
        ' SELECT g.id FROM goals g WHERE g.category_id = categories.id'
        ' ORDER BY g.archived, g.id LIMIT 1)'
    )
    with op.batch_alter_table('goals') as batch:
        batch.drop_constraint('fk_goals_category_id', type_='foreignkey')
        batch.drop_column('category_id')


def downgrade() -> None:
    with op.batch_alter_table('goals') as batch:
        batch.add_column(sa.Column('category_id', sa.Integer(), nullable=True))
        batch.create_foreign_key(
            'fk_goals_category_id', 'categories', ['category_id'], ['id'], ondelete='SET NULL'
        )
    # A goal keeps one of its lines.
    op.execute(
        'UPDATE goals SET category_id = ('
        ' SELECT MIN(c.id) FROM categories c WHERE c.goal_id = goals.id)'
    )
    with op.batch_alter_table('categories') as batch:
        batch.drop_constraint('fk_categories_goal_id', type_='foreignkey')
        batch.drop_column('goal_id')
