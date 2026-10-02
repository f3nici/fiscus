"""Matching imported transactions to bill occurrences.

A transaction pays an occurrence when it is money out of the right account,
its size is within the bill's tolerance of what this person owes, its date is
within the window either side of the due date, and its description contains
the hint (when there is one). The closest date wins; a tie goes to the earlier
occurrence so an old overdue one is cleared first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class OpenOccurrence:
    bill_id: int
    due_date: date
    expected_cents: int  # positive: what leaves the account
    account_id: int | None
    tolerance_pct: int
    window_days: int
    hint: str


@dataclass(frozen=True)
class Candidate:
    transaction_id: int
    account_id: int
    date: date
    amount_cents: int  # signed as the bank has it
    description: str


def amount_ok(expected: int, actual_out: int, tolerance_pct: int) -> bool:
    allowed = max(100, expected * tolerance_pct // 100)
    return abs(actual_out - expected) <= allowed


def matches(occ: OpenOccurrence, txn: Candidate) -> bool:
    if txn.amount_cents >= 0:
        return False
    if occ.account_id is not None and occ.account_id != txn.account_id:
        return False
    if abs((txn.date - occ.due_date).days) > occ.window_days:
        return False
    if not amount_ok(occ.expected_cents, -txn.amount_cents, occ.tolerance_pct):
        return False
    return not (occ.hint and occ.hint.casefold() not in txn.description.casefold())


def assign(
    occurrences: list[OpenOccurrence], candidates: list[Candidate]
) -> list[tuple[OpenOccurrence, Candidate]]:
    """Greedy one-to-one assignment, transactions in date order."""
    taken: set[tuple[int, date]] = set()
    pairs: list[tuple[OpenOccurrence, Candidate]] = []
    for txn in sorted(candidates, key=lambda c: (c.date, c.transaction_id)):
        options = [
            o for o in occurrences if (o.bill_id, o.due_date) not in taken and matches(o, txn)
        ]
        if not options:
            continue
        best = min(options, key=lambda o: (abs((txn.date - o.due_date).days), o.due_date))
        taken.add((best.bill_id, best.due_date))
        pairs.append((best, txn))
    return pairs
