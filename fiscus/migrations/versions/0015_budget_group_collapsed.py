"""budget group collapsed

A group can be folded so the budget page shows only its totals.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-06 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0015'
down_revision: str | None = '0014'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('budget_groups') as batch:
        batch.add_column(
            sa.Column('collapsed', sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table('budget_groups') as batch:
        batch.drop_column('collapsed')
