"""Gathering forecast inputs from the database, and the per-user cache."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

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
    Payslip,
    RecurringTransfer,
    Scenario,
    Transaction,
    User,
)
from fiscus.services import bills, ledger, pay
from fiscus.services.access import owned
from fiscus.services.users import settings_for

AVERAGE_DAYS = 90
OVERDUE_CARRY_DAYS = 62


@dataclass(frozen=True)
class Estimate:
    category_id: int | None
    name: str
    monthly_cents: int  # negative is spending
    source: str  # average | budget


def horizon_days(today: date, months: int) -> int:
    months = min(24, max(1, months))
    return (recurrence.add_months(today, months) - today).days


def spending_account(db: Session, user: User, accts: list[Account]) -> int | None:
    pref = settings_for(db, user).spending_account_id
    if pref and any(a.id == pref for a in accts):
        return pref
    for kind in ('transaction', 'offset', 'savings', 'credit_card'):
        for a in accts:
            if a.type == kind and not a.is_set_aside:
                return a.id
    return accts[0].id if accts else None


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
    """Discretionary spending per category: 3-month average, or the budget if chosen."""
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
        if t.id in paid_txn_ids:
            continue  # bills are forecast from their schedule, not their history
        cat = cats.get(t.category_id) if t.category_id else None
        if cat is not None and cat.is_transfer:
            continue
        if cat is not None and cat.is_income and not cat.is_business:
            continue  # salary comes from pay schedules
        if cat is None and t.amount_cents > 0:
            continue
        totals[t.category_id] += t.amount_cents
    budgets = {
        b.category_id: b for b in db.execute(owned(Budget, user)).scalars() if b.use_in_forecast
    }
    out: list[Estimate] = []
    seen: set[int | None] = set()
    for cid, total in totals.items():
        if cid in budgets:
            continue
        monthly = round(total * 30.4375 / AVERAGE_DAYS)
        if monthly:
            name = cats[cid].name if cid in cats else 'Uncategorised'
            out.append(Estimate(cid, name, monthly, 'average'))
            seen.add(cid)
    for cid, b in budgets.items():
        cat = cats.get(cid)
        if cat is None:
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
    spend_acct = spending_account(db, user, accts)
    valid = {a.id for a in accts}

    def acct_or_default(aid: int | None) -> int | None:
        return aid if aid in valid else spend_acct

    flows: list[Flow] = []
    for ps, net in pay.schedules(db, user, today):
        target = acct_or_default(ps.account_id)
        if target is None:
            continue
        for d in recurrence.occurrences(
            ps.anchor_date, ps.frequency, first, end, until=ps.end_date
        ):
            flows.append(Flow(d, target, net, 'pay', ps.name, ps.id))

    # A payslip dated after today is a one-off pay still to come, such as a bonus.
    for slip in db.execute(
        owned(Payslip, user).where(Payslip.date >= first, Payslip.date <= end)
    ).scalars():
        target = acct_or_default(slip.account_id)
        if target is not None:
            flows.append(
                Flow(slip.date, target, slip.net_cents, 'pay', slip.source or 'Payslip', slip.id)
            )

    for occ in bills.occurrences(db, user, today - timedelta(days=OVERDUE_CARRY_DAYS), end, today):
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
    """The cached base forecast. Recomputed whenever the user's data or the date moves."""
    today = clock.today()
    if months is None:
        months = settings_for(db, user).forecast_months
    months = min(24, max(1, months))
    key = f'v{data_version(db, user)}:{today.isoformat()}:{months}'
    cached = db.get(ForecastCache, user.id)
    if cached is not None and cached.cache_key == key:
        return ForecastResult.from_json(cached.payload)
    result = run(build_input(db, user, today, months))
    if cached is None:
        db.add(
            ForecastCache(
                user_id=user.id,
                cache_key=key,
                payload=result.to_json(),
                created_at=utcnow_naive(clock),
            )
        )
    else:
        cached.cache_key = key
        cached.payload = result.to_json()
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
