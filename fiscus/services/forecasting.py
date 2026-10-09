"""Gathering forecast inputs from the database, and the per-user cache."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fiscus.clock import Clock, utcnow_naive
from fiscus.domain import recurrence
from fiscus.domain.forecast import Flow, ForecastInput, ForecastResult, apply_scenario, run
from fiscus.domain.loan import LoanSpec
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
from fiscus.services import bills, budgeting, ledger, pay
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
        if t.id in paid_txn_ids or t.bill_payment_id or t.payslip_id:
            continue  # bills and pay are forecast from their schedules, not their history
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
    # Bills are forecast from their own dates and paid out of the budget, so
    # only the part of a spending budget beyond its bills is added here.
    in_bills = budgeting.bills_by_category(db, user, today, today, one_offs=False)
    for cid, b in budgets.items():
        cat = cats.get(cid)
        if cat is None:
            continue
        if cat.is_income:
            out.append(Estimate(cid, cat.name, b.monthly_cents, 'budget'))
        else:
            rest = max(0, b.monthly_cents - in_bills.get(cid, 0))
            out.append(Estimate(cid, cat.name, -rest, 'budget'))
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
        target = acct_or_default(occ.share.account_id)
        if target is None:
            continue
        # Still unpaid from the past: assume it goes out tomorrow.
        when = occ.due if occ.due > today else first
        flows.append(Flow(when, target, -occ.share_cents, 'bill', occ.bill.name, occ.bill.id))

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
        large_bill_cents=prefs.large_bill_cents,
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
    db: Session, clock: Clock, user: User, sc: Scenario, months: int | None = None
) -> ForecastResult:
    """A what-if run. Reads real data, writes nothing."""
    today = clock.today()
    if months is None:
        months = settings_for(db, user).forecast_months
    inp = build_input(db, user, today, min(24, max(1, months)))
    adjusted = apply_scenario(inp, [(a.kind, dict(a.params)) for a in sc.adjustments])
    return run(adjusted)
