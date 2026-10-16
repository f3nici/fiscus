"""bills not shared

Fiscus is kept by one person (D52), so a bill is no longer shared. It belongs
to its owner and carries the account and category that sat on the owner's
share. A bill shared with someone else becomes one bill each, at each person's
share, with their own account, category, payments and reminders sent, so
nobody's calendar, budget or forecast moves. A payment left by someone no
longer sharing a bill goes; the transaction it wrote stays, so no balance
moves either.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-16 12:00:00
"""

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from fiscus.domain import splits

revision: str = '0025'
down_revision: str | None = '0024'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copied as they are onto each participant's own bill.
KEPT = (
    'name',
    'frequency',
    'next_due',
    'end_date',
    'split_mode',
    'match_tolerance_pct',
    'match_window_days',
    'description_hint',
    'active',
    'created_at',
)


def _amounts(bill: Any, shares: list[Any]) -> dict[int, int]:
    """Each share as the bill showed it. Values that do not add up (which the
    pages could not show either) split it evenly."""
    specs = [splits.ShareSpec(s.user_id, s.split_value) for s in shares]
    if bill.owner_id not in {s.user_id for s in shares}:
        specs.append(splits.ShareSpec(bill.owner_id, 0))
    try:
        return splits.compute(bill.amount_cents, bill.split_mode, specs, bill.owner_id)
    except splits.SplitError:
        even = [splits.ShareSpec(s.user_id, 0) for s in specs]
        return splits.compute(bill.amount_cents, 'equal', even, bill.owner_id)


def upgrade() -> None:
    with op.batch_alter_table('bills') as batch:
        batch.add_column(sa.Column('account_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('category_id', sa.Integer(), nullable=True))
        batch.create_foreign_key(
            'fk_bills_account_id', 'accounts', ['account_id'], ['id'], ondelete='SET NULL'
        )
        batch.create_foreign_key(
            'fk_bills_category_id', 'categories', ['category_id'], ['id'], ondelete='SET NULL'
        )

    conn = op.get_bind()
    shares: dict[int, list[Any]] = defaultdict(list)
    for s in conn.execute(
        sa.text(
            'SELECT bill_id, user_id, split_value, account_id, category_id'
            ' FROM bill_shares ORDER BY id'
        )
    ):
        shares[s.bill_id].append(s)
    bills = conn.execute(
        sa.text(f'SELECT id, owner_id, amount_cents, {", ".join(KEPT)} FROM bills ORDER BY id')  # noqa: S608
    ).all()
    for bill in bills:
        rows = shares.get(bill.id, [])
        own = next((s for s in rows if s.user_id == bill.owner_id), None)
        # Anyone but the owner on it, even alone: work out each share.
        others = any(s.user_id != bill.owner_id for s in rows)
        amounts = _amounts(bill, rows) if others else {}
        conn.execute(
            sa.text(
                'UPDATE bills SET account_id = :a, category_id = :c, amount_cents = :cents'
                ' WHERE id = :id'
            ),
            {
                'a': own.account_id if own else None,
                'c': own.category_id if own else None,
                'cents': amounts.get(bill.owner_id, bill.amount_cents),
                'id': bill.id,
            },
        )
        for s in rows:
            if s.user_id == bill.owner_id:
                continue
            new_id = conn.execute(
                sa.text(
                    f'INSERT INTO bills (owner_id, amount_cents, account_id, category_id,'  # noqa: S608
                    f' {", ".join(KEPT)}) VALUES (:owner, :cents, :a, :c,'
                    f' {", ".join(":" + k for k in KEPT)})'
                ),
                {
                    'owner': s.user_id,
                    'cents': amounts[s.user_id],
                    'a': s.account_id,
                    'c': s.category_id,
                    **{k: getattr(bill, k) for k in KEPT},
                },
            ).lastrowid
            moved = {'new': new_id, 'old': bill.id, 'uid': s.user_id}
            conn.execute(
                sa.text(
                    'UPDATE bill_payments SET bill_id = :new WHERE bill_id = :old AND user_id = :uid'
                ),
                moved,
            )
            # Reminders key on the bill, so one sent is not sent again.
            conn.execute(
                sa.text(
                    "UPDATE notification_log SET key = :new || substr(key, length(:old) + 1)"
                    " WHERE user_id = :uid AND kind IN ('bill_due', 'overdue')"
                    " AND key LIKE :old || ':%'"
                ),
                {**moved, 'new': str(new_id), 'old': str(bill.id)},
            )

    # Someone who left a bill: their payment goes, the money it moved stays.
    # The kept transaction gives up the payment's dedup key too: SQLite can
    # hand the deleted payment's id to the next one, whose transaction would
    # then clash with it on the same account.
    orphans = (
        'SELECT p.id FROM bill_payments p JOIN bills b ON b.id = p.bill_id'
        ' WHERE p.user_id != b.owner_id'
    )
    conn.execute(
        sa.text(
            "UPDATE transactions SET bill_payment_id = NULL, dedup_hash = 'bill-left:' || id"  # noqa: S608
            f' WHERE bill_payment_id IN ({orphans})'
        )
    )
    conn.execute(sa.text(f'DELETE FROM bill_payments WHERE id IN ({orphans})'))  # noqa: S608

    with op.batch_alter_table('bill_shares') as batch:
        batch.drop_index('ix_bill_shares_user_id')
        batch.drop_index('ix_bill_shares_bill_id')
    op.drop_table('bill_shares')
    with op.batch_alter_table('bills') as batch:
        batch.drop_index('ix_bills_owner_id')
        batch.alter_column('owner_id', new_column_name='user_id')
        batch.drop_column('split_mode')
    op.create_index('ix_bills_user_id', 'bills', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_bills_user_id', 'bills')
    with op.batch_alter_table('bills') as batch:
        batch.alter_column('user_id', new_column_name='owner_id')
        batch.add_column(
            sa.Column('split_mode', sa.String(length=10), nullable=False, server_default='equal')
        )
    op.create_index('ix_bills_owner_id', 'bills', ['owner_id'], unique=False)
    op.create_table(
        'bill_shares',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('bill_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('split_value', sa.Integer(), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=True),
        sa.Column('category_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['bill_id'], ['bills.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('bill_id', 'user_id'),
    )
    with op.batch_alter_table('bill_shares') as batch:
        batch.create_index('ix_bill_shares_bill_id', ['bill_id'], unique=False)
        batch.create_index('ix_bill_shares_user_id', ['user_id'], unique=False)
    # Each bill was its owner's alone: one share, as a bill nobody shared had.
    op.execute(
        'INSERT INTO bill_shares (bill_id, user_id, split_value, account_id, category_id)'
        ' SELECT id, owner_id, 0, account_id, category_id FROM bills'
    )
    with op.batch_alter_table('bills') as batch:
        batch.drop_constraint('fk_bills_category_id', type_='foreignkey')
        batch.drop_constraint('fk_bills_account_id', type_='foreignkey')
        batch.drop_column('category_id')
        batch.drop_column('account_id')
