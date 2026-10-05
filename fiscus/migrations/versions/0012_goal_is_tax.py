"""goal is_tax

A goal can hold the person's tax money, counted as set aside for tax.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-05 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0012'
down_revision: str | None = '0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('goals') as batch:
        batch.add_column(
            sa.Column('is_tax', sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table('goals') as batch:
        batch.drop_column('is_tax')
