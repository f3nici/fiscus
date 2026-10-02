"""Bills from a file: what you own now as specs, and applying a reviewed diff."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from fiscus.clock import Clock, utcnow_naive
from fiscus.domain import recurrence
from fiscus.domain.bill_import import BillSpec, Diff
from fiscus.models import Bill, BillShare, Category, User
from fiscus.services.ledger import category_named


class BillImportError(ValueError):
    pass


def owned_bills(db: Session, user: User) -> list[Bill]:
    return list(
        db.execute(
            select(Bill).where(Bill.owner_id == user.id, Bill.active.is_(True)).order_by(Bill.name)
        ).scalars()
    )


def as_spec(db: Session, bill: Bill) -> BillSpec:
    mine = next((s for s in bill.shares if s.user_id == bill.owner_id), None)
    cat = db.get(Category, mine.category_id) if mine and mine.category_id else None
    others = [s for s in bill.shares if s.user_id != bill.owner_id]
    shared_with, split_bp, keep = '', 0, False
    if len(others) == 1 and bill.split_mode in ('equal', 'percent'):
        other = db.get(User, others[0].user_id)
        shared_with = other.username if other else ''
        split_bp = 5000 if bill.split_mode == 'equal' else others[0].split_value
    elif others:
        keep = True
    return BillSpec(
        name=bill.name,
        amount_cents=bill.amount_cents,
        frequency=bill.frequency,
        category=cat.name if cat else '',
        due_date=bill.next_due,
        shared_with=shared_with,
        split_bp=split_bp,
        keep_shares=keep,
    )


def resolve_partner(db: Session, user: User, username: str) -> User | None:
    if not username:
        return None
    other = db.execute(
        select(User).where(User.username == username.strip().lower())
    ).scalar_one_or_none()
    if other is None or not other.is_active:
        raise BillImportError(f'No active user called "{username}" to share with')
    if other.id == user.id:
        raise BillImportError('You cannot share a bill with yourself')
    return other


def upsert(
    db: Session, clock: Clock, user: User, spec: BillSpec, existing: Bill | None = None
) -> Bill:
    partner = resolve_partner(db, user, spec.shared_with or '')
    bill = existing
    if bill is None:
        due = spec.due_date or recurrence.add_months(clock.today().replace(day=1), 1)
        bill = Bill(
            owner_id=user.id,
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
    # Shares stay as they are when the file does not mention sharing, or when
    # it cannot describe this bill's split and does not name anyone.
    keep = existing is not None and (
        spec.shared_with is None or (as_spec(db, existing).keep_shares and not partner)
    )
    if not keep:
        bill.split_mode = 'percent' if partner else 'equal'
    db.flush()
    if not keep:
        wanted = {user.id: 10000 - spec.split_bp if partner else 0}
        if partner:
            wanted[partner.id] = spec.split_bp
        for share in list(bill.shares):
            if share.user_id not in wanted:
                bill.shares.remove(share)
        have = {s.user_id: s for s in bill.shares}
        for uid, value in wanted.items():
            found = have.get(uid)
            if found is None:
                found = BillShare(user_id=uid)
                bill.shares.append(found)
            found.split_value = value
    if spec.category is not None:
        cat = category_named(db, user, spec.category) if spec.category else None
        mine = next(s for s in bill.shares if s.user_id == user.id)
        mine.category_id = cat.id if cat else None
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
    by_name = {b.name.casefold(): b for b in owned_bills(db, user)}
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
    return ApplyResult(len(diff.added), len(diff.changed), removed)
