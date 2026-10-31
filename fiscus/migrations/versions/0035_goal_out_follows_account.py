"""goal out follows account

Money taken out of a goal is spent, so it writes a transaction in the goal's
account and its balance moves until the next is typed in (D73). The
transaction is deleted with the goal move. Moves from before this move nothing.

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-31 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0035'
down_revision: str | None = '0034'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rebuild_safe() -> None:
    # Rebuilding transactions drops the old table, and bill_payments points at
    # it with ON DELETE CASCADE: only with foreign keys off, as Alembic's
    # connection has them.
    if op.get_bind().execute(sa.text('PRAGMA foreign_keys')).scalar():
        raise RuntimeError(
            'Rebuilding transactions with foreign keys on would delete every bill payment'
            ' that points at it'
        )


def upgrade() -> None:
    _rebuild_safe()
    with op.batch_alter_table('transactions') as batch:
        batch.add_column(sa.Column('goal_move_id', sa.Integer(), nullable=True))
        batch.create_foreign_key(
            'fk_transactions_goal_move_id',
            'goal_moves',
            ['goal_move_id'],
            ['id'],
            ondelete='CASCADE',
        )
        batch.create_unique_constraint('uq_transactions_goal_move_id', ['goal_move_id'])


def downgrade() -> None:
    _rebuild_safe()
    op.execute('DELETE FROM transactions WHERE goal_move_id IS NOT NULL')
    with op.batch_alter_table('transactions') as batch:
        batch.drop_constraint('uq_transactions_goal_move_id', type_='unique')
        batch.drop_constraint('fk_transactions_goal_move_id', type_='foreignkey')
        batch.drop_column('goal_move_id')
