"""variable pay

A pay schedule can vary (casual work): it then expects the recent average of
deposits whose description contains its match text.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-02 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('pay_schedules', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('varies', sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column('match_text', sa.String(length=120), nullable=False, server_default='')
        )


def downgrade() -> None:
    with op.batch_alter_table('pay_schedules', schema=None) as batch_op:
        batch_op.drop_column('match_text')
        batch_op.drop_column('varies')
