"""Small helpers for reading HTML form values."""

from __future__ import annotations

from datetime import date

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
        return round(float(value) * 100)
    except ValueError as exc:
        raise FormError(f'{label}: enter a percentage') from exc


def rate(value: str, label: str) -> str:
    from decimal import Decimal, InvalidOperation

    try:
        r = Decimal(value.strip().rstrip('%'))
    except InvalidOperation as exc:
        raise FormError(f'{label}: enter a rate such as 6.24') from exc
    if not 0 <= r < 50:
        raise FormError(f'{label}: enter a yearly rate between 0 and 50')
    return str(r.normalize()) if r != 0 else '0'
