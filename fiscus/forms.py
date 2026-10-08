"""Small helpers for reading HTML form values."""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from fiscus.money import MoneyError, parse_cents


class FormError(ValueError):
    pass


def req_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise FormError(f'{label}: enter a date') from exc


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


def opt_money(value: str | None, label: str) -> int | None:
    if value is None or not value.strip():
        return None
    return money(value, label)


def choice(value: str, options: tuple[str, ...], label: str) -> str:
    if value not in options:
        raise FormError(f'{label}: choose one of the options')
    return value


def text(value: str, label: str, max_len: int = 120) -> str:
    v = value.strip()
    if not v:
        raise FormError(f'{label} is required')
    return v[:max_len]


def pct_bp(value: str, label: str) -> int:
    """'12.5' percent to 1250 basis points."""
    try:
        pct = Decimal(value.strip().rstrip('%'))
    except InvalidOperation as exc:
        raise FormError(f'{label}: enter a percentage') from exc
    if not pct.is_finite() or not 0 <= pct <= 100:
        raise FormError(f'{label}: enter a percentage from 0 to 100')
    return int((pct * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def rate(value: str, label: str) -> str:
    try:
        r = Decimal(value.strip().rstrip('%'))
    except InvalidOperation as exc:
        raise FormError(f'{label}: enter a rate such as 6.24') from exc
    if not r.is_finite() or not 0 <= r < 50:
        raise FormError(f'{label}: enter a yearly rate between 0 and 50')
    return str(r.normalize()) if r != 0 else '0'
