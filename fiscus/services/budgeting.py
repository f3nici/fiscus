"""The budget: what is set aside for each category in a budget month, and what is left.

A category's budget is what the person allocates to it, and nothing else.
Their bills in it (each share at its monthly equivalent, a one-off in the
period it falls due) are shown beside it but never add to it: a bill is paid
out of the allocation.

Spending is not read from bank transactions. It is what the person takes off a
budget by hand (`BudgetSpend`) plus their share of every bill they mark paid
that falls due in the period. The budget month starts on the day they choose.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fiscus.domain import recurrence
from fiscus.models import Budget, BudgetSpend, Category, User
from fiscus.services import bills, ledger, pay
from fiscus.services.access import owned
from fiscus.services.users import settings_for


@dataclass(frozen=True)
class BudgetRow:
    category: Category
    budget: Budget | None
    spent_cents: int  # taken off by hand (earned, for income)
    bills_cents: int  # this person's bills in the category, per month (shown, not added)
    bills_paid_cents: int  # their share of bills marked paid this period

    @property
    def actual_cents(self) -> int:
        return self.spent_cents + self.bills_paid_cents

    @property
    def budget_cents(self) -> int:
        return self.budget.monthly_cents if self.budget else 0

    @property
    def has_budget(self) -> bool:
        return self.budget is not None

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


@dataclass(frozen=True)
class Summary:
    expected_cents: int  # pay due in the period plus income budgets
    pay_cents: int
    budget_cents: int  # spending budgets, bills paid out of them
    spent_cents: int
    to_spend_cents: int  # what is left in budgets not yet overspent
    over: int

    @property
    def unbudgeted_cents(self) -> int:
        return self.expected_cents - self.budget_cents


def start_day(db: Session, user: User) -> int:
    return settings_for(db, user).budget_start_day


def period(db: Session, user: User, day: date) -> tuple[date, date]:
    """The budget month holding `day`."""
    return recurrence.month_period(day, start_day(db, user))


def period_starting_in(db: Session, user: User, month: date) -> tuple[date, date]:
    """The budget month that starts in the calendar month of `month`."""
    return period(db, user, recurrence.add_months(month, 0, start_day(db, user)))


def bills_by_category(
    db: Session, user: User, start: date, end: date, *, one_offs: bool = True
) -> dict[int, int]:
    """This person's bills per category for the period, smoothed to a monthly amount."""
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
            monthly = mine if one_offs and start <= bill.next_due <= end else 0
        else:
            monthly = recurrence.monthly_equivalent(mine, bill.frequency)
        if monthly:
            out[share.category_id] = out.get(share.category_id, 0) + monthly
    return out


def bills_paid_by_category(
    db: Session, user: User, start: date, end: date, today: date
) -> dict[int, int]:
    """This person's share of bills due in the period and marked paid."""
    out: dict[int, int] = {}
    for occ in bills.occurrences(db, user, start, end, today):
        if occ.payment is None or occ.share.category_id is None:
            continue
        cid = occ.share.category_id
        out[cid] = out.get(cid, 0) + occ.share_cents
    return out


def spent_by_category(db: Session, user: User, start: date, end: date) -> dict[int, int]:
    rows = db.execute(
        select(BudgetSpend.category_id, func.sum(BudgetSpend.amount_cents))
        .where(BudgetSpend.user_id == user.id, BudgetSpend.date >= start, BudgetSpend.date <= end)
        .group_by(BudgetSpend.category_id)
    )
    return {cid: int(total) for cid, total in rows}


def spends(db: Session, user: User, start: date, end: date) -> list[BudgetSpend]:
    return list(
        db.execute(
            owned(BudgetSpend, user)
            .where(BudgetSpend.date >= start, BudgetSpend.date <= end)
            .order_by(BudgetSpend.date.desc(), BudgetSpend.id.desc())
        ).scalars()
    )


def set_total(
    db: Session,
    user: User,
    category: Category,
    total_cents: int,
    day: date,
    *,
    use_in_forecast: bool,
    add: bool = False,
) -> tuple[int, int]:
    """Set a category's budget, the bills in it paid out of it. Returns (total, bills).

    With `add`, the amount goes on top of what is allocated already.
    """
    start, end = period(db, user, day)
    from_bills = bills_by_category(db, user, start, end).get(category.id, 0)
    row = db.execute(
        owned(Budget, user).where(Budget.category_id == category.id)
    ).scalar_one_or_none()
    total = max(0, (row.monthly_cents if row else 0) + total_cents if add else total_cents)
    keep = use_in_forecast or (row is not None and row.use_in_forecast and add)
    if total == 0 and not keep:
        if row is not None:
            db.delete(row)
        return 0, from_bills
    if row is None:
        row = Budget(user_id=user.id, category_id=category.id, monthly_cents=total)
        db.add(row)
    row.monthly_cents = total
    row.use_in_forecast = keep
    db.flush()
    return total, from_bills


def rows(db: Session, user: User, start: date, end: date, today: date) -> list[BudgetRow]:
    spent = spent_by_category(db, user, start, end)
    paid = bills_paid_by_category(db, user, start, end, today)
    budgets = {b.category_id: b for b in db.execute(owned(Budget, user)).scalars()}
    from_bills = bills_by_category(db, user, start, end)
    out = []
    for cat in ledger.categories(db, user):
        if cat.is_transfer:
            continue
        cid = cat.id
        if cid in budgets or spent.get(cid) or paid.get(cid) or from_bills.get(cid):
            out.append(
                BudgetRow(
                    cat,
                    budgets.get(cid),
                    spent.get(cid, 0),
                    from_bills.get(cid, 0),
                    paid.get(cid, 0),
                )
            )
    out.sort(key=lambda r: (r.category.is_income, -r.budget_cents, r.category.name))
    return out


def expected_pay(db: Session, user: User, start: date, end: date, today: date) -> int:
    """Net pay in the period: every payday (its payslip's net once recorded), and
    one-off payslips still to come. Past one-offs are left out: before paydays
    were recorded, a past payslip was usually a regular pay already counted."""
    return sum(
        i.net_cents
        for i in pay.items(db, user, start, end, today)
        if i.schedule is not None or i.when > today
    )


def summary(rs: list[BudgetRow], pay_cents: int) -> Summary:
    spend = [r for r in rs if not r.category.is_income]
    income = sum(r.budget_cents for r in rs if r.category.is_income)
    return Summary(
        expected_cents=pay_cents + income,
        pay_cents=pay_cents,
        budget_cents=sum(r.budget_cents for r in spend),
        spent_cents=sum(r.actual_cents for r in spend),
        to_spend_cents=sum(max(0, r.remaining_cents) for r in spend if r.has_budget),
        over=sum(1 for r in spend if r.state == 'over'),
    )
