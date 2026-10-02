"""Monthly budget against actual spending.

A category's budget is this person's bills in it (each share at its monthly
equivalent, a one-off in the month it falls due) plus whatever else they set
aside for it. The bills are counted for them; only the rest is stored.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from fiscus.domain import recurrence
from fiscus.models import Budget, Category, User
from fiscus.services import bills, ledger
from fiscus.services.access import owned


@dataclass(frozen=True)
class BudgetRow:
    category: Category
    budget: Budget | None
    actual_cents: int  # positive: spent (or earned, for income)
    bills_cents: int  # this person's bills in the category, per month

    @property
    def extra_cents(self) -> int:
        return self.budget.monthly_cents if self.budget else 0

    @property
    def budget_cents(self) -> int:
        return self.bills_cents + self.extra_cents

    @property
    def has_budget(self) -> bool:
        return self.budget is not None or self.bills_cents > 0

    @property
    def remaining_cents(self) -> int:
        return self.budget_cents - self.actual_cents

    @property
    def pct(self) -> int:
        if not self.budget_cents:
            return 0
        return min(999, round(100 * self.actual_cents / self.budget_cents))

    @property
    def state(self) -> str:
        if not self.budget_cents:
            return 'none'
        if self.category.is_income:
            return 'ok'
        if self.actual_cents > self.budget_cents:
            return 'over'
        if self.pct >= 90:
            return 'near'
        return 'ok'


def month_bounds(month: date) -> tuple[date, date]:
    start = month.replace(day=1)
    return start, start.replace(day=calendar.monthrange(start.year, start.month)[1])


def bills_by_category(db: Session, user: User, month: date) -> dict[int, int]:
    """This person's bills per category for the month, smoothed to a monthly amount."""
    start, end = month_bounds(month)
    out: dict[int, int] = {}
    for share in bills.user_shares(db, user):
        bill = share.bill
        # A recurring bill's next due date is not when it starts: it is running.
        if share.category_id is None:
            continue
        if bill.end_date is not None and bill.end_date < start:
            continue
        mine = bills.share_amounts(bill)[user.id]
        if bill.frequency == 'one_off':
            monthly = mine if start <= bill.next_due <= end else 0
        else:
            monthly = recurrence.monthly_equivalent(mine, bill.frequency)
        if monthly:
            out[share.category_id] = out.get(share.category_id, 0) + monthly
    return out


def set_total(
    db: Session,
    user: User,
    category: Category,
    total_cents: int,
    month: date,
    *,
    use_in_forecast: bool,
    add: bool = False,
) -> tuple[int, int]:
    """Set a category's budget to a total, bills included. Returns (total, bills).

    Only the part above the bills is stored. With `add`, the amount goes on top
    of whatever is set aside already.
    """
    from_bills = bills_by_category(db, user, month).get(category.id, 0)
    row = db.execute(
        owned(Budget, user).where(Budget.category_id == category.id)
    ).scalar_one_or_none()
    extra = (row.monthly_cents if row else 0) + total_cents if add else total_cents - from_bills
    extra = max(0, extra)
    keep = use_in_forecast or (row is not None and row.use_in_forecast and add)
    if extra == 0 and not keep:
        if row is not None:
            db.delete(row)
        return from_bills, from_bills
    if row is None:
        row = Budget(user_id=user.id, category_id=category.id, monthly_cents=extra)
        db.add(row)
    row.monthly_cents = extra
    row.use_in_forecast = keep
    db.flush()
    return from_bills + extra, from_bills


def rows(db: Session, user: User, month: date, today: date) -> list[BudgetRow]:
    start, end = month_bounds(month)
    spend = ledger.spend_by_category(db, user, start, end)
    budgets = {b.category_id: b for b in db.execute(owned(Budget, user)).scalars()}
    from_bills = bills_by_category(db, user, month)
    out = []
    for cat in ledger.categories(db, user):
        if cat.is_transfer:
            continue
        net = spend.get(cat.id, 0)
        actual = net if cat.is_income else -net
        if cat.id in budgets or actual or from_bills.get(cat.id):
            out.append(BudgetRow(cat, budgets.get(cat.id), actual, from_bills.get(cat.id, 0)))
    out.sort(key=lambda r: (r.category.is_income, -r.budget_cents, r.category.name))
    return out


def totals(rs: list[BudgetRow]) -> dict[str, int]:
    spend = [r for r in rs if not r.category.is_income]
    return {
        'budget': sum(r.budget_cents for r in spend),
        'actual': sum(r.actual_cents for r in spend),
        'over': sum(1 for r in spend if r.state == 'over'),
    }
