"""Pay: what a casual job is expected to pay, and which payday a payslip records.

The average received per payday over a recent window (thirteen weeks, or a
year for a quarterly pay). Paydays with no
deposit count as nothing paid, so a week without shifts lowers the average
rather than being ignored. The window starts at the first deposit seen in it,
so a job that began recently is not averaged over weeks it did not exist.

A payslip can record a payday of a schedule. It then stands in for that
payday's expected pay everywhere (D27).

Irregular pay (D74), from a business say, has no paydays: each payment is a
payslip on it, whatever the day, and it is expected to bring in a typical
month, the average of the last year.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction

from fiscus.domain import recurrence
from fiscus.money import round_div

WINDOW_DAYS = 91  # thirteen weeks
# Paid every few months: a year, so the average covers four paydays.
LONG_WINDOW_DAYS = 365


def window_days(frequency: str) -> int:
    return LONG_WINDOW_DAYS if frequency == 'quarterly' else WINDOW_DAYS


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
    window_start = today - timedelta(days=window_days(frequency))
    seen = [(d, c) for d, c in deposits if window_start < d <= end and c > 0]
    if not seen or frequency == 'one_off':
        return fallback_cents
    start = max(window_start, min(d for d, _c in seen) - timedelta(days=SLACK_DAYS))
    paydays = sum(
        1 for _d in recurrence.occurrences(anchor, frequency, start, end, not_before=start)
    )
    return round_div(sum(c for _d, c in seen), max(paydays, 1))


def typical_month(
    start: date, paid: Iterable[tuple[date, int]], today: date, fallback_cents: int
) -> int:
    """What irregular pay brings in a typical month (D74): what it paid over
    the last year, today included, a month's share of it. The year starts no
    earlier than the pay did (its start, or its first payment if that came
    first), and counts as a month at least, so one payment soon after it
    starts is a month's pay rather than thirty. Until it has paid anything in
    the year, `fallback_cents`, the typical month typed in."""
    window_start = today - timedelta(days=LONG_WINDOW_DAYS)
    seen = [(d, c) for d, c in paid if window_start < d <= today]
    if not any(c > 0 for _d, c in seen):
        return fallback_cents
    first = max(window_start + timedelta(days=1), min([start, *(d for d, _c in seen)]))
    days = (today - first).days + 1
    return round_div(sum(c for _d, c in seen) * 365, max(days * 12, 365))


def spread(monthly_cents: int, first: date, last: date) -> list[tuple[date, int]]:
    """A month's amount over each day from `first` to `last`, a twelfth of a
    365-day year a day, fractions of a cent carried so the days add up. Days
    that come to nothing are left out."""
    per_day = Fraction(monthly_cents * 12, 365)
    carry = Fraction(0)
    out: list[tuple[date, int]] = []
    day = first
    while day <= last:
        carry += per_day
        whole = round_div(carry.numerator, carry.denominator)
        if whole:
            carry -= whole
            out.append((day, whole))
        day += timedelta(days=1)
    return out


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
class IrregularPay:
    """Irregular pay a payslip can go on, on any day (D74)."""

    schedule_id: int
    name: str
    employers: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f'pay:{self.schedule_id}'


@dataclass(frozen=True)
class Unsure:
    """Several jobs are paid around a payslip's date and its employer names none."""

    choices: tuple[OpenPayday, ...]


def _names(employer: str, op: OpenPayday | IrregularPay) -> bool:
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


def match_irregular(employer: str, pays: Sequence[IrregularPay]) -> IrregularPay | None:
    """The irregular pay a payslip no payday matched is for: the one its
    employer names, or an earlier payment's employer does. None when none
    does, or more than one, so a bonus is not taken for business pay."""
    named = [p for p in pays if _names(employer, p)]
    return named[0] if len(named) == 1 else None
