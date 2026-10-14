"""Whether a saved-up budget line will hold enough when its next bill is due.

A line spent every few months saves a month's budget each budget month. Its
next unpaid bill is checked against what it will hold by the budget month that
bill falls in: what it holds now, the budget months still to come before then,
and what was put aside from it into a goal since its last bill (that money is
out of the line but kept for the bill).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from fiscus.domain.goals import months_left


@dataclass(frozen=True)
class DueCheck:
    due: date
    need_cents: int  # unpaid bills due from this budget month to the one holding `due`
    by_then_cents: int  # what the line will hold by then
    saved_cents: int  # what it holds in this budget month

    @property
    def short_cents(self) -> int:
        return max(0, self.need_cents - self.by_then_cents)

    @property
    def lined_up_cents(self) -> int:
        """What the line would hold in this budget month to have just enough
        by then: the bills less the months still to come and what was put
        aside."""
        return max(0, self.saved_cents + self.need_cents - self.by_then_cents)


def check(
    *,
    period_start: date,
    start_day: int,
    due: date,
    need_cents: int,
    saved_cents: int,
    monthly_cents: int,
    put_aside_cents: int = 0,
) -> DueCheck:
    """`saved_cents` is what the line holds to the end of the budget month
    starting `period_start`; each later month before the one holding `due`
    adds `monthly_cents`."""
    ahead = max(0, months_left(period_start, due, start_day) - 1)
    by_then = saved_cents + ahead * monthly_cents + put_aside_cents
    return DueCheck(due, need_cents, by_then, saved_cents)
