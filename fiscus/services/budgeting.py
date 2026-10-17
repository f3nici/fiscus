"""The budget: what is set aside for each category in a budget month, and what is left.

A category's budget is what the person allocates to it, and nothing else.
Their bills in it (each at its monthly equivalent, a one-off in the period
it falls due) are shown beside it but never add to it: a bill is paid
out of the allocation.

Spending is not read from bank transactions. It is what the person takes off a
budget by hand (`BudgetSpend`) plus every bill they mark paid
that falls due in the period. The budget month starts on the day they choose.
What is taken off by hand also comes off the account it was paid from (D49).

A line spent every few months (rego, insurance, a yearly subscription) is
saved up: its monthly share builds up from when it was set, and what is spent
comes out of that, so paying it is not one month far over and the rest under.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fiscus.domain import recurrence, saving
from fiscus.models import (
    Account,
    Bill,
    BillPayment,
    Budget,
    BudgetSpend,
    Category,
    GoalMove,
    Transaction,
    User,
)
from fiscus.money import round_div
from fiscus.services import bills, ledger, pay
from fiscus.services.access import owned
from fiscus.services.users import settings_for

# How often a budget line is spent, in months.
CYCLES = {
    1: 'Every month',
    2: 'Every 2 months',
    3: 'Every 3 months',
    6: 'Every 6 months',
    12: 'Every year',
}
BILL_CYCLES = {'bimonthly': 2, 'quarterly': 3, 'yearly': 12}


@dataclass(frozen=True)
class BudgetRow:
    category: Category
    budget: Budget | None
    spent_cents: int  # taken off by hand (earned, for income)
    bills_cents: int  # this person's bills in the category, per month (shown, not added)
    bills_paid_cents: int  # bills marked paid this period
    # Saved up and not yet spent, to the end of the period, for a line spent
    # every few months. None for a monthly line.
    saved_cents: int | None = None

    @property
    def saving(self) -> bool:
        return self.saved_cents is not None

    @property
    def cycle_months(self) -> int:
        return self.budget.cycle_months if self.budget else 1

    @property
    def actual_cents(self) -> int:
        return self.spent_cents + self.bills_paid_cents

    @property
    def budget_cents(self) -> int:
        return self.budget.monthly_cents if self.budget else 0

    @property
    def cycle_total_cents(self) -> int:
        """The budget for one cycle, as typed: $2,480 for a yearly line."""
        return cycle_total(self.budget) if self.budget else 0

    @property
    def has_budget(self) -> bool:
        return self.budget is not None

    @property
    def remaining_cents(self) -> int:
        if self.saved_cents is not None:
            return self.saved_cents
        return self.budget_cents - self.actual_cents

    @property
    def pct(self) -> int:
        if not self.budget_cents:
            return 0
        return min(999, round_div(100 * self.actual_cents, self.budget_cents))

    @property
    def state(self) -> str:
        if not self.budget_cents:
            return 'none'
        if self.category.is_income:
            return 'ok'
        if self.saved_cents is not None:
            return 'over' if self.saved_cents < 0 else 'ok'
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
    saved_cents: int = 0  # the part of to_spend held by saved-up lines

    @property
    def unbudgeted_cents(self) -> int:
        return self.expected_cents - self.budget_cents


def cycle_total(row: Budget) -> int:
    """A line's amount for one cycle; older rows without one multiply out."""
    if row.total_cents is not None:
        return row.total_cents
    return row.monthly_cents * row.cycle_months


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
    for bill in bills.user_bills(db, user):
        # A recurring bill's next due date is not when it starts: it is running.
        if bill.category_id is None:
            continue
        if bill.end_date is not None and bill.end_date < start:
            continue
        if bill.frequency == 'one_off':
            monthly = bill.amount_cents if one_offs and start <= bill.next_due <= end else 0
        else:
            monthly = recurrence.monthly_equivalent(bill.amount_cents, bill.frequency)
        if monthly:
            out[bill.category_id] = out.get(bill.category_id, 0) + monthly
    return out


def bill_cycles(db: Session, user: User) -> dict[int, int]:
    """The longest cycle, in months, of this person's running bills per category."""
    out: dict[int, int] = {}
    for bill in bills.user_bills(db, user):
        months = BILL_CYCLES.get(bill.frequency, 1)
        if bill.category_id is not None and months > 1:
            out[bill.category_id] = max(out.get(bill.category_id, 1), months)
    return out


def bills_paid_by_category(db: Session, user: User, start: date, end: date) -> dict[int, int]:
    """What was paid for this person's bills due in the period, by the bill's
    category: each payment as it was marked paid, whatever the bill says now,
    stopped or not (D57)."""
    rows = db.execute(
        select(Bill.category_id, func.sum(BillPayment.amount_cents))
        .join(Bill, Bill.id == BillPayment.bill_id)
        .where(
            BillPayment.user_id == user.id,
            BillPayment.due_date >= start,
            BillPayment.due_date <= end,
            Bill.category_id.is_not(None),
        )
        .group_by(Bill.category_id)
    )
    return {cid: int(total) for cid, total in rows if cid is not None}


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


def spent_from(db: Session, user: User, start: date, end: date) -> dict[int, str]:
    """The account each spend in [start, end] came off (D49), by its id. A spend
    from before that, or from no account, is not in it."""
    return {
        sid: name
        for sid, name in db.execute(
            select(Transaction.budget_spend_id, Account.name)
            .join(Account, Account.id == Transaction.account_id)
            .join(BudgetSpend, BudgetSpend.id == Transaction.budget_spend_id)
            .where(
                Transaction.user_id == user.id,
                BudgetSpend.date >= start,
                BudgetSpend.date <= end,
            )
        )
        if sid is not None
    }


def set_total(
    db: Session,
    user: User,
    category: Category,
    total_cents: int,
    day: date,
    *,
    use_in_forecast: bool,
    add: bool = False,
    cycle_months: int | None = None,
    saved_cents: int | None = None,
) -> tuple[int, int]:
    """Set a category's budget, the bills in it paid out of it. Returns the
    monthly (total, bills).

    The total is for `cycle_months` (the line's own cycle when None): kept as
    typed, and as a monthly amount. With `add`, it goes on top of what is
    allocated already. A line spent every few months keeps what it has saved
    when it changes, or holds `saved_cents` this budget month (`hold`).
    """
    start, end = period(db, user, day)
    from_bills = bills_by_category(db, user, start, end).get(category.id, 0)
    row = db.execute(
        owned(Budget, user).where(Budget.category_id == category.id)
    ).scalar_one_or_none()
    cycle = cycle_months or (row.cycle_months if row else 1)
    total = total_cents
    if add and row is not None:
        # What is there already, over the new cycle, plus the new amount.
        total += round_div(cycle_total(row) * cycle, row.cycle_months)
    total = max(0, total)
    monthly = round_div(total, cycle)
    keep = use_in_forecast or (row is not None and row.use_in_forecast and add)
    if monthly == 0 and not keep and cycle == 1:
        if row is not None:
            db.delete(row)
        return 0, from_bills
    if cycle > 1:
        if row is not None and row.saved_since is not None and row.saved_since < start:
            # Carry on from what is saved up to this month, at the old amount.
            before = start - timedelta(days=1)
            opening = saved_to(db, user, row, before)
        elif row is not None and row.saved_since is not None:
            opening = row.saved_opening_cents
        else:
            opening = 0
        since: date | None = start
    else:
        opening, since = 0, None
    if row is None:
        row = Budget(user_id=user.id, category_id=category.id, monthly_cents=monthly)
        db.add(row)
    row.monthly_cents = monthly
    row.total_cents = total
    row.use_in_forecast = keep
    row.cycle_months = cycle
    row.saved_since = since
    row.saved_opening_cents = opening
    db.flush()
    if cycle > 1 and saved_cents is not None:
        hold(db, user, row, saved_cents, day)
    return monthly, from_bills


def hold(db: Session, user: User, row: Budget, saved_cents: int, day: date) -> None:
    """Make a saved-up line hold `saved_cents` in the budget month holding
    `day`, as the budget shows it (that month's share in, what came off it
    out), and save on from there."""
    start, end = period(db, user, day)
    row.saved_since = start
    row.saved_opening_cents = 0
    row.saved_opening_cents = saved_cents - saved_to(db, user, row, end)
    db.flush()


def saved_to(db: Session, user: User, row: Budget, end: date) -> int:
    """What a saved-up line holds at the end of `end`'s day: its opening amount,
    plus a month's budget for every budget month since it started, less what
    was spent (by hand and bills paid) in that time. A full cycle saves exactly
    the amount typed for it."""
    since = row.saved_since
    if since is None or end < since:
        return row.saved_opening_cents
    first = start_day(db, user)
    months, p = 0, since
    while p <= end:
        months += 1
        p = recurrence.add_months(p, 1, first)
    cid = row.category_id
    spent = spent_by_category(db, user, since, end).get(cid, 0)
    paid = bills_paid_by_category(db, user, since, end).get(cid, 0)
    saved = round_div(cycle_total(row) * months, row.cycle_months)
    return row.saved_opening_cents + saved - spent - paid


def rows(db: Session, user: User, start: date, end: date) -> list[BudgetRow]:
    spent = spent_by_category(db, user, start, end)
    paid = bills_paid_by_category(db, user, start, end)
    budgets = {b.category_id: b for b in db.execute(owned(Budget, user)).scalars()}
    from_bills = bills_by_category(db, user, start, end)
    out = []
    for cat in ledger.categories(db, user):
        if cat.is_transfer:
            continue
        cid = cat.id
        # Any entry taken off, even if they net to nothing: the line's Edit
        # panel is the only place to remove one.
        if cid in budgets or cid in spent or paid.get(cid) or from_bills.get(cid):
            b = budgets.get(cid)
            saved = None
            if (
                b is not None
                and b.cycle_months > 1
                and not cat.is_income
                and b.saved_since is not None
                and b.saved_since <= end
            ):
                saved = saved_to(db, user, b, end)
            out.append(
                BudgetRow(
                    cat,
                    b,
                    spent.get(cid, 0),
                    from_bills.get(cid, 0),
                    paid.get(cid, 0),
                    saved,
                )
            )
    out.sort(key=lambda r: (r.category.is_income, -r.budget_cents, r.category.name))
    return out


# How far back to look for a line's last bill, and how many budget months
# ahead to check its bills.
DUE_LOOKBACK = timedelta(days=400)
DUE_AHEAD_MONTHS = 12


def due_checks(
    db: Session, user: User, rs: list[BudgetRow], start: date, today: date
) -> dict[int, saving.Outlook]:
    """For each saved-up spending line with a bill still to pay: every budget
    month in the year from the one starting `start` that holds one of its
    bills, and whether the line will hold all that is due by then. Bills still
    unpaid from before it (as far back as the forecast carries them) come
    first, and bills marked paid before their budget month still count, since
    they have not come off the line yet. What was put aside counts from the
    last bill, even if the line was changed since: it went into the goal, not
    into what the line carried forward."""
    lines = {
        r.category.id: r
        for r in rs
        if r.saved_cents is not None and r.budget is not None and not r.category.is_income
    }
    if not lines:
        return {}
    first = start_day(db, user)
    _s, end = period(db, user, start)
    horizon = recurrence.add_months(start, DUE_AHEAD_MONTHS, first) - timedelta(days=1)
    owed_from = min(start, today - timedelta(days=bills.OVERDUE_CARRY_DAYS))
    occs = [
        o
        for o in bills.occurrences(db, user, start - DUE_LOOKBACK, horizon, today)
        if o.bill.category_id in lines
    ]
    out: dict[int, saving.Outlook] = {}
    for cid, r in lines.items():
        mine = [o for o in occs if o.bill.category_id == cid]
        unpaid = [o for o in mine if o.payment is None and o.due >= owed_from]
        if not unpaid or r.budget is None or r.saved_cents is None:
            continue
        before = [o.due for o in mine if o.due < unpaid[0].due]
        since = before[-1] + timedelta(days=1) if before else start - DUE_LOOKBACK
        ahead = [o for o in mine if o.payment is not None and o.due > end]
        found = saving.outlook(
            period_start=start,
            start_day=first,
            # Paid ahead: what was paid comes off the line (D57), not the bill's amount now.
            bills=[saving.Due(o.due, o.amount_cents) for o in unpaid]
            + [saving.Due(o.due, o.amount_cents, paid=True) for o in ahead],
            saved_cents=r.saved_cents,
            monthly_cents=r.budget.monthly_cents,
            put_aside_cents=put_aside(db, user, cid, since, today),
        )
        if found is not None:
            out[cid] = found
    return out


def put_aside(db: Session, user: User, category_id: int, start: date, end: date) -> int:
    """What was spent from a line into a goal in the period: out of the line,
    but kept for its bills."""
    total = db.execute(
        select(func.sum(BudgetSpend.amount_cents))
        .join(GoalMove, GoalMove.spend_id == BudgetSpend.id)
        .where(
            BudgetSpend.user_id == user.id,
            BudgetSpend.category_id == category_id,
            BudgetSpend.date >= start,
            BudgetSpend.date <= end,
        )
    ).scalar()
    return int(total or 0)


# Pay this far apart is spread over the budget months rather than landing in one.
SPREAD_PAY = ('quarterly',)


def spread_pay(db: Session, user: User, start: date, end: date, today: date) -> int:
    """Quarterly pay as a monthly amount (its expected net x 4 / 12), for every
    such schedule running in the period."""
    return sum(
        recurrence.monthly_equivalent(net, ps.frequency)
        for ps, net in pay.schedules(db, user, today)
        if ps.frequency in SPREAD_PAY
        and ps.anchor_date <= end
        and (ps.end_date is None or ps.end_date >= start)
    )


def expected_pay(db: Session, user: User, start: date, end: date, today: date) -> int:
    """Net pay in the period: every payday (its payslip's net once recorded), and
    one-off payslips still to come. Past one-offs are left out: before paydays
    were recorded, a past payslip was usually a regular pay already counted.
    Quarterly pay counts a month's share each month (`spread_pay`)."""
    return spread_pay(db, user, start, end, today) + sum(
        i.net_cents
        for i in pay.items(db, user, start, end, today)
        if (i.schedule is not None and i.schedule.frequency not in SPREAD_PAY)
        or (i.schedule is None and i.when > today)
    )


def summary(
    rs: list[BudgetRow],
    pay_cents: int,
    *,
    loan_cents: int = 0,
    loan_paid_cents: int = 0,
) -> Summary:
    """Totals for the month. Home loan repayments (`loans.budget_lines`) are
    always budgeted, and spent as each one falls due. Goals are not budgeted:
    what they hold only comes off the money free in accounts."""
    spend = [r for r in rs if not r.category.is_income]
    income = sum(r.budget_cents for r in rs if r.category.is_income)
    return Summary(
        expected_cents=pay_cents + income,
        pay_cents=pay_cents,
        budget_cents=sum(r.budget_cents for r in spend) + loan_cents,
        spent_cents=sum(r.actual_cents for r in spend) + loan_paid_cents,
        to_spend_cents=sum(max(0, r.remaining_cents) for r in spend if r.has_budget)
        + max(0, loan_cents - loan_paid_cents),
        over=sum(1 for r in spend if r.state == 'over'),
        saved_cents=sum(max(0, r.saved_cents or 0) for r in spend if r.has_budget),
    )
