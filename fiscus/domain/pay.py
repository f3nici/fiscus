"""Pay that varies: what to expect from a casual job each payday.

The average received per payday over a recent window. Paydays with no
deposit count as nothing paid, so a week without shifts lowers the average
rather than being ignored. The window starts at the first deposit seen in it,
so a job that began recently is not averaged over weeks it did not exist.
"""

from __future__ import annotations

from collections.abc import Iterable
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
