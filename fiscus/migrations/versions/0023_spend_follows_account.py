"""spend follows account

Spending taken off a budget by hand writes a transaction in the account it
was paid from, so its balance moves until the next is typed in (D49). The
transaction is deleted with the spend. Spends from before this move nothing.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-15 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0023'
down_revision: str | None = '0022'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('transactions') as batch:
        batch.add_column(sa.Column('budget_spend_id', sa.Integer(), nullable=True))
        batch.create_foreign_key(
            'fk_transactions_budget_spend_id',
            'budget_spends',
            ['budget_spend_id'],
            ['id'],
            ondelete='CASCADE',
        )
        batch.create_unique_constraint('uq_transactions_budget_spend_id', ['budget_spend_id'])


def downgrade() -> None:
    op.execute('DELETE FROM transactions WHERE budget_spend_id IS NOT NULL')
    with op.batch_alter_table('transactions') as batch:
        batch.drop_constraint('uq_transactions_budget_spend_id', type_='unique')
        batch.drop_constraint('fk_transactions_budget_spend_id', type_='foreignkey')
        batch.drop_column('budget_spend_id')
