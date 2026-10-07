"""payslip reminders

An ntfy reminder for a payday that passed without a payslip, on by default
like the other reminders (they only send once ntfy is turned on).

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-07 11:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0018'
down_revision: str | None = '0017'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('user_settings') as batch:
        batch.add_column(
            sa.Column('ntfy_payslips', sa.Boolean(), nullable=False, server_default=sa.true())
        )


def downgrade() -> None:
    with op.batch_alter_table('user_settings') as batch:
        batch.drop_column('ntfy_payslips')
