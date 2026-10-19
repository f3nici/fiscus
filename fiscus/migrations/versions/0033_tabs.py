"""tabs

The screens on a person's bottom tabs, in order, as a JSON list of keys
(D67). Empty is the default set, so nobody's tabs move on upgrade but for
More, which the menu replaces.

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-19 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0033'
down_revision: str | None = '0032'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('user_settings') as batch:
        batch.add_column(sa.Column('tabs', sa.Text(), nullable=False, server_default=''))


def downgrade() -> None:
    with op.batch_alter_table('user_settings') as batch:
        batch.drop_column('tabs')
