"""Pay: what a casual job is expected to pay, and which payday a payslip records.

The average received per payday over a recent window. Paydays with no
deposit count as nothing paid, so a week without shifts lowers the average
rather than being ignored. The window starts at the first deposit seen in it,
so a job that began recently is not averaged over weeks it did not exist.

A payslip can record a payday of a schedule. It then stands in for that
payday's expected pay everywhere (D27).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from fiscus.domain import recurrence

WINDOW_DAYS = 91  # thirteen weeks
# A deposit can land a day or two either side of the payday.
SLACK_DAYS = 3


def expected_pay(
    anchor: date,
    frequency: str,
    deposits: Iterable[tuple[date, int]],
    today: date,
    fallback_cents: int,
) -> int:
    """Average net pay per payday, or `fallback_cents` when nothing has been paid yet."""
    # Up to yesterday: today's pay may not have landed or been imported yet.
    end = today - timedelta(days=1)
    window_start = today - timedelta(days=WINDOW_DAYS)
    seen = [(d, c) for d, c in deposits if window_start < d <= end and c > 0]
    if not seen or frequency == 'one_off':
        return fallback_cents
    start = max(window_start, min(d for d, _c in seen) - timedelta(days=SLACK_DAYS))
    paydays = sum(
        1 for _d in recurrence.occurrences(anchor, frequency, start, end, not_before=start)
    )
    return round(sum(c for _d, c in seen) / max(paydays, 1))


# ------------------------------------------------------------------ payslips

# Paydays this far back without a payslip are asked for; older ones are not.
ASK_DAYS = 56
# A payslip's payment date can sit a few days off its schedule's payday.
MATCH_DAYS = 3
# A payslip recorded against a payday is dated within this of it.
LINK_DAYS = 31


@dataclass(frozen=True)
class OpenPayday:
    schedule_id: int
    payday: date
    name: str

    @property
    def key(self) -> str:
        return f'{self.schedule_id}:{self.payday.isoformat()}'


def match_paydays(
    slips: Sequence[tuple[date, str]], open_paydays: Sequence[OpenPayday]
) -> list[OpenPayday | None]:
    """The open payday each payslip (date, employer) records, or None for a one-off.

    Within MATCH_DAYS of the payslip's date; a schedule whose name and the
    employer contain one another comes first, then the nearest. Each payday is
    taken once.
    """
    pairs: list[tuple[bool, int, int, int]] = []
    for i, (when, employer) in enumerate(slips):
        emp = employer.casefold().strip()
        for j, op in enumerate(open_paydays):
            gap = abs((op.payday - when).days)
            if gap > MATCH_DAYS:
                continue
            name = op.name.casefold().strip()
            named = bool(emp and name and (name in emp or emp in name))
            pairs.append((not named, gap, i, j))
    out: list[OpenPayday | None] = [None] * len(slips)
    used: set[int] = set()
    for _named, _gap, i, j in sorted(pairs):
        if out[i] is None and j not in used:
            out[i] = open_paydays[j]
            used.add(j)
    return out
