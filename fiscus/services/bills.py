"""Bills: shares, occurrences, status and matching.

Each participant (the owner included) sees a bill at their own share. Paying is
per person: the owner's occurrence is paid by a transaction for the whole
bill (they pay the biller), a participant's by a transaction for their share
(they pay the owner), or either by marking it paid by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from fiscus.clock import Clock, utcnow_naive
from fiscus.domain import recurrence, splits
from fiscus.domain.matching import Candidate, OpenOccurrence, assign
from fiscus.models import Bill, BillPayment, BillShare, Transaction, User
from fiscus.services import ledger

DUE_SOON_DAYS = 7
# Unpaid this far back is still owed: the forecast pays it tomorrow, and the
# budget's saved-up lines and the agenda count it.
OVERDUE_CARRY_DAYS = 62


def share_amounts(bill: Bill) -> dict[int, int]:
    specs = [splits.ShareSpec(s.user_id, s.split_value) for s in bill.shares]
    return splits.compute(bill.amount_cents, bill.split_mode, specs, bill.owner_id)


def expected_out(bill: Bill, user_id: int) -> int:
    """What leaves this person's account for one occurrence."""
    return bill.amount_cents if user_id == bill.owner_id else share_amounts(bill)[user_id]


@dataclass
class Occurrence:
    bill: Bill
    share: BillShare
    due: date
    share_cents: int
    status: str  # paid | overdue | due_soon | upcoming
    payment: BillPayment | None

    @property
    def shared(self) -> bool:
        return len(self.bill.shares) > 1

    @property
    def is_owner(self) -> bool:
        return self.share.user_id == self.bill.owner_id


def user_shares(db: Session, user: User, *, active_only: bool = True) -> list[BillShare]:
    q = select(BillShare).join(Bill).where(BillShare.user_id == user.id)
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
    shares = user_shares(db, user)
    if not shares:
        return []
    bill_ids = [s.bill_id for s in shares]
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
    for share in shares:
        bill = share.bill
        amounts = share_amounts(bill)
        for due in recurrence.occurrences(
            bill.next_due, bill.frequency, start, end, until=bill.end_date
        ):
            pay = payments.get((bill.id, due))
            out.append(
                Occurrence(bill, share, due, amounts[user.id], _status(due, today, pay), pay)
            )
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


def auto_match(db: Session, clock: Clock, user: User, txns: list[Transaction]) -> int:
    """Mark this user's open occurrences paid from newly imported transactions."""
    outgoing = [t for t in txns if t.amount_cents < 0]
    if not outgoing:
        return 0
    lo = min(t.date for t in outgoing)
    hi = max(t.date for t in outgoing)
    shares = user_shares(db, user)
    widest = max((s.bill.match_window_days for s in shares), default=0)
    open_occ: list[OpenOccurrence] = []
    for o in occurrences(
        db, user, lo - timedelta(days=widest), hi + timedelta(days=widest), clock.today()
    ):
        if o.payment is not None:
            continue
        open_occ.append(
            OpenOccurrence(
                bill_id=o.bill.id,
                due_date=o.due,
                expected_cents=expected_out(o.bill, user.id),
                account_id=o.share.account_id,
                tolerance_pct=o.bill.match_tolerance_pct,
                window_days=o.bill.match_window_days,
                hint=o.bill.description_hint,
            )
        )
    already = set(
        db.execute(
            select(BillPayment.transaction_id).where(
                BillPayment.transaction_id.in_([t.id for t in outgoing])
            )
        ).scalars()
    )
    candidates = [
        Candidate(t.id, t.account_id, t.date, t.amount_cents, t.description)
        for t in outgoing
        if t.id not in already
    ]
    now = utcnow_naive(clock)
    pairs = assign(open_occ, candidates)
    for occ, txn in pairs:
        db.add(
            BillPayment(
                bill_id=occ.bill_id,
                user_id=user.id,
                due_date=occ.due_date,
                paid_on=txn.date,
                transaction_id=txn.transaction_id,
                created_at=now,
            )
        )
    db.flush()
    return len(pairs)


def mark_paid(db: Session, clock: Clock, user: User, bill: Bill, due: date) -> BillPayment:
    """Mark one of this person's occurrences paid by hand, today. Their share
    comes off their account for it today (D42), as the forecast had it."""
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
    share = next(s for s in bill.shares if s.user_id == user.id)
    ledger.follow(
        db,
        clock,
        user,
        pay,
        account_id=share.account_id,
        day=pay.paid_on,
        cents=-share_amounts(bill)[user.id],
        description=bill.name,
        category_id=share.category_id,
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
