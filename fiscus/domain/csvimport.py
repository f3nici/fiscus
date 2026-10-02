"""Bank CSV parsing and de-duplication.

ANZ exports are headerless: date (DD/MM/YYYY), signed amount, description, and
sometimes a trailing balance. That shape is detected automatically; anything
else goes through a column mapping saved per account.

De-duplication hashes account + date + amount + description + occurrence
index. The index counts identical (date, amount, description) rows earlier in
the same file, so two genuine identical coffees on one day are both kept,
while the same day appearing in two overlapping exports collapses to one.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from fiscus.money import MoneyError, parse_cents

DATE_FORMATS = {
    'DD/MM/YYYY': '%d/%m/%Y',
    'D/M/YY': '%d/%m/%y',
    'YYYY-MM-DD': '%Y-%m-%d',
    'MM/DD/YYYY': '%m/%d/%Y',
    'DD-MM-YYYY': '%d-%m-%Y',
    'D MMM YYYY': '%d %b %Y',
}

_ANZ_DATE = re.compile(r'^\d{1,2}/\d{1,2}/\d{4}$')
_AMOUNT = re.compile(r'^[-+]?\$?[\d,]*\.?\d+$')
MAX_ROWS = 50_000
MAX_BYTES = 10 * 1024 * 1024


class ImportError_(ValueError):
    """A file that cannot be imported, with a message fit for a person."""


@dataclass(frozen=True)
class ParsedRow:
    line: int
    date: date
    amount_cents: int
    description: str


@dataclass
class ParseResult:
    rows: list[ParsedRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Mapping:
    has_header: bool = False
    date_col: int = 0
    date_format: str = 'DD/MM/YYYY'
    amount_col: int | None = 1
    debit_col: int | None = None
    credit_col: int | None = None
    description_cols: tuple[int, ...] = (2,)
    invert: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Mapping:
        def opt(key: str) -> int | None:
            v = raw.get(key)
            return None if v in (None, '', -1, '-1') else int(v)

        cols = raw.get('description_cols') or [2]
        fmt = str(raw.get('date_format', 'DD/MM/YYYY'))
        if fmt not in DATE_FORMATS:
            raise ImportError_(f'Unknown date format {fmt}')
        m = cls(
            has_header=bool(raw.get('has_header', False)),
            date_col=int(raw.get('date_col', 0)),
            date_format=fmt,
            amount_col=opt('amount_col'),
            debit_col=opt('debit_col'),
            credit_col=opt('credit_col'),
            description_cols=tuple(int(c) for c in cols),
            invert=bool(raw.get('invert', False)),
        )
        if m.amount_col is None and m.debit_col is None and m.credit_col is None:
            raise ImportError_('Choose an amount column, or debit and credit columns')
        return m

    def to_dict(self) -> dict[str, Any]:
        return {
            'has_header': self.has_header,
            'date_col': self.date_col,
            'date_format': self.date_format,
            'amount_col': self.amount_col,
            'debit_col': self.debit_col,
            'credit_col': self.credit_col,
            'description_cols': list(self.description_cols),
            'invert': self.invert,
        }


ANZ = Mapping()


def decode(data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise ImportError_('That file is larger than 10 MB')
    if b'\x00' in data[:8192]:
        raise ImportError_('The file is not text')
    for enc in ('utf-8-sig', 'cp1252'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ImportError_('The file is not text')


def read_rows(text: str) -> list[list[str]]:
    sample = text[:4096]
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=',;\t')
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if len(rows) > MAX_ROWS:
        raise ImportError_(f'That file has more than {MAX_ROWS:,} rows')
    return rows


def is_anz(rows: list[list[str]]) -> bool:
    """Headerless, date first as DD/MM/YYYY, signed amount second, text third."""
    probe = rows[:20]
    if not probe:
        return False
    for r in probe:
        if len(r) < 3:
            return False
        if not _ANZ_DATE.match(r[0].strip()):
            return False
        if not _AMOUNT.match(r[1].strip().replace(' ', '')):
            return False
    return True


def normalise_description(text: str) -> str:
    return ' '.join(text.split())


def _parse_date(text: str, fmt: str) -> date:
    return datetime.strptime(text.strip(), DATE_FORMATS[fmt]).date()  # noqa: DTZ007


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row) or idx < 0:
        return ''
    return row[idx].strip()


def parse(rows: list[list[str]], mapping: Mapping) -> ParseResult:
    out = ParseResult()
    body = rows[1:] if mapping.has_header else rows
    first_line = 2 if mapping.has_header else 1
    for i, row in enumerate(body):
        line = first_line + i
        try:
            when = _parse_date(_cell(row, mapping.date_col), mapping.date_format)
        except (ValueError, KeyError):
            out.errors.append(f'Line {line}: "{_cell(row, mapping.date_col)}" is not a date')
            continue
        try:
            if mapping.amount_col is not None:
                amount = parse_cents(_cell(row, mapping.amount_col))
            else:
                debit = _cell(row, mapping.debit_col)
                credit = _cell(row, mapping.credit_col)
                amount = (parse_cents(credit) if credit else 0) - (
                    abs(parse_cents(debit)) if debit else 0
                )
        except MoneyError:
            out.errors.append(f'Line {line}: no amount')
            continue
        if mapping.invert:
            amount = -amount
        desc = normalise_description(' '.join(_cell(row, c) for c in mapping.description_cols))
        out.rows.append(ParsedRow(line, when, amount, desc[:500]))
    return out


def dedup_hashes(account_id: int, rows: list[ParsedRow]) -> list[str]:
    seen: Counter[tuple[date, int, str]] = Counter()
    hashes: list[str] = []
    for r in rows:
        key = (r.date, r.amount_cents, r.description)
        n = seen[key]
        seen[key] += 1
        hashes.append(row_hash(account_id, r.date, r.amount_cents, r.description, n))
    return hashes


def row_hash(account_id: int, when: date, amount_cents: int, description: str, n: int) -> str:
    raw = f'{account_id}|{when.isoformat()}|{amount_cents}|{normalise_description(description)}|{n}'
    return hashlib.sha256(raw.encode()).hexdigest()


def csv_safe(value: object) -> str:
    """Spreadsheets run a cell starting =, +, -, @ as a formula. Prefix those."""
    text = '' if value is None else str(value)
    if text and text[0] in '=+-@\t\r':
        return "'" + text
    return text
