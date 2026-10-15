"""chart lines

Lines turned off or on in the forecast chart's legend are kept per person,
so they stay that way on every device until turned back (D50).

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-15 13:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0024'
down_revision: str | None = '0023'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('user_settings') as batch:
        batch.add_column(sa.Column('chart_lines', sa.Text(), nullable=False, server_default='{}'))


def downgrade() -> None:
    with op.batch_alter_table('user_settings') as batch:
        batch.drop_column('chart_lines')
