"""Monthly budget against actual spending."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from fiscus.models import Budget, Category, User
from fiscus.services import bills, ledger
from fiscus.services.access import owned


@dataclass(frozen=True)
class BudgetRow:
    category: Category
    budget: Budget | None
    actual_cents: int  # positive: spent (or earned, for income)
    bills_cents: int  # this person's bill shares due this month in the category

    @property
    def budget_cents(self) -> int:
        return self.budget.monthly_cents if self.budget else 0

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


def rows(db: Session, user: User, month: date, today: date) -> list[BudgetRow]:
    start, end = month_bounds(month)
    spend = ledger.spend_by_category(db, user, start, end)
    budgets = {b.category_id: b for b in db.execute(owned(Budget, user)).scalars()}
    bill_totals: dict[int, int] = {}
    for occ in bills.occurrences(db, user, start, end, today):
        cid = occ.share.category_id
        if cid is not None:
            bill_totals[cid] = bill_totals.get(cid, 0) + occ.share_cents
    out = []
    for cat in ledger.categories(db, user):
        if cat.is_transfer:
            continue
        net = spend.get(cat.id, 0)
        actual = net if cat.is_income else -net
        if cat.id in budgets or actual or bill_totals.get(cat.id):
            out.append(BudgetRow(cat, budgets.get(cat.id), actual, bill_totals.get(cat.id, 0)))
    out.sort(key=lambda r: (r.category.is_income, -r.budget_cents, r.category.name))
    return out


def totals(rs: list[BudgetRow]) -> dict[str, int]:
    spend = [r for r in rs if not r.category.is_income]
    return {
        'budget': sum(r.budget_cents for r in spend),
        'actual': sum(r.actual_cents for r in spend),
        'over': sum(1 for r in spend if r.state == 'over'),
    }
