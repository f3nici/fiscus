"""Gathering forecast inputs from the database, and the per-user cache."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fiscus.clock import Clock, utcnow_naive
from fiscus.domain import recurrence
from fiscus.domain.forecast import (
    BudgetLine,
    Flow,
    ForecastInput,
    ForecastResult,
    apply_scenario,
    run,
)
from fiscus.domain.forecast import still_to_spend as _still_to_spend
from fiscus.domain.loan import LoanSpec, repayments
from fiscus.models import (
    Account,
    BillPayment,
    Budget,
    ForecastCache,
    Loan,
    RecurringTransfer,
    Scenario,
    Transaction,
    User,
)
from fiscus.money import round_div
from fiscus.services import bills, budgeting, goals, ledger, pay
from fiscus.services.access import owned
from fiscus.services.users import settings_for

AVERAGE_DAYS = 90
# 365.25 / 12 days in a month, scaled to whole numbers.
DAYS_PER_MONTH_X10000 = 304375


@dataclass(frozen=True)
class Estimate:
    category_id: int | None
    name: str
    monthly_cents: int  # negative is spending
    source: str  # average | budget


def horizon_days(today: date, months: int) -> int:
    months = min(24, max(1, months))
    return (recurrence.add_months(today, months) - today).days


def loan_spec(db: Session, loan: Loan) -> LoanSpec:
    offsets = (
        db.execute(
            select(Account.id).where(
                Account.loan_id == loan.id,
                Account.user_id == loan.user_id,
                Account.archived.is_(False),
            )
        )
        .scalars()
        .all()
    )
    rates = [(r.effective_date, Decimal(r.rate)) for r in loan.rates]
    if not rates:
        rates = [(loan.start_date, Decimal(0))]
    return LoanSpec(
        id=loan.id,
        name=loan.name,
        principal_cents=loan.principal_cents,
        start_date=loan.start_date,
        term_months=loan.term_months,
        frequency=loan.repayment_frequency,
        balance_cents=loan.balance_cents,
        balance_date=loan.balance_date,
        rates=rates,
        repayment_override_cents=loan.repayment_override_cents,
        first_repayment=loan.first_repayment_date,
        account_id=loan.repayment_account_id,
        offset_account_ids=tuple(offsets),
    )


def estimates(db: Session, user: User, today: date) -> list[Estimate]:
    """Discretionary spending per category: the budget, or a 3-month average without one."""
    start = today - timedelta(days=AVERAGE_DAYS)
    cats = {c.id: c for c in ledger.categories(db, user)}
    paid_txn_ids = set(
        db.execute(
            select(BillPayment.transaction_id).where(
                BillPayment.user_id == user.id, BillPayment.transaction_id.is_not(None)
            )
        ).scalars()
    )
    totals: dict[int | None, int] = defaultdict(int)
    for t in db.execute(
        owned(Transaction, user).where(Transaction.date > start, Transaction.date <= today)
    ).scalars():
        if (
            t.id in paid_txn_ids
            or t.bill_payment_id
            or t.payslip_id
            or t.budget_spend_id
            or t.dedup_hash.startswith(ledger.KEPT)
        ):
            # Bills and pay are forecast from their schedules, and spending
            # taken off a budget from the budget, not from their history.
            continue
        cat = cats.get(t.category_id) if t.category_id else None
        if cat is not None and cat.is_transfer:
            continue
        if cat is not None and cat.is_income and not cat.is_business:
            continue  # salary comes from pay schedules
        if cat is None and t.amount_cents > 0:
            continue
        totals[t.category_id] += t.amount_cents
    # Spending is budgeted rather than imported, so a budget replaces the average.
    budgets = {b.category_id: b for b in db.execute(owned(Budget, user)).scalars()}
    out: list[Estimate] = []
    seen: set[int | None] = set()
    for cid, total in totals.items():
        if cid in budgets:
            continue
        monthly = round_div(total * DAYS_PER_MONTH_X10000, AVERAGE_DAYS * 10000)
        if monthly:
            name = cats[cid].name if cid in cats else 'Uncategorised'
            out.append(Estimate(cid, name, monthly, 'average'))
            seen.add(cid)
    # Bills are forecast from their own dates, so a spending budget adds only
    # what it budgets on top of them, which is all that is stored (D64).
    for cid, b in budgets.items():
        cat = cats.get(cid)
        if cat is None or (not cat.is_income and not b.monthly_cents):
            continue
        sign = 1 if cat.is_income else -1
        out.append(Estimate(cid, cat.name, sign * b.monthly_cents, 'budget'))
    out.sort(key=lambda e: e.monthly_cents)
    return out


def build_input(db: Session, user: User, today: date, months: int) -> ForecastInput:
    prefs = settings_for(db, user)
    accts = ledger.accounts(db, user)
    balances = ledger.balances(db, user, today)
    balances = {a.id: balances.get(a.id, 0) for a in accts}
    days = horizon_days(today, months)
    end = today + timedelta(days=days)
    first = today + timedelta(days=1)
    spend_acct = ledger.spending_account(db, user, accts)
    valid = {a.id for a in accts}

    def acct_or_default(aid: int | None) -> int | None:
        return aid if aid in valid else spend_acct

    flows: list[Flow] = []
    # Schedule paydays (their payslip's figures once one records them) and
    # one-off payslips still to come, such as a bonus.
    for item in pay.items(db, user, first, end, today):
        target = acct_or_default(item.account_id)
        if target is not None:
            ref = item.schedule.id if item.schedule else None
            flows.append(Flow(item.when, target, item.net_cents, 'pay', item.label, ref))

    for occ in bills.occurrences(
        db, user, today - timedelta(days=bills.OVERDUE_CARRY_DAYS), end, today
    ):
        if occ.payment is not None:
            continue
        target = acct_or_default(occ.bill.account_id)
        if target is None:
            continue
        # Still unpaid from the past: assume it goes out tomorrow.
        when = occ.due if occ.due > today else first
        flows.append(Flow(when, target, -occ.bill.amount_cents, 'bill', occ.bill.name, occ.bill.id))

    for tr in db.execute(owned(RecurringTransfer, user)).scalars():
        if tr.from_account_id not in valid or tr.to_account_id not in valid:
            continue
        for d in recurrence.occurrences(
            tr.anchor_date, tr.frequency, first, end, until=tr.end_date
        ):
            flows.append(Flow(d, tr.from_account_id, -tr.amount_cents, 'transfer', tr.name, tr.id))
            flows.append(Flow(d, tr.to_account_id, tr.amount_cents, 'transfer', tr.name, tr.id))

    daily: dict[int, Decimal] = {}
    if spend_acct is not None:
        monthly_total = sum(e.monthly_cents for e in estimates(db, user, today))
        if monthly_total:
            daily[spend_acct] = Decimal(monthly_total) * 12 / 365

    loans = [loan_spec(db, loan) for loan in db.execute(owned(Loan, user)).scalars()]
    return ForecastInput(
        today=today,
        days=days,
        balances=balances,
        account_names={a.id: a.name for a in accts},
        account_types={a.id: a.type for a in accts},
        flows=flows,
        daily=daily,
        loans=loans,
        low_balance_cents=prefs.low_balance_cents,
    )


def data_version(db: Session, user: User) -> int:
    return int(db.scalar(select(User.data_version).where(User.id == user.id)) or 0)


def base(db: Session, clock: Clock, user: User, months: int | None = None) -> ForecastResult:
    """The cached base forecast. Recomputed whenever the user's data or the date
    moves. Each horizon asked for is kept beside the others (the dashboard's
    three months and the forecast page's twelve), so they do not push each
    other out."""
    today = clock.today()
    if months is None:
        months = settings_for(db, user).forecast_months
    months = min(24, max(1, months))
    key = f'v{data_version(db, user)}:{today.isoformat()}'
    cached = db.get(ForecastCache, user.id)
    runs: dict[str, Any] = {}
    if cached is not None and cached.cache_key == key and isinstance(cached.payload, dict):
        runs = cached.payload.get('runs') or {}
        if str(months) in runs:
            return ForecastResult.from_json(runs[str(months)])
    result = run(build_input(db, user, today, months))
    # A new dict, so the JSON column is seen to change.
    payload = {'runs': {**runs, str(months): result.to_json()}}
    if cached is None:
        db.add(
            ForecastCache(
                user_id=user.id, cache_key=key, payload=payload, created_at=utcnow_naive(clock)
            )
        )
    else:
        cached.cache_key = key
        cached.payload = payload
        cached.created_at = utcnow_naive(clock)
    db.flush()
    return result


def scenario(
    db: Session, clock: Clock, user: User, scs: list[Scenario], months: int | None = None
) -> ForecastResult:
    """A what-if run, one scenario or several together, every change of each
    laid over the same forecast (D69). Reads real data, writes nothing."""
    today = clock.today()
    if months is None:
        months = settings_for(db, user).forecast_months
    inp = build_input(db, user, today, min(24, max(1, months)))
    changes = [(a.kind, dict(a.params)) for sc in scs for a in sc.adjustments]
    return run(apply_scenario(inp, changes))


def still_to_spend(db: Session, user: User, today: date, days: int) -> list[int]:
    """What is still budgeted for the rest of the budget month, today and each
    day of a forecast `days` long (D51). Today's is the budget page's Still to
    spend; later days spend each line as the forecast does."""
    start, end = budgeting.period(db, user, today)
    rows = [
        r for r in budgeting.rows(db, user, start, end) if r.has_budget and not r.category.is_income
    ]
    # The forecast spends the part of a budget beyond its bills a day at a
    # time, and the bills on their dates (`build_input`).
    per_day = {
        e.category_id: Fraction(-e.monthly_cents * 12, 365)
        for e in estimates(db, user, today)
        if e.source == 'budget' and e.monthly_cents < 0
    }
    horizon = today + timedelta(days=days)
    first = today + timedelta(days=1)
    recurring = budgeting.bills_by_category(db, user, start, end, one_offs=False)
    unpaid: dict[int, list[tuple[date, date, int]]] = defaultdict(list)
    for occ in bills.occurrences(
        db, user, today - timedelta(days=bills.OVERDUE_CARRY_DAYS), horizon, today
    ):
        if occ.payment is None and occ.bill.category_id is not None:
            when = occ.due if occ.due > today else first
            unpaid[occ.bill.category_id].append((when, occ.due, occ.bill.amount_cents))
    lines = [
        BudgetLine(
            left_cents=r.remaining_cents,
            monthly_cents=r.estimate_cents + recurring.get(r.category.id, 0),
            per_day=per_day.get(r.category.id, Fraction(0)),
            bills=tuple(unpaid.get(r.category.id, ())),
            saved=r.saving,
        )
        for r in rows
    ]
    # Loan repayments as the budget has them: today's offset held steady.
    last = budgeting.period(db, user, horizon)[1]
    balances = ledger.balances(db, user, today)
    due: list[tuple[date, int]] = []
    for loan in db.execute(owned(Loan, user)).scalars():
        spec = loan_spec(db, loan)
        offset = sum(max(0, balances.get(a, 0)) for a in spec.offset_account_ids)
        due.extend(repayments(spec, first, last, offset))
    return _still_to_spend(today, days, budgeting.start_day(db, user), lines, due)


@dataclass(frozen=True)
class Free:
    """Free to spend, day by day, and the two things taken off the accounts."""

    days: list[int]
    goals_held: int
    to_spend: int  # today's, the budget page's Still to spend


def free_to_spend(
    db: Session,
    user: User,
    today: date,
    base: ForecastResult,
    accounts: list[Account] | None = None,
) -> Free:
    """Free to spend as the budget page has it today, for each day of `base`
    (D51): the accounts it counts, less what goals keep aside today (the same
    amount off every day, D46) and what is still budgeted for the rest of each
    budget month. `accounts` is `ledger.accounts`, when the caller has it."""
    if accounts is None:
        accounts = ledger.accounts(db, user)
    accts = [a for a in accounts if not a.is_set_aside]
    counted = [a.id for a in accts if a.id in base.accounts]
    held = goals.held_outside(db, user, accts)
    to_spend = still_to_spend(db, user, today, len(base.dates) - 1)
    days = [
        sum(base.accounts[a][i] for a in counted) - held - to_spend[i]
        for i in range(len(base.dates))
    ]
    return Free(days, held, to_spend[0])


# The chart's own lines a person can turn off or on in its legend; each
# account is `account:<id>` (D50).
CHART_LINES = frozenset({'combined', 'trend', 'free', 'free_trend', 'scenario', 'scenario_trend'})


def chart_lines(db: Session, user: User) -> dict[str, bool]:
    """The chart lines turned off (False) or on (True) by hand, kept until
    turned back; a line not in it shows as the chart draws it."""
    try:
        saved = json.loads(settings_for(db, user).chart_lines or '{}')
    except ValueError:
        return {}
    if not isinstance(saved, dict):
        return {}
    return {k: v for k, v in saved.items() if isinstance(k, str) and isinstance(v, bool)}


def set_chart_line(db: Session, user: User, key: str, shown: bool) -> None:
    lines = chart_lines(db, user)
    lines[key] = shown
    settings_for(db, user).chart_lines = json.dumps(lines, sort_keys=True)
    db.flush()
