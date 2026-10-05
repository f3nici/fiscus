"""Savings goals: money set aside inside an account, and how much a month reaches a target.

A goal holds what has been put into it (its moves added up). With a target
and a date, the budget asks for an even share each budget month: what is
still to save at the start of the month, over the budget months left to the
one holding the date. Asking from the start of the month keeps the figure
steady while money goes in during it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from fiscus.domain import recurrence


def months_left(period_start: date, target: date, start_day: int) -> int:
    """Budget months from the one starting `period_start` to the one holding
    `target`, both counted. 0 when the target is before the period."""
    if target < period_start:
        return 0
    n, p = 0, period_start
    while p <= target:
        n += 1
        p = recurrence.add_months(p, 1, start_day)
    return n


def per_month(remaining_cents: int, months: int) -> int:
    """An even monthly share of what is left, rounded up so the target is met."""
    if remaining_cents <= 0 or months <= 0:
        return 0
    return -(-remaining_cents // months)


@dataclass(frozen=True)
class Progress:
    held_cents: int
    target_cents: int | None

    @property
    def remaining_cents(self) -> int:
        if self.target_cents is None:
            return 0
        return max(0, self.target_cents - self.held_cents)

    @property
    def reached(self) -> bool:
        return self.target_cents is not None and self.held_cents >= self.target_cents

    @property
    def pct(self) -> int:
        if not self.target_cents:
            return 0
        return max(0, min(100, self.held_cents * 100 // self.target_cents))


def unallocated(balance_cents: int, held: list[int]) -> int:
    """What is left in an account once its goals are taken out. Negative when
    more is set aside than the account holds."""
    return balance_cents - sum(held)
