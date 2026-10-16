"""no import mappings

`import_mappings` held the column mappings of statement imports, which went
in D37; nothing has read it since. It only points at other tables, so it is
dropped without rebuilding any.

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-16 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0027'
down_revision: str | None = '0026'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index('ix_import_mappings_user_id', 'import_mappings')
    op.drop_table('import_mappings')


def downgrade() -> None:
    op.create_table(
        'import_mappings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('mapping', sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('account_id'),
    )
    op.create_index('ix_import_mappings_user_id', 'import_mappings', ['user_id'], unique=False)
