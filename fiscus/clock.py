"""The one place that knows what day it is.

Every "today" in Fiscus is the household's date in FISCUS_TIMEZONE, never the
container's UTC date. Tests replace the clock rather than patching datetime.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo


class Clock:
    def __init__(self, tz: ZoneInfo, fixed: datetime | None = None) -> None:
        self.tz = tz
        self._fixed = fixed

    def now(self) -> datetime:
        """Aware UTC now."""
        if self._fixed is not None:
            return self._fixed.astimezone(UTC)
        return datetime.now(UTC)

    def local_now(self) -> datetime:
        return self.now().astimezone(self.tz)

    def today(self) -> date:
        return self.local_now().date()

    def set(self, when: datetime) -> None:
        self._fixed = when


def utcnow_naive(clock: Clock) -> datetime:
    """SQLite stores naive datetimes; Fiscus stores them as UTC."""
    return clock.now().replace(tzinfo=None)
