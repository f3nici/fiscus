"""bill varies

A bill can be marked as varying (power, water, gas): each occurrence is
marked paid at what it came to, and its amount becomes the average of the
last three paid (D66). Every bill starts as not varying.

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-19 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0032'
down_revision: str | None = '0031'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'bills', sa.Column('varies', sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade() -> None:
    # Dropping a column rebuilds the table, and bill_payments points at it with
    # ON DELETE CASCADE: only with foreign keys off, as Alembic's connection has them.
    if op.get_bind().execute(sa.text('PRAGMA foreign_keys')).scalar():
        raise RuntimeError(
            'Rebuilding bills with foreign keys on would delete every payment that points at it'
        )
    with op.batch_alter_table('bills') as batch:
        batch.drop_column('varies')
