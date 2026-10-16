"""no password columns

Password sign-in went in D52 and nothing has read `users.password_enabled` or
`users.password_hash` since; they go. SQLite drops a column by rebuilding the
table, and every owned table points at `users` with ON DELETE CASCADE, so the
rebuild runs only with foreign keys off, as Alembic's connection has them.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-16 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0026'
down_revision: str | None = '0025'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _refuse_with_foreign_keys_on() -> None:
    if op.get_bind().execute(sa.text('PRAGMA foreign_keys')).scalar():
        raise RuntimeError(
            'Rebuilding users with foreign keys on would delete every row that points at it'
        )


def upgrade() -> None:
    _refuse_with_foreign_keys_on()
    with op.batch_alter_table('users') as batch:
        batch.drop_column('password_hash')
        batch.drop_column('password_enabled')


def downgrade() -> None:
    _refuse_with_foreign_keys_on()
    with op.batch_alter_table('users') as batch:
        batch.add_column(
            sa.Column('password_enabled', sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(sa.Column('password_hash', sa.String(length=255), nullable=True))
