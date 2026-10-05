"""budget groups

Budget lines can be grouped under headings and put in order by hand.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-05 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0014'
down_revision: str | None = '0013'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'budget_groups',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_budget_groups_user_id', 'budget_groups', ['user_id'])
    with op.batch_alter_table('categories') as batch:
        batch.add_column(sa.Column('budget_group_id', sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column('budget_position', sa.Integer(), nullable=False, server_default='0')
        )
        batch.create_foreign_key(
            'fk_categories_budget_group_id',
            'budget_groups',
            ['budget_group_id'],
            ['id'],
            ondelete='SET NULL',
        )


def downgrade() -> None:
    with op.batch_alter_table('categories') as batch:
        batch.drop_constraint('fk_categories_budget_group_id', type_='foreignkey')
        batch.drop_column('budget_position')
        batch.drop_column('budget_group_id')
    op.drop_index('ix_budget_groups_user_id', table_name='budget_groups')
    op.drop_table('budget_groups')
