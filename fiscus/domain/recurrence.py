"""Recurring dates.

One expansion is shared by the calendar, the forecast, bill status and the
reminders, so the four can never disagree about when something is due.

Monthly, quarterly and yearly keep the anchor's day of month and clamp to the
last day of a shorter month: an anchor on 31 January falls on 28 or 29
February, then back on 31 March.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterator
from datetime import date, timedelta

DAYS = {'weekly': 7, 'fortnightly': 14}
MONTHS = {'monthly': 1, 'quarterly': 3, 'yearly': 12}

# Occurrences per year, for normalising amounts to a month.
PER_YEAR = {
    'weekly': 52,
    'fortnightly': 26,
    'monthly': 12,
    'quarterly': 4,
    'yearly': 1,
}


def add_months(d: date, months: int, anchor_day: int | None = None) -> date:
    day = anchor_day if anchor_day is not None else d.day
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last))


def nth(anchor: date, frequency: str, n: int) -> date:
    """The n-th occurrence (0 is the anchor). n may be negative."""
    if frequency in DAYS:
        return anchor + timedelta(days=DAYS[frequency] * n)
    if frequency in MONTHS:
        return add_months(anchor, MONTHS[frequency] * n, anchor.day)
    if frequency == 'one_off':
        if n != 0:
            raise ValueError('a one-off has a single occurrence')
        return anchor
    raise ValueError(f'unknown frequency {frequency!r}')


def occurrences(
    anchor: date,
    frequency: str,
    start: date,
    end: date,
    *,
    until: date | None = None,
    not_before: date | None = None,
) -> Iterator[date]:
    """Occurrences in [start, end], never after `until` and never before `not_before`.

    `not_before` defaults to the anchor: a schedule does not run backwards.
    """
    floor = anchor if not_before is None else not_before
    lo = max(start, floor)
    hi = end if until is None else min(end, until)
    if lo > hi:
        return
    if frequency == 'one_off':
        if lo <= anchor <= hi:
            yield anchor
        return
    if frequency in DAYS:
        step = DAYS[frequency]
        # ceil((lo - anchor) / step), for either sign
        n = -(-(lo - anchor).days // step)
    else:
        step_m = MONTHS[frequency]
        months = (lo.year - anchor.year) * 12 + (lo.month - anchor.month)
        n = months // step_m - 1
    while True:
        d = nth(anchor, frequency, n)
        if d > hi:
            return
        if d >= lo:
            yield d
        n += 1


def next_on_or_after(anchor: date, frequency: str, day: date) -> date | None:
    for d in occurrences(anchor, frequency, day, day + timedelta(days=400)):
        return d
    return None


def monthly_equivalent(amount_cents: int, frequency: str) -> int:
    """Normalise to a month: weekly x 52 / 12 and so on. One-offs are not monthly."""
    if frequency == 'one_off':
        return 0
    per_year = PER_YEAR[frequency]
    total = amount_cents * per_year
    q, r = divmod(abs(total), 12)
    if r * 2 >= 12:
        q += 1
    return q if total >= 0 else -q
