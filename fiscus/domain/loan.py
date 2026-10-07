"""Home loan maths.

Repayment: the standard annuity, P x r / (1 - (1 + r)^-n), with r the annual
rate over the repayments per year. Interest: accrued daily on (balance - offset)
at rate / 365, never below zero, and charged to the balance once a month on
the loan's anniversary day. Fractions of a cent carry until the charge, which
is how an Australian lender's statement adds up.

A variable-rate change recalculates the repayment over the remaining term from
the balance on that day, as lenders do, unless the repayment is fixed by hand.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from fiscus.domain.recurrence import add_months, nth

PERIODS_PER_YEAR = {'weekly': 52, 'fortnightly': 26, 'monthly': 12}
DAYS_PER_YEAR = Decimal(365)
ONE = Decimal(1)
CENT = Decimal('1')


def cents(value: Decimal) -> int:
    return int(value.quantize(CENT, rounding=ROUND_HALF_UP))


def repayment(principal_cents: int, annual_rate_pct: Decimal, periods: int, per_year: int) -> int:
    if periods <= 0:
        return principal_cents
    if principal_cents <= 0:
        return 0
    r = annual_rate_pct / Decimal(100) / Decimal(per_year)
    p = Decimal(principal_cents)
    if r == 0:
        return cents(p / periods)
    return cents(p * r / (ONE - (ONE + r) ** -periods))


def rate_on(rates: list[tuple[date, Decimal]], day: date) -> Decimal:
    current = rates[0][1] if rates else Decimal(0)
    for eff, rate in rates:
        if eff <= day:
            current = rate
        else:
            break
    return current


@dataclass
class LoanSpec:
    id: int
    name: str
    principal_cents: int
    start_date: date
    term_months: int
    frequency: str
    balance_cents: int
    balance_date: date
    rates: list[tuple[date, Decimal]]
    repayment_override_cents: int | None = None
    account_id: int | None = None
    offset_account_ids: tuple[int, ...] = ()
    # (date, cents, from account) applied on top of the scheduled repayment
    extra_repayments: list[tuple[date, int, int | None]] = field(default_factory=list)

    @property
    def maturity(self) -> date:
        return add_months(self.start_date, self.term_months)

    @property
    def per_year(self) -> int:
        return PERIODS_PER_YEAR[self.frequency]

    def periods_between(self, day: date) -> int:
        """Repayments still to come after `day` until maturity."""
        n = 0
        k = 1
        while True:
            d = nth(self.start_date, self.frequency, k)
            if d > self.maturity:
                return n
            if d > day:
                n += 1
            k += 1
            if k > 5000:
                return n

    def scheduled_repayment(self) -> int:
        """The repayment as at the balance date.

        At settlement's rate on the amount borrowed, unless the rate has changed
        since: then repriced from the known balance over the remaining term, as
        the lender would have done when it changed.
        """
        if self.repayment_override_cents:
            return self.repayment_override_cents
        changed = any(self.start_date < eff <= self.balance_date for eff, _ in self.rates)
        if changed:
            rate = rate_on(self.rates, self.balance_date)
            remaining = self.periods_between(self.balance_date)
            return repayment(self.balance_cents, rate, max(remaining, 1), self.per_year)
        rate = rate_on(self.rates, self.start_date)
        periods = self.term_months * self.per_year // 12
        return repayment(self.principal_cents, rate, periods, self.per_year)


def repayments(
    spec: LoanSpec, start: date, end: date, offset_cents: int = 0
) -> list[tuple[date, int]]:
    """Scheduled repayments from start to end, repriced at each rate change.

    Stepped from the balance date with the offset held at `offset_cents`. Days
    before the balance date show the repayment as at that date.
    """
    sim = LoanSim(spec, opening_offset_cents=offset_cents)
    out: list[tuple[date, int]] = []
    day = start
    while day <= min(end, spec.balance_date):
        if is_repayment_day(spec, day) and day <= spec.maturity:
            out.append((day, sim.payment))
        day += timedelta(days=1)
    day = spec.balance_date
    while day < end:
        day += timedelta(days=1)
        paid = sim.repayments_due(day)
        # The scheduled repayment comes first in what is paid that day.
        if paid and day >= start and is_repayment_day(spec, day):
            out.append((day, paid[0][0]))
        if sim.balance <= 0:
            break
        sim.end_of_day(day, offset_cents)
    return out


def is_repayment_day(spec: LoanSpec, day: date) -> bool:
    if day <= spec.start_date:
        return False
    if spec.frequency == 'monthly':
        return nth(spec.start_date, 'monthly', _months_between(spec.start_date, day)) == day
    step = 7 if spec.frequency == 'weekly' else 14
    return (day - spec.start_date).days % step == 0


def is_charge_day(spec: LoanSpec, day: date) -> bool:
    """Interest is charged monthly on the start date's day of month (clamped)."""
    if day <= spec.start_date:
        return False
    return nth(spec.start_date, 'monthly', _months_between(spec.start_date, day)) == day


def _months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def last_charge(spec: LoanSpec, day: date) -> date:
    """The latest interest charge day on or before `day`; the start date before the first."""
    if day <= spec.start_date:
        return spec.start_date
    n = _months_between(spec.start_date, day)
    charge = nth(spec.start_date, 'monthly', n)
    if charge > day:
        charge = nth(spec.start_date, 'monthly', n - 1)
    return max(charge, spec.start_date)


def accrued_at_balance_date(spec: LoanSpec, offset_cents: int) -> Decimal:
    """Interest accrued from the last charge to the end of the balance date.

    A lender's balance leaves it out until the charge day adds it, so the first
    charge after the balance date includes it. Worked on the balance as known
    (repayments in between are not undone) and the offset given.
    """
    effective = Decimal(max(0, spec.balance_cents - max(0, offset_cents)))
    total = Decimal(0)
    day = last_charge(spec, spec.balance_date)
    while day < spec.balance_date:
        day += timedelta(days=1)
        total += effective * rate_on(spec.rates, day) / Decimal(100) / DAYS_PER_YEAR
    return total


@dataclass
class LoanSim:
    """Steps one loan a day at a time. The forecast owns the account balances."""

    spec: LoanSpec
    balance: int = 0
    accrued: Decimal = Decimal(0)
    payment: int = 0
    rate: Decimal = Decimal(0)
    paid_off: date | None = None
    interest_charged: int = 0
    extra_by_day: dict[date, list[tuple[int, int | None]]] = field(default_factory=dict)
    # The offset as at the balance date, for the interest accrued before it.
    opening_offset_cents: int = 0

    def __post_init__(self) -> None:
        self.balance = self.spec.balance_cents
        self.accrued = accrued_at_balance_date(self.spec, self.opening_offset_cents)
        self.payment = self.spec.scheduled_repayment()
        self.rate = rate_on(self.spec.rates, self.spec.balance_date)
        for d, amount, acct in self.spec.extra_repayments:
            self.extra_by_day.setdefault(d, []).append((amount, acct))

    def _maybe_reprice(self, day: date) -> None:
        rate = rate_on(self.spec.rates, day)
        if rate != self.rate:
            self.rate = rate
            if not self.spec.repayment_override_cents:
                remaining = self.spec.periods_between(day)
                self.payment = repayment(self.balance, rate, max(remaining, 1), self.spec.per_year)

    def repayments_due(self, day: date) -> list[tuple[int, int | None]]:
        """Money leaving accounts today for this loan, as (cents, account)."""
        self._maybe_reprice(day)
        out: list[tuple[int, int | None]] = []
        if self.balance <= 0:
            return out
        if is_repayment_day(self.spec, day):
            amount = min(self.payment, self.balance)
            if amount > 0:
                out.append((amount, self.spec.account_id))
                self.balance -= amount
        for extra, acct in self.extra_by_day.get(day, []):
            amount = min(extra, max(self.balance, 0))
            if amount > 0:
                out.append((amount, acct if acct is not None else self.spec.account_id))
                self.balance -= amount
        if self.balance <= 0 and self.paid_off is None:
            self.paid_off = day
        return out

    def end_of_day(self, day: date, offset_cents: int) -> Decimal:
        """Accrue today's interest; charge it on the charge day. Returns today's accrual."""
        if self.balance <= 0:
            return Decimal(0)
        effective = max(0, self.balance - max(0, offset_cents))
        today = Decimal(effective) * self.rate / Decimal(100) / DAYS_PER_YEAR
        self.accrued += today
        if is_charge_day(self.spec, day):
            charge = cents(self.accrued)
            self.balance += charge
            self.interest_charged += charge
            self.accrued = Decimal(0)
        return today


@dataclass(frozen=True)
class Payoff:
    paid_off: date | None
    total_interest: int
    repayment: int


def project_payoff(
    spec: LoanSpec, from_day: date, offset_for: Callable[[date], int], max_years: int = 50
) -> Payoff:
    """When the loan is paid off, and the interest still to come after `from_day`.

    Stepped from the balance date, so repayments between it and `from_day`
    are counted rather than skipped.
    """
    sim = LoanSim(spec, opening_offset_cents=offset_for(spec.balance_date))
    day = spec.balance_date
    limit = max(from_day, day) + timedelta(days=365 * max_years)
    charged_before = 0
    while day <= limit:
        day += timedelta(days=1)
        sim.repayments_due(day)
        if sim.balance <= 0:
            break
        sim.end_of_day(day, offset_for(day))
        if day <= from_day:
            charged_before = sim.interest_charged
    total = sim.interest_charged - charged_before + cents(sim.accrued)
    return Payoff(sim.paid_off, total, sim.payment)


def interest_saved(
    balance_cents: int,
    rates: list[tuple[date, Decimal]],
    offset_by_day: dict[date, int],
) -> int:
    """Interest the offset avoided over the given days, holding the balance fixed."""
    total = Decimal(0)
    for day, offset in offset_by_day.items():
        covered = min(balance_cents, max(0, offset))
        total += Decimal(covered) * rate_on(rates, day) / Decimal(100) / DAYS_PER_YEAR
    return cents(total)
