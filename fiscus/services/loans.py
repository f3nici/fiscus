"""Loan summaries: repayment, offset savings and payoff with and without the offset."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.orm import Session

from fiscus.domain import loan as loan_math
from fiscus.domain.loan import repayments
from fiscus.domain.tax import fy_bounds
from fiscus.models import Loan, User
from fiscus.services import ledger
from fiscus.services.access import owned
from fiscus.services.forecasting import loan_spec


@dataclass(frozen=True)
class LoanSummary:
    loan: Loan
    rate: str
    repayment_cents: int
    offset_ids: tuple[int, ...]
    offset_balance_cents: int
    saved_month_cents: int
    saved_year_cents: int
    # The first day of each that is counted, when the offset's balance was
    # first typed in after the month or year began.
    saved_month_from: date | None
    saved_year_from: date | None
    with_offset: loan_math.Payoff
    without_offset: loan_math.Payoff
    next_repayment: date | None = None
    # False until settlement: there is no interest, so nothing saved yet.
    started: bool = True

    @property
    def lifetime_saving_cents(self) -> int:
        return self.without_offset.total_interest - self.with_offset.total_interest

    @property
    def months_sooner(self) -> int | None:
        a, b = self.with_offset.paid_off, self.without_offset.paid_off
        if a is None or b is None:
            return None
        return (b.year - a.year) * 12 + (b.month - a.month)


def summarise(db: Session, user: User, loan: Loan, today: date) -> LoanSummary:
    spec = loan_spec(db, loan)
    balances = ledger.balances(db, user, today)
    offset_now = sum(max(0, balances.get(a, 0)) for a in spec.offset_account_ids)

    month_start = today.replace(day=1)
    fy_start, _ = fy_bounds(today)

    def saved_since(start: date) -> tuple[int, date | None]:
        start = max(start, loan.start_date)
        if start > today:
            return 0, None
        daily, known = ledger.balance_history(db, user, list(spec.offset_account_ids), start, today)
        saved = loan_math.interest_saved(loan.balance_cents, spec.rates, daily)
        return saved, (known if known > start else None)

    # The current repayment, after any rate changes up to today.
    sim = loan_math.LoanSim(spec, opening_offset_cents=offset_now)
    day = spec.balance_date
    while day < today:
        day += timedelta(days=1)
        sim.repayments_due(day)
        sim.end_of_day(day, offset_now)
    month_saved, month_from = saved_since(month_start)
    year_saved, year_from = saved_since(fy_start)
    projected_from = max(today, spec.balance_date)
    with_offset = loan_math.project_payoff(spec, projected_from, lambda _d: offset_now)
    without = loan_math.project_payoff(spec, projected_from, lambda _d: 0)
    return LoanSummary(
        loan=loan,
        rate=str(loan_math.rate_on(spec.rates, today)),
        repayment_cents=sim.payment,
        offset_ids=spec.offset_account_ids,
        offset_balance_cents=offset_now,
        saved_month_cents=month_saved,
        saved_year_cents=year_saved,
        saved_month_from=month_from,
        saved_year_from=year_from,
        with_offset=with_offset,
        without_offset=without,
        next_repayment=spec.next_repayment(today) if loan.balance_cents > 0 else None,
        started=loan.start_date <= today,
    )


@dataclass(frozen=True)
class BudgetLoan:
    """A home loan's repayments in a budget month: always in the budget, set by
    the loan, and spent as each repayment day passes."""

    loan: Loan
    budget_cents: int
    paid_cents: int
    count: int
    paid_count: int

    @property
    def remaining_cents(self) -> int:
        return self.budget_cents - self.paid_cents


def budget_lines(db: Session, user: User, start: date, end: date, today: date) -> list[BudgetLoan]:
    out = []
    loans = db.execute(owned(Loan, user).order_by(Loan.name)).scalars().all()
    balances = ledger.balances(db, user, today) if loans else {}
    for loan in loans:
        spec = loan_spec(db, loan)
        # Today's offset held steady, as on the calendar.
        offset = sum(max(0, balances.get(a, 0)) for a in spec.offset_account_ids)
        due = repayments(spec, start, end, offset)
        if not due:
            continue
        paid = [p for d, p in due if d <= today]
        out.append(BudgetLoan(loan, sum(p for _, p in due), sum(paid), len(due), len(paid)))
    return out
