"""Small helpers for reading HTML form values."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from fiscus.money import MoneyError, parse_cents


class FormError(ValueError):
    pass


# Any real date, and far from date.min and date.max, which recurring dates,
# the calendar and the forecast step past.
EARLIEST = date(1900, 1, 1)
LATEST = date(2199, 12, 31)


def iso_date(value: str | None) -> date | None:
    """A YYYY-MM-DD date from EARLIEST to LATEST, else None: for dates in a
    URL, where a bad one means the default."""
    try:
        day = date.fromisoformat((value or '').strip())
    except ValueError:
        return None
    return day if EARLIEST <= day <= LATEST else None


def req_date(value: str, label: str) -> date:
    day = iso_date(value)
    if day is None:
        raise FormError(f'{label}: enter a date from {EARLIEST.year} to {LATEST.year}')
    return day


def opt_date(value: str | None, label: str) -> date | None:
    if value is None or not value.strip():
        return None
    return req_date(value, label)


def ends_after(start: date, end: date | None, label: str = 'The end date') -> date | None:
    """`end`, refused when it is before `start`."""
    if end is not None and end < start:
        raise FormError(f'{label} is before the start')
    return end


def int_or(value: object, default: int = 0) -> int:
    """A whole number from a form or query value, else `default`."""
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


def money(value: str, label: str, *, allow_negative: bool = True) -> int:
    try:
        return parse_cents(value, allow_negative=allow_negative)
    except MoneyError as exc:
        raise FormError(f'{label}: {exc}') from exc


def choice(value: str, options: tuple[str, ...], label: str) -> str:
    if value not in options:
        raise FormError(f'{label}: choose one of the options')
    return value


def text(value: str, label: str, max_len: int = 120) -> str:
    v = value.strip()
    if not v:
        raise FormError(f'{label} is required')
    return v[:max_len]


def rate(value: str, label: str) -> str:
    try:
        r = Decimal(value.strip().rstrip('%'))
    except InvalidOperation as exc:
        raise FormError(f'{label}: enter a rate such as 6.24') from exc
    if not r.is_finite() or not 0 <= r < 50:
        raise FormError(f'{label}: enter a yearly rate between 0 and 50')
    return str(r.normalize()) if r != 0 else '0'
