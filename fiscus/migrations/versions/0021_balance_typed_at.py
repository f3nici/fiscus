"""balance typed at

A balance typed in held everything dated its day, so pay put in that
afternoon, after the morning's balance, moved nothing. When it was typed is
now kept, and what is put in after it, dated its day, moves it (D45).
Balances typed in before this have no time and hold their whole day.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-14 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0021'
down_revision: str | None = '0020'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('account_balances') as batch:
        batch.add_column(sa.Column('typed_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('account_balances') as batch:
        batch.drop_column('typed_at')
