"""bills absorb housing

Housing items already had a bill each; the bills stay and the items go.
A budget's amount becomes what is set aside on top of the person's bills in
that category, so the budget a person sees (bills plus that) is unchanged.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-02 12:00:00
"""

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from fiscus.domain import recurrence, splits

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    bills = {
        r.id: r
        for r in conn.execute(
            sa.text(
                'SELECT id, owner_id, amount_cents, frequency, split_mode FROM bills WHERE active'
            )
        )
    }
    shares: dict[int, list[Any]] = defaultdict(list)
    for s in conn.execute(
        sa.text('SELECT bill_id, user_id, split_value, category_id FROM bill_shares ORDER BY id')
    ):
        if s.bill_id in bills:
            shares[s.bill_id].append(s)
    from_bills: dict[tuple[int, int], int] = defaultdict(int)
    for bill_id, rows in shares.items():
        b = bills[bill_id]
        try:
            amounts = splits.compute(
                b.amount_cents,
                b.split_mode,
                [splits.ShareSpec(s.user_id, s.split_value) for s in rows],
                b.owner_id,
            )
        except splits.SplitError:
            continue
        for s in rows:
            if s.category_id is not None:
                from_bills[(s.user_id, s.category_id)] += recurrence.monthly_equivalent(
                    amounts[s.user_id], b.frequency
                )
    for bud in conn.execute(
        sa.text('SELECT id, user_id, category_id, monthly_cents, use_in_forecast FROM budgets')
    ).fetchall():
        extra = max(0, bud.monthly_cents - from_bills.get((bud.user_id, bud.category_id), 0))
        if extra == 0 and not bud.use_in_forecast:
            conn.execute(sa.text('DELETE FROM budgets WHERE id = :id'), {'id': bud.id})
        else:
            conn.execute(
                sa.text('UPDATE budgets SET monthly_cents = :c WHERE id = :id'),
                {'c': extra, 'id': bud.id},
            )

    with op.batch_alter_table('bills', schema=None) as batch_op:
        batch_op.drop_column('housing_item_id')
    with op.batch_alter_table('housing_items', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_housing_items_user_id'))
    op.drop_table('housing_items')


def downgrade() -> None:
    # The items and the old budget amounts are not restored: their bills remain.
    op.create_table(
        'housing_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('amount_cents', sa.Integer(), nullable=False),
        sa.Column('frequency', sa.String(length=20), nullable=False),
        sa.Column('category_id', sa.Integer(), nullable=False),
        sa.Column('due_date', sa.Date(), nullable=True),
        sa.Column('shared_with_id', sa.Integer(), nullable=True),
        sa.Column('shared_split_bp', sa.Integer(), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['shared_with_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'name'),
    )
    with op.batch_alter_table('housing_items', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_housing_items_user_id'), ['user_id'], unique=False)
    with op.batch_alter_table('bills', schema=None) as batch_op:
        batch_op.add_column(sa.Column('housing_item_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_bills_housing_item_id',
            'housing_items',
            ['housing_item_id'],
            ['id'],
            ondelete='SET NULL',
        )
