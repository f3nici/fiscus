"""no bill markers

The forecast chart no longer marks paydays and large bills with dots (D46),
so the size of bill it marked from is not kept.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-14 13:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0022'
down_revision: str | None = '0021'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('user_settings') as batch:
        batch.drop_column('large_bill_cents')


def downgrade() -> None:
    with op.batch_alter_table('user_settings') as batch:
        batch.add_column(
            sa.Column('large_bill_cents', sa.Integer(), nullable=False, server_default='50000')
        )
