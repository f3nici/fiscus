"""payslip account

A payslip dated after today is expected income, paid into this account.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-02 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('payslips', schema=None) as batch_op:
        batch_op.add_column(sa.Column('account_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_payslips_account_id', 'accounts', ['account_id'], ['id'], ondelete='SET NULL'
        )


def downgrade() -> None:
    with op.batch_alter_table('payslips', schema=None) as batch_op:
        batch_op.drop_constraint('fk_payslips_account_id', type_='foreignkey')
        batch_op.drop_column('account_id')
