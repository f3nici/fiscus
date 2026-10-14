"""The forecast engine. Pure: inputs in, daily balances out.

Day 0 is today with today's real balances. Each later day applies, in order:
scheduled flows (pay, bills at this person's share, transfers, one-offs),
loan repayments, the day's slice of discretionary spending, then end-of-day
loan interest on (loan - offset) using the offset accounts' balances as they
stand that evening.

Scenarios never reach this module as database rows: `apply_scenario` returns
a modified copy of the inputs and the engine runs on that.
"""

from __future__ import annotations

import calendar
import copy
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Any

from fiscus.domain import recurrence
from fiscus.domain.loan import LoanSim, LoanSpec
from fiscus.money import round_div


@dataclass(frozen=True)
class Flow:
    date: date
    account_id: int
    amount_cents: int
    kind: str  # pay | bill | transfer | one_off | income
    label: str
    ref: int | None = None


@dataclass
class ForecastInput:
    today: date
    days: int
    balances: dict[int, int]
    account_names: dict[int, str]
    account_types: dict[int, str]
    flows: list[Flow] = field(default_factory=list)
    # cents per day, per account (negative is spending)
    daily: dict[int, Decimal] = field(default_factory=dict)
    loans: list[LoanSpec] = field(default_factory=list)
    low_balance_cents: int = 0


@dataclass(frozen=True)
class Trend:
    """The straight line that best fits a run of daily balances (least
    squares), one value a day, and how much it rises in an average month."""

    line: list[int]
    per_month: int


def _cents(value: Fraction) -> int:
    return round_div(value.numerator, value.denominator)


def trend(values: list[int]) -> Trend:
    n = len(values)
    if n < 2:
        return Trend(list(values), 0)
    sx = n * (n - 1) // 2
    sxx = (n - 1) * n * (2 * n - 1) // 6
    sy = sum(values)
    sxy = sum(i * v for i, v in enumerate(values))
    slope = Fraction(n * sxy - sx * sy, n * sxx - sx * sx)
    start = (sy - slope * sx) / n
    # 365.25 / 12 days in a month.
    return Trend([_cents(start + slope * i) for i in range(n)], _cents(slope * 1461 / 48))


@dataclass
class ForecastResult:
    dates: list[date]
    accounts: dict[int, list[int]]
    combined: list[int]
    loans: dict[int, list[int]]
    low_balance: dict[str, Any] | None
    month_end: list[dict[str, Any]]
    loan_payoff: dict[int, date | None]
    loan_interest: dict[int, int]

    @property
    def trend(self) -> Trend:
        """The combined balance's trend over every day shown, today included."""
        return trend(self.combined)

    def balance_on(self, day: date) -> int | None:
        idx = (day - self.dates[0]).days
        if 0 <= idx < len(self.dates):
            return self.combined[idx]
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            'dates': [d.isoformat() for d in self.dates],
            'accounts': {str(k): v for k, v in self.accounts.items()},
            'combined': self.combined,
            'loans': {str(k): v for k, v in self.loans.items()},
            'low_balance': (
                {**self.low_balance, 'date': self.low_balance['date'].isoformat()}
                if self.low_balance
                else None
            ),
            'month_end': [{**m, 'date': m['date'].isoformat()} for m in self.month_end],
            'loan_payoff': {
                str(k): (v.isoformat() if v else None) for k, v in self.loan_payoff.items()
            },
            'loan_interest': {str(k): v for k, v in self.loan_interest.items()},
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> ForecastResult:
        def d(s: str) -> date:
            return date.fromisoformat(s)

        low = raw['low_balance']
        return cls(
            dates=[d(x) for x in raw['dates']],
            accounts={int(k): v for k, v in raw['accounts'].items()},
            combined=raw['combined'],
            loans={int(k): v for k, v in raw['loans'].items()},
            low_balance={**low, 'date': d(low['date'])} if low else None,
            month_end=[{**m, 'date': d(m['date'])} for m in raw['month_end']],
            loan_payoff={int(k): d(v) if v else None for k, v in raw['loan_payoff'].items()},
            loan_interest={int(k): v for k, v in raw['loan_interest'].items()},
        )


def _round(value: Decimal) -> int:
    return int(value.quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def run(inp: ForecastInput) -> ForecastResult:
    balances = dict(inp.balances)
    dates = [inp.today + timedelta(days=i) for i in range(inp.days + 1)]
    end = dates[-1]
    by_day: dict[date, list[Flow]] = {}
    for f in inp.flows:
        if inp.today < f.date <= end:
            by_day.setdefault(f.date, []).append(f)

    series: dict[int, list[int]] = {a: [balances.get(a, 0)] for a in inp.account_names}
    combined = [sum(balances.values())]
    sims = [
        LoanSim(spec, opening_offset_cents=sum(balances.get(a, 0) for a in spec.offset_account_ids))
        for spec in inp.loans
    ]
    # Roll each loan from the day its balance was known up to today, without
    # touching accounts: those repayments are already in the bank history.
    for sim in sims:
        day = sim.spec.balance_date
        offset_now = sim.opening_offset_cents
        while day < inp.today:
            day += timedelta(days=1)
            sim.repayments_due(day)
            sim.end_of_day(day, offset_now)
    loan_series: dict[int, list[int]] = {s.spec.id: [s.balance] for s in sims}
    interest_before = {s.spec.id: s.interest_charged for s in sims}
    carry: dict[int, Decimal] = dict.fromkeys(inp.daily, Decimal(0))
    low: dict[str, Any] | None = None
    watched = {a for a, t in inp.account_types.items() if t != 'credit_card'}

    for day in dates[1:]:
        for f in by_day.get(day, []):
            balances[f.account_id] = balances.get(f.account_id, 0) + f.amount_cents
        for sim in sims:
            for amount, acct in sim.repayments_due(day):
                if acct is not None:
                    balances[acct] = balances.get(acct, 0) - amount
        for acct, per_day in inp.daily.items():
            carry[acct] += per_day
            whole = _round(carry[acct])
            if whole:
                carry[acct] -= whole
                balances[acct] = balances.get(acct, 0) + whole
        for sim in sims:
            offset = sum(balances.get(a, 0) for a in sim.spec.offset_account_ids)
            sim.end_of_day(day, offset)
            loan_series[sim.spec.id].append(max(sim.balance, 0))
        for acct in series:
            series[acct].append(balances.get(acct, 0))
        combined.append(sum(balances.get(a, 0) for a in series))
        if low is None:
            for acct in sorted(watched):
                if balances.get(acct, 0) < inp.low_balance_cents:
                    low = {
                        'date': day,
                        'account_id': acct,
                        'account': inp.account_names.get(acct, ''),
                        'balance': balances.get(acct, 0),
                    }
                    break

    month_end: list[dict[str, Any]] = []
    for i, day in enumerate(dates):
        last = calendar.monthrange(day.year, day.month)[1]
        if day.day == last or i == len(dates) - 1:
            month_end.append(
                {
                    'date': day,
                    'combined': combined[i],
                    'accounts': {str(a): series[a][i] for a in series},
                }
            )

    return ForecastResult(
        dates=dates,
        accounts=series,
        combined=combined,
        loans=loan_series,
        low_balance=low,
        month_end=month_end,
        loan_payoff={s.spec.id: s.paid_off for s in sims},
        loan_interest={s.spec.id: s.interest_charged - interest_before[s.spec.id] for s in sims},
    )


# ------------------------------------------------------------------ scenarios

SCENARIO_KINDS = {
    'one_off': 'One-off expense or income',
    'income_change': 'Income change',
    'rate_change': 'Interest rate change',
    'new_bill': 'New recurring bill',
    'new_income': 'New recurring income (after tax)',
    'extra_repayment': 'Extra loan repayment',
}


class ScenarioError(ValueError):
    pass


def _d(params: dict[str, Any], key: str) -> date:
    try:
        return date.fromisoformat(str(params[key]))
    except (KeyError, ValueError) as exc:
        raise ScenarioError(f'{key} must be a date') from exc


def _i(params: dict[str, Any], key: str) -> int:
    try:
        return int(params[key])
    except (KeyError, ValueError, TypeError) as exc:
        raise ScenarioError(f'{key} must be a whole number') from exc


def apply_scenario(
    inp: ForecastInput, adjustments: list[tuple[str, dict[str, Any]]]
) -> ForecastInput:
    """A changed copy. The input object is never mutated."""
    out = copy.deepcopy(inp)
    end = out.today + timedelta(days=out.days)
    for kind, p in adjustments:
        if kind == 'one_off':
            out.flows.append(
                Flow(
                    _d(p, 'date'),
                    _i(p, 'account_id'),
                    _i(p, 'amount_cents'),
                    'one_off',
                    str(p.get('label') or 'Scenario'),
                )
            )
        elif kind == 'income_change':
            start = _d(p, 'from_date')
            target = p.get('pay_schedule_id')
            new_amount = p.get('new_net_cents')
            delta = int(p.get('delta_cents') or 0)
            changed: list[Flow] = []
            for f in out.flows:
                hit = (
                    f.kind == 'pay'
                    and f.date >= start
                    and (target in (None, '') or f.ref == int(target))
                )
                if hit:
                    amount = int(new_amount) if new_amount not in (None, '') else f.amount_cents
                    changed.append(
                        Flow(f.date, f.account_id, amount + delta, f.kind, f.label, f.ref)
                    )
                else:
                    changed.append(f)
            out.flows = changed
        elif kind == 'rate_change':
            loan_id = _i(p, 'loan_id')
            start = _d(p, 'from_date')
            rate = Decimal(str(p['rate']))
            for spec in out.loans:
                if spec.id == loan_id:
                    kept = [(d, r) for d, r in spec.rates if d < start]
                    spec.rates = [*kept, (start, rate)]
        elif kind in ('new_bill', 'new_income'):
            # Income is its own kind of flow, so an income change to every
            # pay schedule leaves it alone.
            bill = kind == 'new_bill'
            anchor = _d(p, 'start_date')
            until = date.fromisoformat(p['end_date']) if p.get('end_date') else None
            amount = abs(_i(p, 'amount_cents')) * (-1 if bill else 1)
            label = str(p.get('name') or ('New bill' if bill else 'New income'))
            for when in recurrence.occurrences(
                anchor, str(p['frequency']), out.today + timedelta(days=1), end, until=until
            ):
                flow_kind = 'bill' if bill else 'income'
                out.flows.append(Flow(when, _i(p, 'account_id'), amount, flow_kind, label))
        elif kind == 'extra_repayment':
            loan_id = _i(p, 'loan_id')
            anchor = _d(p, 'date')
            freq = str(p.get('frequency') or 'one_off')
            acct = p.get('account_id')
            for spec in out.loans:
                if spec.id != loan_id:
                    continue
                for when in recurrence.occurrences(
                    anchor, freq, out.today + timedelta(days=1), end
                ):
                    spec.extra_repayments.append(
                        (when, abs(_i(p, 'amount_cents')), int(acct) if acct else None)
                    )
        else:
            raise ScenarioError(f'Unknown adjustment {kind!r}')
    return out
