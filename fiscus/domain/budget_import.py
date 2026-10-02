"""Reading a budget spreadsheet into monthly budget lines.

Built for the shape household budgets usually take: a header row with Item,
Amount and Frequency (and often Notes), section headings on rows of their own
("HOME & PROPERTY"), and total rows that are not lines. Each line becomes a
monthly amount using the same normalisation as everywhere else (weekly x 52 /
12 and so on). One-off costs are listed but never budgeted monthly.

Cells arrive as whatever the reader produced: str, int, float, None. Numbers
are turned into cents through Decimal straight away and never kept as floats.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from fiscus.domain import recurrence
from fiscus.money import MoneyError, parse_cents

HEADER_WORDS = {
    'item': ('item', 'name', 'description', 'expense', 'line'),
    'amount': ('amount', 'cost', 'value', 'price'),
    'frequency': ('frequency', 'how often', 'freq', 'period', 'every'),
    'notes': ('notes', 'note', 'comment', 'comments', 'details'),
}

FREQUENCIES = {
    'weekly': 'weekly',
    'week': 'weekly',
    'fortnightly': 'fortnightly',
    'fortnight': 'fortnightly',
    'monthly': 'monthly',
    'month': 'monthly',
    'quarterly': 'quarterly',
    'quarter': 'quarterly',
    'annual': 'yearly',
    'annually': 'yearly',
    'yearly': 'yearly',
    'year': 'yearly',
    'daily': 'daily',
    'day': 'daily',
    'one-off': 'one_off',
    'one off': 'one_off',
    'oneoff': 'one_off',
    'once': 'one_off',
}

FREQ_LABELS = {
    'weekly': 'Weekly',
    'fortnightly': 'Fortnightly',
    'monthly': 'Monthly',
    'quarterly': 'Quarterly',
    'yearly': 'Yearly',
    'daily': 'Daily',
    'one_off': 'One-off',
}

MORTGAGE = re.compile(r'\bmortgage\b|home\s*loan\s*repayment', re.IGNORECASE)
# A total row starts with the word; a heading may mention a total in passing
# ("ONE-OFF COSTS, not part of the monthly total above").
TOTAL = re.compile(r'^\s*(sub)?totals?\b|^\s*summary\b', re.IGNORECASE)


@dataclass(frozen=True)
class Header:
    row: int
    item: int
    amount: int
    frequency: int
    notes: int | None


@dataclass
class Line:
    index: int
    row: int  # 1-based, as the spreadsheet shows it
    section: str
    name: str
    amount_cents: int | None
    frequency: str | None
    monthly_cents: int | None
    notes: str
    include: bool = True
    reason: str = ''  # why it is excluded or needs attention

    @property
    def frequency_label(self) -> str:
        return FREQ_LABELS.get(self.frequency or '', '')


def _text(value: Any) -> str:
    if value is None:
        return ''
    return ' '.join(str(value).split())


def _norm(value: Any) -> str:
    return re.sub(r'[^a-z ]', '', _text(value).lower()).strip()


def find_header(rows: list[list[Any]], scan: int = 40) -> Header | None:
    for r, row in enumerate(rows[:scan]):
        found: dict[str, int] = {}
        for c, cell in enumerate(row):
            text = _norm(cell)
            if not text:
                continue
            for key, words in HEADER_WORDS.items():
                if key not in found and any(text == w or text.startswith(w + ' ') for w in words):
                    found[key] = c
                    break
        if {'item', 'amount', 'frequency'} <= found.keys():
            return Header(r, found['item'], found['amount'], found['frequency'], found.get('notes'))
    return None


def _cell(row: list[Any], idx: int | None) -> Any:
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def to_cents(value: Any) -> int | None:
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, int | float):
            # Through repr, so 1253.92 is 1253.92 and not its binary neighbour.
            return parse_cents(Decimal(repr(value)), allow_negative=False)
        return parse_cents(str(value), allow_negative=False)
    except (MoneyError, InvalidOperation, ValueError):
        return None


def monthly(amount_cents: int, frequency: str) -> int:
    if frequency == 'daily':
        q, r = divmod(amount_cents * 365, 12)
        return q + (1 if r * 2 >= 12 else 0)
    return recurrence.monthly_equivalent(amount_cents, frequency)


def section_title(text: str) -> str:
    t = _text(text).strip(' :-')
    # Keep the heading, drop a trailing explanation: "ONE-OFF COSTS — paid once".
    t = re.split(r'\s+[—–-]\s+|\s*\(', t, maxsplit=1)[0].strip()
    # "HOME & PROPERTY" reads better as "Home & property".
    return t[:1].upper() + t[1:].lower() if t.isupper() else t


def parse(rows: list[list[Any]], header: Header, *, has_loan: bool = False) -> list[Line]:
    lines: list[Line] = []
    section = ''
    for r in range(header.row + 1, len(rows)):
        row = rows[r]
        name = _text(_cell(row, header.item))
        if not name:
            continue
        raw_amount = _cell(row, header.amount)
        raw_freq = _norm(_cell(row, header.frequency))
        freq = FREQUENCIES.get(raw_freq)
        if raw_freq == '' and (raw_amount is None or raw_amount == ''):
            if not TOTAL.search(name):
                section = section_title(name)
            continue
        if TOTAL.search(name) and freq is None:
            continue
        amount = to_cents(raw_amount)
        notes = _text(_cell(row, header.notes))[:300]
        line = Line(len(lines), r + 1, section, name[:120], amount, freq, None, notes)
        if freq is None:
            line.include = False
            line.reason = f'Frequency "{_text(_cell(row, header.frequency))}" not recognised'
        elif freq == 'one_off':
            line.include = False
            line.reason = 'One-off, not a monthly cost'
        elif amount is None:
            line.include = False
            line.reason = 'No amount in the file: enter the monthly figure to include it'
        else:
            line.monthly_cents = monthly(amount, freq)
            if has_loan and MORTGAGE.search(name):
                line.include = False
                line.reason = 'Your home loan already tracks this repayment'
        lines.append(line)
    return lines
