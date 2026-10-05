"""payslip payday

A payslip can record one payday of a pay schedule, and then stands in for
that payday's expected pay. Existing payslips stay one-offs.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-05 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('payslips', schema=None) as batch_op:
        batch_op.add_column(sa.Column('schedule_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('payday', sa.Date(), nullable=True))
        batch_op.create_foreign_key(
            'fk_payslips_schedule_id_pay_schedules',
            'pay_schedules',
            ['schedule_id'],
            ['id'],
            ondelete='SET NULL',
        )
        batch_op.create_index('ix_payslips_schedule_id', ['schedule_id'], unique=False)
        batch_op.create_unique_constraint('uq_payslips_schedule_id', ['schedule_id', 'payday'])


def downgrade() -> None:
    with op.batch_alter_table('payslips', schema=None) as batch_op:
        batch_op.drop_constraint('uq_payslips_schedule_id', type_='unique')
        batch_op.drop_index('ix_payslips_schedule_id')
        batch_op.drop_constraint('fk_payslips_schedule_id_pay_schedules', type_='foreignkey')
        batch_op.drop_column('payday')
        batch_op.drop_column('schedule_id')
