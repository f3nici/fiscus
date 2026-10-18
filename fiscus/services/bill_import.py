"""Bills from a file: what you own now as specs, and applying a reviewed diff."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from fiscus.clock import Clock, utcnow_naive
from fiscus.domain import recurrence
from fiscus.domain.bill_import import BillSpec, Diff
from fiscus.models import Bill, Category, User
from fiscus.services import budgeting
from fiscus.services.bills import user_bills
from fiscus.services.ledger import category_named


def as_spec(db: Session, bill: Bill) -> BillSpec:
    cat = db.get(Category, bill.category_id) if bill.category_id else None
    return BillSpec(
        name=bill.name,
        amount_cents=bill.amount_cents,
        frequency=bill.frequency,
        category=cat.name if cat else '',
        due_date=bill.next_due,
    )


def upsert(
    db: Session, clock: Clock, user: User, spec: BillSpec, existing: Bill | None = None
) -> Bill:
    bill = existing
    if bill is None:
        due = spec.due_date or recurrence.add_months(clock.today().replace(day=1), 1)
        bill = Bill(
            user_id=user.id,
            created_at=utcnow_naive(clock),
            name=spec.name,
            amount_cents=spec.amount_cents,
            frequency=spec.frequency,
            next_due=due,
        )
        db.add(bill)
    bill.name = spec.name
    bill.amount_cents = spec.amount_cents
    bill.frequency = spec.frequency
    if spec.due_date is not None:
        bill.next_due = spec.due_date
    bill.active = True
    if spec.category is not None:
        cat = category_named(db, user, spec.category) if spec.category else None
        bill.category_id = cat.id if cat else None
    db.flush()
    return bill


@dataclass
class ApplyResult:
    added: int
    changed: int
    removed: int


def apply_diff(
    db: Session, clock: Clock, user: User, diff: Diff, *, remove_missing: bool
) -> ApplyResult:
    by_name = {b.name.casefold(): b for b in user_bills(db, user)}
    for spec in diff.added:
        upsert(db, clock, user, spec)
    for _old, spec in diff.changed:
        upsert(db, clock, user, spec, by_name[spec.key()])
    removed = 0
    if remove_missing:
        # Stopped, not deleted: their payment history stays.
        for spec in diff.removed:
            by_name[spec.key()].active = False
            removed += 1
    db.flush()
    budgeting.start_saving(db, user, clock.today())
    return ApplyResult(len(diff.added), len(diff.changed), removed)
