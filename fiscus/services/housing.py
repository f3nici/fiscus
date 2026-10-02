"""Housing line items. Each becomes a budget amount and a recurring bill.

The budget for any category that holds housing items is the monthly total of
this person's share of those items, recalculated whenever an item changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fiscus.clock import Clock, utcnow_naive
from fiscus.domain import recurrence
from fiscus.domain.housing import Diff, ItemSpec
from fiscus.models import Bill, BillShare, Budget, Category, HousingItem, User
from fiscus.services.access import owned


class HousingError(ValueError):
    pass


def items(db: Session, user: User) -> list[HousingItem]:
    return list(db.execute(owned(HousingItem, user).order_by(HousingItem.name)).scalars())


def as_spec(db: Session, item: HousingItem) -> ItemSpec:
    cat = db.get(Category, item.category_id)
    other = db.get(User, item.shared_with_id) if item.shared_with_id else None
    return ItemSpec(
        name=item.name,
        amount_cents=item.amount_cents,
        frequency=item.frequency,
        category=cat.name if cat else '',
        due_date=item.due_date,
        shared_with=other.username if other else '',
        split_bp=item.shared_split_bp if other else 0,
    )


def category_named(db: Session, user: User, name: str) -> Category:
    name = name.strip() or 'Housing'
    cat = db.execute(
        owned(Category, user).where(func.lower(Category.name) == name.lower())
    ).scalar_one_or_none()
    if cat is None:
        cat = Category(user_id=user.id, name=name[:80])
        db.add(cat)
        db.flush()
    return cat


def resolve_partner(db: Session, user: User, username: str) -> User | None:
    if not username:
        return None
    other = db.execute(
        select(User).where(User.username == username.strip().lower())
    ).scalar_one_or_none()
    if other is None or not other.is_active:
        raise HousingError(f'No active user called "{username}" to share with')
    if other.id == user.id:
        raise HousingError('You cannot share an item with yourself')
    return other


def my_share(item: HousingItem) -> int:
    if item.shared_with_id:
        return item.amount_cents * (10000 - item.shared_split_bp) // 10000
    return item.amount_cents


def default_due(today: date) -> date:
    return recurrence.add_months(today.replace(day=1), 1)


def upsert(
    db: Session,
    clock: Clock,
    user: User,
    spec: ItemSpec,
    account_id: int | None = None,
    existing: HousingItem | None = None,
) -> HousingItem:
    partner = resolve_partner(db, user, spec.shared_with)
    cat = category_named(db, user, spec.category)
    old_cat = existing.category_id if existing else None
    item = existing or HousingItem(user_id=user.id, name=spec.name)
    item.name = spec.name
    item.amount_cents = spec.amount_cents
    item.frequency = spec.frequency
    item.category_id = cat.id
    item.due_date = spec.due_date
    item.shared_with_id = partner.id if partner else None
    item.shared_split_bp = spec.split_bp if partner else 0
    if account_id is not None or existing is None:
        item.account_id = account_id
    if existing is None:
        db.add(item)
    db.flush()
    _sync_bill(db, clock, user, item)
    recompute_budget(db, user, cat.id)
    if old_cat and old_cat != cat.id:
        recompute_budget(db, user, old_cat)
    return item


def remove(db: Session, user: User, item: HousingItem) -> None:
    bill = _bill_for(db, item)
    if bill is not None:
        bill.active = False
        bill.housing_item_id = None
    cat_id = item.category_id
    db.delete(item)
    db.flush()
    recompute_budget(db, user, cat_id)


def _bill_for(db: Session, item: HousingItem) -> Bill | None:
    return db.execute(select(Bill).where(Bill.housing_item_id == item.id)).scalar_one_or_none()


def _sync_bill(db: Session, clock: Clock, user: User, item: HousingItem) -> Bill:
    bill = _bill_for(db, item)
    if bill is None:
        bill = Bill(
            owner_id=user.id,
            housing_item_id=item.id,
            created_at=utcnow_naive(clock),
            name=item.name,
            amount_cents=item.amount_cents,
            frequency=item.frequency,
            next_due=item.due_date or default_due(clock.today()),
        )
        db.add(bill)
    bill.name = item.name
    bill.amount_cents = item.amount_cents
    bill.frequency = item.frequency
    bill.next_due = item.due_date or bill.next_due or default_due(clock.today())
    bill.active = True
    bill.split_mode = 'percent' if item.shared_with_id else 'equal'
    db.flush()
    wanted = {user.id: 10000 - item.shared_split_bp if item.shared_with_id else 0}
    if item.shared_with_id:
        wanted[item.shared_with_id] = item.shared_split_bp
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
        if uid == user.id:
            found.account_id = item.account_id
            found.category_id = item.category_id
    db.flush()
    return bill


def recompute_budget(db: Session, user: User, category_id: int) -> None:
    total = sum(
        recurrence.monthly_equivalent(my_share(i), i.frequency)
        for i in db.execute(
            owned(HousingItem, user).where(HousingItem.category_id == category_id)
        ).scalars()
    )
    budget = db.execute(
        owned(Budget, user).where(Budget.category_id == category_id)
    ).scalar_one_or_none()
    if total == 0:
        if budget is not None:
            db.delete(budget)
        return
    if budget is None:
        db.add(Budget(user_id=user.id, category_id=category_id, monthly_cents=total))
    else:
        budget.monthly_cents = total


@dataclass
class ApplyResult:
    added: int
    changed: int
    removed: int


def apply_diff(
    db: Session, clock: Clock, user: User, diff: Diff, *, remove_missing: bool
) -> ApplyResult:
    by_name = {i.name.casefold(): i for i in items(db, user)}
    for spec in diff.added:
        upsert(db, clock, user, spec)
    for _old, spec in diff.changed:
        upsert(db, clock, user, spec, existing=by_name[spec.name.casefold()])
    removed = 0
    if remove_missing:
        for spec in diff.removed:
            remove(db, user, by_name[spec.name.casefold()])
            removed += 1
    return ApplyResult(len(diff.added), len(diff.changed), removed)
