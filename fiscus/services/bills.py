"""Bills: occurrences and their status.

An occurrence is paid by marking it paid by hand, which takes it off the
bill's account (D42).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from fiscus.clock import Clock, utcnow_naive
from fiscus.domain import recurrence
from fiscus.models import Bill, BillPayment, User
from fiscus.services import ledger
from fiscus.services.access import owned

DUE_SOON_DAYS = 7
# Unpaid this far back is still owed: the forecast pays it tomorrow, and the
# budget's saved-up lines and the agenda count it.
OVERDUE_CARRY_DAYS = 62


@dataclass
class Occurrence:
    bill: Bill
    due: date
    status: str  # paid | overdue | due_soon | upcoming
    payment: BillPayment | None


def user_bills(db: Session, user: User, *, active_only: bool = True) -> list[Bill]:
    q = owned(Bill, user)
    if active_only:
        q = q.where(Bill.active.is_(True))
    return list(db.execute(q.order_by(Bill.name)).scalars())


def _status(due: date, today: date, payment: BillPayment | None) -> str:
    if payment is not None:
        return 'paid'
    if due < today:
        return 'overdue'
    if due <= today + timedelta(days=DUE_SOON_DAYS):
        return 'due_soon'
    return 'upcoming'


def occurrences(db: Session, user: User, start: date, end: date, today: date) -> list[Occurrence]:
    found = user_bills(db, user)
    if not found:
        return []
    bill_ids = [b.id for b in found]
    payments = {
        (p.bill_id, p.due_date): p
        for p in db.execute(
            select(BillPayment).where(
                BillPayment.user_id == user.id,
                BillPayment.bill_id.in_(bill_ids),
                BillPayment.due_date >= start,
                BillPayment.due_date <= end,
            )
        ).scalars()
    }
    out: list[Occurrence] = []
    for bill in found:
        for due in recurrence.occurrences(
            bill.next_due, bill.frequency, start, end, until=bill.end_date
        ):
            pay = payments.get((bill.id, due))
            out.append(Occurrence(bill, due, _status(due, today, pay), pay))
    out.sort(key=lambda o: (o.due, o.bill.name))
    return out


def overdue(db: Session, user: User, today: date) -> list[Occurrence]:
    """Unpaid occurrences from the last OVERDUE_CARRY_DAYS: the one window the
    dashboard, bills list, agenda, reminders, budget and forecast share."""
    return [
        o
        for o in occurrences(db, user, today - timedelta(days=OVERDUE_CARRY_DAYS), today, today)
        if o.status == 'overdue'
    ]


def mark_paid(db: Session, clock: Clock, user: User, bill: Bill, due: date) -> BillPayment:
    """Mark an occurrence paid by hand, today. It comes off the bill's account
    today (D42), as the forecast had it."""
    existing = db.execute(
        select(BillPayment).where(
            BillPayment.bill_id == bill.id,
            BillPayment.user_id == user.id,
            BillPayment.due_date == due,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    pay = BillPayment(
        bill_id=bill.id,
        user_id=user.id,
        due_date=due,
        paid_on=clock.today(),
        created_at=utcnow_naive(clock),
    )
    db.add(pay)
    db.flush()
    ledger.follow(
        db,
        clock,
        user,
        pay,
        account_id=bill.account_id,
        day=pay.paid_on,
        cents=-bill.amount_cents,
        description=bill.name,
        category_id=bill.category_id,
    )
    return pay


def unmark(db: Session, user: User, bill: Bill, due: date) -> bool:
    existing = db.execute(
        select(BillPayment).where(
            BillPayment.bill_id == bill.id,
            BillPayment.user_id == user.id,
            BillPayment.due_date == due,
        )
    ).scalar_one_or_none()
    if existing is None:
        return False
    db.delete(existing)
    return True


def is_occurrence(bill: Bill, due: date) -> bool:
    return any(recurrence.occurrences(bill.next_due, bill.frequency, due, due, until=bill.end_date))
