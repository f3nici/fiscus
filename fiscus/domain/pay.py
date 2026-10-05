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
    # Employers on the schedule's earlier payslips; they name it as well.
    employers: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f'{self.schedule_id}:{self.payday.isoformat()}'


@dataclass(frozen=True)
class Unsure:
    """Several jobs are paid around a payslip's date and its employer names none."""

    choices: tuple[OpenPayday, ...]


def _names(employer: str, op: OpenPayday) -> bool:
    emp = employer.casefold().strip()
    for n in (op.name, *op.employers):
        name = n.casefold().strip()
        if emp and name and (name in emp or emp in name):
            return True
    return False


def match_paydays(
    slips: Sequence[tuple[date, str]], open_paydays: Sequence[OpenPayday]
) -> list[OpenPayday | Unsure | None]:
    """The open payday each payslip (date, employer) records, or None for a one-off.

    Within MATCH_DAYS of the payslip's date; a schedule named like the
    employer (or by an earlier payslip's employer) comes first, then the
    nearest. Each payday is taken once. When the nearest open paydays belong
    to more than one job and none is named, the payslip is Unsure rather
    than given to whichever job sorts first.
    """
    pairs: list[tuple[bool, int, int, int]] = []
    for i, (when, employer) in enumerate(slips):
        for j, op in enumerate(open_paydays):
            gap = abs((op.payday - when).days)
            if gap > MATCH_DAYS:
                continue
            pairs.append((not _names(employer, op), gap, i, j))
    out: list[OpenPayday | Unsure | None] = [None] * len(slips)
    used: set[int] = set()
    for unnamed, gap, i, j in sorted(pairs):
        if out[i] is not None or j in used:
            continue
        if unnamed:
            rivals = tuple(
                open_paydays[k]
                for u, g, i2, k in sorted(pairs)
                if i2 == i and u and g == gap and k not in used
            )
            if len({r.schedule_id for r in rivals}) > 1:
                out[i] = Unsure(rivals)
                continue
        out[i] = open_paydays[j]
        used.add(j)
    return out
