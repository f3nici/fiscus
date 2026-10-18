"""Whether a saved-up budget line will hold enough for its bills.

A line spent every few months saves a month's budget each budget month. Each
budget month ahead with a bill to pay is checked against everything due up to
its end: what the line holds now, the budget months still to come before then,
and what was put aside from it into a goal since its last bill (that money is
out of the line but kept for the bill). Two bills in one line (two cars' rego)
are checked together, so paying the first does not leave the second short.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date

from fiscus.domain import recurrence
from fiscus.domain.goals import months_left


@dataclass(frozen=True)
class Due:
    due: date
    cents: int
    # Marked paid before its budget month: it has not come off the line yet,
    # but will, so it counts in what later bills need.
    paid: bool = False


@dataclass(frozen=True)
class DueCheck:
    due: date
    need_cents: int  # bills due from this budget month to the end of the one holding `due`
    by_then_cents: int  # what the line will hold by then
    saved_cents: int  # what it holds in this budget month
    bill_cents: int  # the bills to pay in the budget month holding `due`

    @property
    def short_cents(self) -> int:
        return max(0, self.need_cents - self.by_then_cents)

    @property
    def lined_up_cents(self) -> int:
        """What the line would hold in this budget month to have just enough
        by then: the bills less the months still to come and what was put
        aside."""
        return max(0, self.saved_cents + self.need_cents - self.by_then_cents)


@dataclass(frozen=True)
class Outlook:
    """One check per budget month with a bill to pay, in order."""

    checks: tuple[DueCheck, ...]

    @property
    def next(self) -> DueCheck:
        return self.checks[0]

    @property
    def worst(self) -> DueCheck | None:
        """Where the line falls furthest short, the first such if several."""
        short = [c for c in self.checks if c.short_cents]
        return max(short, key=lambda c: c.short_cents) if short else None

    @property
    def lined_up_cents(self) -> int:
        """What the line would hold in this budget month to have every bill
        on its day."""
        return max(c.lined_up_cents for c in self.checks)


def check(
    *,
    period_start: date,
    start_day: int,
    due: date,
    need_cents: int,
    saved_cents: int,
    monthly_cents: int = 0,
    put_aside_cents: int = 0,
    bill_cents: int | None = None,
    added: Callable[[int], int] | None = None,
) -> DueCheck:
    """`saved_cents` is what the line holds to the end of the budget month
    starting `period_start`; each later month up to the one holding `due`
    adds `monthly_cents`, or `added(n)` is what the next n months add."""
    ahead = max(0, months_left(period_start, due, start_day) - 1)
    gained = added(ahead) if added is not None else ahead * monthly_cents
    by_then = saved_cents + gained + put_aside_cents
    bill = need_cents if bill_cents is None else bill_cents
    return DueCheck(due, need_cents, by_then, saved_cents, bill)


def outlook(
    *,
    period_start: date,
    start_day: int,
    bills: Iterable[Due],
    saved_cents: int,
    monthly_cents: int = 0,
    put_aside_cents: int = 0,
    added: Callable[[int], int] | None = None,
) -> Outlook | None:
    """Every budget month holding a bill to pay, checked against all the bills
    due up to its end. None when there is nothing to pay."""
    months: dict[date, list[Due]] = {}
    for b in sorted(bills, key=lambda b: b.due):
        months.setdefault(recurrence.month_period(b.due, start_day)[0], []).append(b)
    out: list[DueCheck] = []
    need = 0
    for items in months.values():
        need += sum(b.cents for b in items)
        unpaid = [b for b in items if not b.paid]
        if unpaid:
            out.append(
                check(
                    period_start=period_start,
                    start_day=start_day,
                    due=unpaid[0].due,
                    need_cents=need,
                    saved_cents=saved_cents,
                    monthly_cents=monthly_cents,
                    put_aside_cents=put_aside_cents,
                    bill_cents=sum(b.cents for b in unpaid),
                    added=added,
                )
            )
    return Outlook(tuple(out)) if out else None
