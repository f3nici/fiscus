"""Loan summaries: repayment, offset savings and payoff with and without the offset."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.orm import Session

from fiscus.domain import loan as loan_math
from fiscus.domain.tax import fy_bounds
from fiscus.models import Loan, User
from fiscus.services import ledger
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
    with_offset: loan_math.Payoff
    without_offset: loan_math.Payoff
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

    def saved_since(start: date) -> int:
        start = max(start, loan.start_date)
        if start > today:
            return 0
        daily = ledger.daily_balances(db, user, list(spec.offset_account_ids), start, today)
        return loan_math.interest_saved(loan.balance_cents, spec.rates, daily)

    # The current repayment, after any rate changes up to today.
    sim = loan_math.LoanSim(spec)
    day = spec.balance_date
    while day < today:
        day += timedelta(days=1)
        sim.repayments_due(day)
        sim.end_of_day(day, offset_now)
    projected_from = max(today, spec.balance_date)
    with_offset = loan_math.project_payoff(spec, projected_from, lambda _d: offset_now)
    without = loan_math.project_payoff(spec, projected_from, lambda _d: 0)
    return LoanSummary(
        loan=loan,
        rate=str(loan_math.rate_on(spec.rates, today)),
        repayment_cents=sim.payment,
        offset_ids=spec.offset_account_ids,
        offset_balance_cents=offset_now,
        saved_month_cents=saved_since(month_start),
        saved_year_cents=saved_since(fy_start),
        with_offset=with_offset,
        without_offset=without,
        started=loan.start_date <= today,
    )
