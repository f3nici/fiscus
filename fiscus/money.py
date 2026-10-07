"""Money is integer cents. Never a float."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


class MoneyError(ValueError):
    pass


def parse_cents(text: str | int | Decimal | None, *, allow_negative: bool = True) -> int:
    """Parse "$1,234.50", "-12.3", "(45.00)" or "12" into cents."""
    if text is None:
        raise MoneyError('An amount is required')
    if isinstance(text, int):
        return text * 100
    if isinstance(text, Decimal):
        value = text
    else:
        raw = text.strip().replace(',', '').replace('$', '').replace(' ', '')
        negative = False
        if raw.startswith('(') and raw.endswith(')'):
            negative, raw = True, raw[1:-1]
        if raw.endswith('-'):
            negative, raw = True, raw[:-1]
        if raw.startswith('+'):
            raw = raw[1:]
        if raw == '':
            raise MoneyError('An amount is required')
        if 'e' in raw.lower():
            # Decimal reads "1e3" as 1000; nobody means that in an amount.
            raise MoneyError(f'"{text}" is not an amount')
        try:
            value = Decimal(raw)
        except InvalidOperation as exc:
            raise MoneyError(f'"{text}" is not an amount') from exc
        if negative:
            value = -value
    if not value.is_finite():
        raise MoneyError('Amount must be a number')
    try:
        cents = int((value * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except InvalidOperation as exc:
        raise MoneyError('Amount is too large') from exc
    if not allow_negative and cents < 0:
        raise MoneyError('Amount cannot be negative')
    if abs(cents) > 100_000_000_000:
        raise MoneyError('Amount is too large')
    return cents


def to_decimal(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(Decimal('0.01'))


def fmt(cents: int | None, *, sign: bool = False, symbol: bool = True) -> str:
    """Format cents as "$1,234.50" / "-$12.00"."""
    if cents is None:
        return ''
    negative = cents < 0
    whole, part = divmod(abs(cents), 100)
    body = f'{"$" if symbol else ""}{whole:,}.{part:02d}'
    if negative:
        return '-' + body
    if sign and cents > 0:
        return '+' + body
    return body


def fmt_plain(cents: int) -> str:
    """Form-field value: "1234.50"."""
    return str(to_decimal(cents))


def round_div(numerator: int, denominator: int) -> int:
    """Integer division rounding half away from zero."""
    if denominator == 0:
        raise ZeroDivisionError
    q = Decimal(numerator) / Decimal(denominator)
    return int(q.quantize(Decimal('1'), rounding=ROUND_HALF_UP))
