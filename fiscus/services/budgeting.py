"""The budget: what is set aside for each category in a budget month, and what is left.

A spending category's budget is its bills (each at its monthly equivalent,
a one-off in the budget month it falls due) plus what the person budgets on
top of them, an estimate of everything else (D64). Bills are known amounts,
so they set the budget themselves: a line can never be budgeted below them,
and a dearer bill raises it. Only the estimate is stored. Every spending
category holding a bill has a line (`follow_bills`).

Spending is not read from bank transactions. It is what the person takes off a
budget by hand (`BudgetSpend`) plus every bill they mark paid
that falls due in the period. The budget month starts on the day they choose.
What is taken off by hand also comes off the account it was paid from (D49).

A line spent every few months (rego, insurance, a yearly subscription) is
saved up: its monthly share builds up from when it was set, and what is spent
comes out of that, so paying it is not one month far over and the rest under.
A monthly line holding a bill paid less often (a phone plan and its yearly
device cover) is saved up too, so what it does not spend waits for that bill.
"""

from __future__ import annotations

from collections.abc import Callable
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
    bills_cents: int  # this person's bills in the category, per month (in its budget)
    bills_paid_cents: int  # bills marked paid this period
    # Saved up and not yet spent, to the end of the period, for a line spent
    # every few months. None for a monthly line.
    saved_cents: int | None = None
    bills_yearly_cents: int = 0  # its recurring bills over a year

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
    def estimate_cents(self) -> int:
        """What is budgeted on top of its bills, a month."""
        return self.budget.monthly_cents if self.budget else 0

    @property
    def estimate_total_cents(self) -> int:
        """What is budgeted on top of its bills for one cycle, as typed."""
        return cycle_total(self.budget) if self.budget else 0

    @property
    def budget_cents(self) -> int:
        """Its bills and the estimate on top (D64). Income has no bills."""
        if self.budget is None:
            return 0
        if self.category.is_income:
            return self.budget.monthly_cents
        return self.budget.monthly_cents + self.bills_cents

    @property
    def cycle_total_cents(self) -> int:
        """The budget for one cycle: a yearly line's bills for a year and the
        estimate on top."""
        if self.budget is None:
            return 0
        bills_part = round_div(self.bills_yearly_cents * self.cycle_months, 12)
        return cycle_total(self.budget) + bills_part

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
    budget_cents: int  # spending budgets, their bills and the estimates on top
    spent_cents: int
    to_spend_cents: int  # what is left in budgets not yet overspent
    over: int
    saved_cents: int = 0  # the part of to_spend held by saved-up lines
    bills_cents: int = 0  # the part of budget_cents that is bills

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


def _running(bill: Bill, start: date) -> bool:
    """Not ended before `start`. A recurring bill's next due date is not when
    it starts: it is running."""
    return bill.end_date is None or bill.end_date >= start


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


def bill_cycles(db: Session, user: User, start: date | None = None) -> dict[int, int]:
    """The longest cycle, in months, of this person's running bills per
    category, for those with a bill paid less often than monthly. With
    `start`, a bill that ended before it is left out."""
    out: dict[int, int] = {}
    for bill in bills.user_bills(db, user):
        months = BILL_CYCLES.get(bill.frequency, 1)
        if bill.category_id is None or months == 1:
            continue
        if start is not None and bill.end_date is not None and bill.end_date < start:
            continue
        out[bill.category_id] = max(out.get(bill.category_id, 1), months)
    return out


def bills_yearly(db: Session, user: User, start: date) -> dict[int, int]:
    """This person's recurring bills per category over a year, exactly (52 of a
    weekly bill), leaving out those ended before `start`."""
    out: dict[int, int] = {}
    for bill in bills.user_bills(db, user):
        if bill.category_id is None or bill.frequency == 'one_off' or not _running(bill, start):
            continue
        yearly = bill.amount_cents * recurrence.PER_YEAR[bill.frequency]
        out[bill.category_id] = out.get(bill.category_id, 0) + yearly
    return out


def order_amounts(db: Session, user: User) -> dict[int, int]:
    """Each line's budget over a year, its bills and the estimate on top, for
    putting lines never moved by hand in order (D35)."""
    yearly = bills_yearly(db, user, date.min)
    return {
        b.category_id: round_div(cycle_total(b) * 12, b.cycle_months) + yearly.get(b.category_id, 0)
        for b in db.execute(owned(Budget, user)).scalars()
    }


def saves_up(row: Budget, category: Category, cycles: dict[int, int]) -> bool:
    """Whether a line is saved up: spent every few months, or a monthly
    spending line holding a bill paid less often (`bill_cycles`), so the
    bill's share builds up rather than going each month (D63)."""
    if category.is_income:
        return False
    return row.cycle_months > 1 or category.id in cycles


def follow_bills(db: Session, user: User, day: date) -> None:
    """Every spending category holding a bill has a budget line, its bills in it
    and nothing on top until the person budgets more (D64). A monthly line that
    has come to hold a bill paid less often saves up from the budget month
    holding `day`, from what it has left then (D63); one already saving
    carries on as it is."""
    start, _end = period(db, user, day)
    cycles = bill_cycles(db, user, start)
    held = {
        b.category_id
        for b in bills.user_bills(db, user)
        if b.category_id is not None
        and _running(b, start)
        and (b.frequency != 'one_off' or b.next_due >= start)
    }
    if not held:
        return
    lines = {
        b.category_id: b
        for b in db.execute(owned(Budget, user).where(Budget.category_id.in_(held))).scalars()
    }
    for cat in db.execute(owned(Category, user).where(Category.id.in_(held))).scalars():
        if cat.is_income or cat.is_transfer:
            continue
        row = lines.get(cat.id)
        if row is None:
            row = Budget(
                user_id=user.id,
                category_id=cat.id,
                monthly_cents=0,
                total_cents=0,
                use_in_forecast=False,
                cycle_months=1,
                saved_opening_cents=0,
            )
            db.add(row)
        if row.cycle_months == 1 and row.saved_since is None and cat.id in cycles:
            row.saved_since = start
            row.saved_opening_cents = 0
    db.flush()


def settle(db: Session, user: User, day: date, category_ids: set[int] | None = None) -> None:
    """Before bills change: carry each saved-up line (in `category_ids`, or all)
    forward to the budget month holding `day` at its bills as they were, so the
    change counts from this budget month on, not in months gone (D64)."""
    start, _end = period(db, user, day)
    cycles = bill_cycles(db, user, start)
    q = owned(Budget, user).where(Budget.saved_since < start)
    if category_ids is not None:
        q = q.where(Budget.category_id.in_(category_ids))
    for row in db.execute(q).scalars():
        cat = db.get(Category, row.category_id)
        if cat is None or not saves_up(row, cat, cycles):
            continue
        row.saved_opening_cents = saved_to(db, user, row, start - timedelta(days=1))
        row.saved_since = start
    db.flush()


def follow_paid(db: Session, user: User, bill: Bill, day: date) -> list[int]:
    """A bill whose amount varies expects the average of its last three paid
    (D66), so its line's budget and the forecast use it. Its saved-up line
    keeps what the old amount gave it, so the new one counts from the budget
    month holding `day` (D64). Call it once a payment is in or out. Returns
    the amounts averaged; none paid leaves the amount as it is."""
    if not bill.varies:
        return []
    paid = bills.last_paid(db, user, bill)
    if not paid:
        return paid
    average = round_div(sum(paid), len(paid))
    if average != bill.amount_cents:
        if bill.category_id is not None:
            settle(db, user, day, {bill.category_id})
        bill.amount_cents = average
        db.flush()
    return paid


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


def spent_from(db: Session, user: User, start: date, end: date) -> dict[int, Account]:
    """The account each spend in [start, end] came off (D49), by its id. A spend
    from before that, or from no account, is not in it."""
    return {
        sid: acct
        for sid, acct in db.execute(
            select(Transaction.budget_spend_id, Account)
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
    whole: bool = False,
) -> tuple[int, int]:
    """Set what a category budgets on top of its bills (D64). Returns the
    monthly (estimate, bills).

    The amount is for `cycle_months` (the line's own cycle when None): kept as
    typed, and as a monthly amount. With `whole` it is the line's whole budget,
    its bills in it, so only the rest is on top. With `add`, it goes on top of
    what is budgeted already. A saved-up line (`saves_up`) keeps what it has
    saved when it changes, or holds `saved_cents` this budget month (`hold`).
    A line with no bills and nothing on top is removed.
    """
    start, end = period(db, user, day)
    from_bills = 0
    yearly = 0
    if not category.is_income:
        from_bills = bills_by_category(db, user, start, end).get(category.id, 0)
        yearly = bills_yearly(db, user, start).get(category.id, 0)
    row = db.execute(
        owned(Budget, user).where(Budget.category_id == category.id)
    ).scalar_one_or_none()
    cycle = cycle_months or (row.cycle_months if row else 1)
    total = total_cents
    if add and row is not None:
        # What is there already, over the new cycle, plus the new amount.
        total += round_div(cycle_total(row) * cycle, row.cycle_months)
    elif whole:
        total -= round_div(yearly * cycle, 12)
    total = max(0, total)
    monthly = round_div(total, cycle)
    keep = use_in_forecast or (row is not None and row.use_in_forecast and add)
    if monthly == 0 and not keep and cycle == 1 and not from_bills and not yearly:
        if row is not None:
            db.delete(row)
        return 0, from_bills
    saves = cycle > 1 or (not category.is_income and category.id in bill_cycles(db, user, start))
    if saves:
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
    if saves and saved_cents is not None:
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


def taken_in(
    db: Session,
    user: User,
    row: Budget,
    since: date,
    end: date,
    held: list[Bill] | None = None,
) -> int:
    """What a line takes in over the budget months from the one starting
    `since` to the one holding `end` (D64): a month's share of what is on top
    of its bills each month (a cycle takes exactly what was typed for it), its
    recurring bills' share for each month they run (a year of a yearly bill is
    exactly the bill), and its one-off bills due in those months."""
    first = start_day(db, user)
    months: list[date] = []
    p = since
    while p <= end:
        months.append(p)
        p = recurrence.add_months(p, 1, first)
    if not months:
        return 0
    total = round_div(cycle_total(row) * len(months), row.cycle_months)
    cat = db.get(Category, row.category_id)
    if cat is None or cat.is_income:
        return total
    for bill in held if held is not None else bills.user_bills(db, user):
        if bill.category_id != row.category_id:
            continue
        if bill.frequency == 'one_off':
            if months[0] <= bill.next_due < p:
                total += bill.amount_cents
            continue
        n = sum(1 for m in months if _running(bill, m))
        total += round_div(bill.amount_cents * recurrence.PER_YEAR[bill.frequency] * n, 12)
    return total


def saved_to(db: Session, user: User, row: Budget, end: date) -> int:
    """What a saved-up line holds at the end of `end`'s day: its opening amount,
    plus what it took in each budget month since it started (`taken_in`), less
    what was spent (by hand and bills paid) in that time."""
    since = row.saved_since
    if since is None or end < since:
        return row.saved_opening_cents
    cid = row.category_id
    spent = spent_by_category(db, user, since, end).get(cid, 0)
    paid = bills_paid_by_category(db, user, since, end).get(cid, 0)
    return row.saved_opening_cents + taken_in(db, user, row, since, end) - spent - paid


def rows(db: Session, user: User, start: date, end: date) -> list[BudgetRow]:
    spent = spent_by_category(db, user, start, end)
    paid = bills_paid_by_category(db, user, start, end)
    budgets = {b.category_id: b for b in db.execute(owned(Budget, user)).scalars()}
    from_bills = bills_by_category(db, user, start, end)
    yearly = bills_yearly(db, user, start)
    cycles = bill_cycles(db, user, start)
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
                and saves_up(b, cat, cycles)
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
                    yearly.get(cid, 0),
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
    held = bills.user_bills(db, user)
    following = recurrence.add_months(start, 1, first)

    def added(row: Budget) -> Callable[[int], int]:
        """What the line takes in over the next k budget months."""

        def k_months(k: int) -> int:
            if k <= 0:
                return 0
            last = recurrence.add_months(start, k + 1, first) - timedelta(days=1)
            return taken_in(db, user, row, following, last, held)

        return k_months

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
            added=added(r.budget),
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
        bills_cents=sum(r.bills_cents for r in spend if r.has_budget),
    )
