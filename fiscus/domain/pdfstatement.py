"""Bank statement PDFs into a table the CSV import can read.

A statement is a table drawn as positioned text. Characters are grouped into
lines by baseline and into cells by the gaps between them. A line that starts
with a date is a transaction. Amounts are put into columns by their right
edge, which is how statements align them, and header words (withdrawals,
deposits, balance...) name those columns, so common layouts need no mapping.
Text lines just below a transaction continue its description.

Dates without a year ("02 SEP") take it from the latest full date in the
statement, normally the end of the period.

The result is a table with a header row: Date (YYYY-MM-DD), Description, then
one column per amount position. Parsing bytes is the only work; no I/O.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from fiscus.domain.csvimport import ImportError_, Mapping

MAX_PAGES = 100

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
    )
}
_MON = r'(?P<mon>[A-Za-z]{3,9})\.?'
_DATE_PATTERNS = [
    re.compile(r'(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})'),
    re.compile(r'(?P<d>\d{1,2})[/.-](?P<m>\d{1,2})[/.-](?P<y>\d{4}|\d{2})(?![\d/])'),
    re.compile(r'(?P<d>\d{1,2})-' + _MON + r'(?:-(?P<y>\d{4}|\d{2}))?(?![\d/.,])'),
    re.compile(r'(?P<d>\d{1,2}) ' + _MON + r'(?:,? (?P<y>\d{4}))?(?![\d/.,])'),
    re.compile(_MON + r' (?P<d>\d{1,2})(?:,? (?P<y>\d{4}))?(?![\d/.,])'),
    re.compile(r'(?P<d>\d{1,2})/(?P<m>\d{1,2})(?![\d/])'),
]
_AMOUNT = re.compile(
    r'^(?P<neg1>-)?\(?(?P<neg2>-)?\$?(?P<num>\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})\)?'
    r'\s?(?P<suffix>CR|DR|Cr|Dr|cr|dr|-)?$'
)
_MARKER = re.compile(r'^(CR|DR|Cr|Dr|cr|dr)$')
_SKIP = re.compile(
    r'opening balance|closing balance|balance brought forward|balance carried forward|'
    r'brought forward|carried forward|^balance$|^totals?\b',
    re.IGNORECASE,
)
_HEADER_WORDS = [
    ('Balance', ('balance',)),
    ('Debit', ('withdrawal', 'debit', 'money out', 'paid out', 'spent')),
    ('Credit', ('deposit', 'credit', 'money in', 'paid in', 'received')),
    ('Amount', ('amount',)),
]


@dataclass(frozen=True)
class Char:
    text: str
    x0: float
    x1: float
    y: float
    size: float


@dataclass
class Cell:
    text: str
    x0: float
    x1: float


@dataclass
class Line:
    page: int
    y: float
    size: float
    cells: list[Cell]


@dataclass
class _Txn:
    when: date | None
    yearless: tuple[int, int] | None
    desc: list[str]
    desc_x0: float
    amounts: list[Cell]
    page: int
    y: float
    size: float


@dataclass
class Table:
    rows: list[list[str]]
    mapping: Mapping | None


def is_pdf(data: bytes) -> bool:
    return data[:1024].lstrip().startswith(b'%PDF-')


# ---------------------------------------------------------------- extraction


def extract_chars(data: bytes, max_pages: int = MAX_PAGES) -> list[list[Char]]:
    """Every visible character, page by page."""
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTChar
    from pdfminer.pdfdocument import PDFEncryptionError, PDFPasswordIncorrect

    def walk(obj: Any) -> Iterator[Any]:
        if isinstance(obj, LTChar):
            yield obj
            return
        try:
            children = list(obj)
        except TypeError:
            return
        for child in children:
            yield from walk(child)

    pages: list[list[Char]] = []
    try:
        for page in extract_pages(io.BytesIO(data), maxpages=max_pages, laparams=None):
            chars = [
                Char(c.get_text(), c.x0, c.x1, round(c.y0, 1), c.size or 1.0)
                for c in walk(page)
                if c.get_text().strip()
            ]
            pages.append(chars)
    except (PDFPasswordIncorrect, PDFEncryptionError) as exc:
        raise ImportError_(
            'That PDF is password protected. Save an unlocked copy and upload that.'
        ) from exc
    except ImportError_:
        raise
    except Exception as exc:  # pdfminer raises many types for a damaged file
        raise ImportError_('That PDF could not be read') from exc
    return pages


def lines_from_chars(pages: list[list[Char]]) -> list[Line]:
    out: list[Line] = []
    for pno, chars in enumerate(pages):
        rows: list[list[Char]] = []
        for c in sorted(chars, key=lambda c: (-c.y, c.x0)):
            if rows and abs(rows[-1][0].y - c.y) <= max(2.0, c.size * 0.3):
                rows[-1].append(c)
            else:
                rows.append([c])
        for row in rows:
            row.sort(key=lambda c: c.x0)
            size = max(c.size for c in row)
            cells: list[Cell] = []
            prev: Char | None = None
            for c in row:
                gap = c.x0 - prev.x1 if prev else 0.0
                if prev is None or gap > size * 0.8:
                    cells.append(Cell(c.text, c.x0, c.x1))
                else:
                    cell = cells[-1]
                    cell.text += (' ' if gap > size * 0.15 else '') + c.text
                    cell.x1 = c.x1
                prev = c
            out.append(Line(pno, row[0].y, size, cells))
    return out


# --------------------------------------------------------------------- dates


def _month(text: str) -> int | None:
    return _MONTHS.get(text[:3].lower())


def _match_date(text: str) -> tuple[int, int, int | None, int] | None:
    """(day, month, year or None, characters used) for a date at the start of text."""
    for pat in _DATE_PATTERNS:
        m = pat.match(text)
        if not m:
            continue
        g = m.groupdict()
        month = _month(g['mon']) if g.get('mon') else int(g['m'])
        if month is None or not 1 <= month <= 12:
            continue
        day = int(g['d'])
        year = None
        if g.get('y'):
            year = int(g['y'])
            year += 2000 if year < 100 else 0
        try:
            date(year or 2000, month, day)
        except ValueError:
            continue
        end = m.end()
        if end < len(text) and text[end] not in ' \t':
            continue
        return day, month, year, end
    return None


def _full_dates(lines: Iterable[Line]) -> Iterator[date]:
    pat = re.compile(
        r'\b(\d{1,2}[/.-]\d{1,2}[/.-]\d{4}|\d{4}-\d{2}-\d{2}|'
        r'\d{1,2}[ -][A-Za-z]{3,9}\.?[ ,-]+\d{4}|[A-Za-z]{3,9} \d{1,2},? \d{4})\b'
    )
    for line in lines:
        for cell in line.cells:
            for m in pat.finditer(cell.text):
                hit = _match_date(m.group(1))
                if hit and hit[2]:
                    yield date(hit[2], hit[1], hit[0])


def _with_year(day: int, month: int, end: date) -> date:
    for year in (end.year, end.year - 1):
        try:
            when = date(year, month, day)
        except ValueError:  # 29 Feb
            continue
        if when <= end + timedelta(days=7):
            return when
    return date(end.year - 1, month, 28)


# -------------------------------------------------------------------- amounts


def _amount(text: str) -> str | None:
    """A normalised amount: "-12.34" for a debit marker, "+12.34" for credit, else "12.34"."""
    m = _AMOUNT.match(text.strip())
    if not m:
        return None
    num = m['num'].replace(',', '')
    suffix = (m['suffix'] or '').upper()
    negative = bool(m['neg1'] or m['neg2'] or suffix in ('DR', '-')) or (
        text.strip().startswith('(') and text.strip().endswith(')')
    )
    if negative:
        return '-' + num
    if suffix == 'CR':
        return '+' + num
    return num


def _split_amounts(cells: list[Cell]) -> tuple[list[Cell], list[Cell]]:
    """Text cells, then the trailing amount cells with markers folded in."""
    cells = list(cells)
    amounts: list[Cell] = []
    while cells:
        last = cells[-1]
        if _MARKER.match(last.text) and len(cells) > 1 and _amount(cells[-2].text):
            prev = cells[-2]
            cells[-2:] = [Cell(f'{prev.text} {last.text}', prev.x0, last.x1)]
            continue
        if _amount(last.text) is None:
            break
        amounts.insert(0, cells.pop())
    return cells, amounts


# --------------------------------------------------------------------- table


def _header(lines: list[Line]) -> list[tuple[str, float]]:
    """Amount column names and their right edges, from the first header line."""
    for line in lines:
        found: list[tuple[str, float]] = []
        texts = [c.text.lower() for c in line.cells]
        if not any('date' in t for t in texts):
            continue
        for cell, low in zip(line.cells, texts, strict=True):
            for name, words in _HEADER_WORDS:
                if any(w in low for w in words):
                    found.append((name, cell.x1))
                    break
        if found:
            return found
    return []


def _clusters(edges: list[float], tolerance: float) -> list[float]:
    centres: list[list[float]] = []
    for x in sorted(edges):
        if centres and x - centres[-1][-1] <= tolerance:
            centres[-1].append(x)
        else:
            centres.append([x])
    return [sum(c) / len(c) for c in centres]


def _start_txn(line: Line) -> _Txn | None:
    if not line.cells:
        return None
    joined = ' '.join(c.text for c in line.cells[:3])
    hit = _match_date(joined)
    if hit is None:
        return None
    day, month, year, used = hit
    # Drop the characters the date used from the front of the cells.
    cells = [Cell(c.text, c.x0, c.x1) for c in line.cells]
    while used > 0 and cells:
        n = len(cells[0].text)
        if used >= n:
            cells.pop(0)
            used -= n + 1
        else:
            c = cells[0]
            c.x0 += (c.x1 - c.x0) * used / n
            c.text = c.text[used:].strip()
            used = 0
    # A second date column (processed date) is not part of the description.
    if cells:
        second = _match_date(cells[0].text)
        if second and second[3] == len(cells[0].text):
            cells.pop(0)
    text, amounts = _split_amounts(cells)
    when = date(year, month, day) if year else None
    return _Txn(
        when=when,
        yearless=None if year else (day, month),
        desc=[c.text for c in text],
        desc_x0=text[0].x0 if text else (amounts[0].x0 if amounts else line.cells[-1].x1),
        amounts=amounts,
        page=line.page,
        y=line.y,
        size=line.size,
    )


def _continues(txn: _Txn, line: Line) -> bool:
    return (
        line.page == txn.page
        and bool(line.cells)
        and line.cells[0].x0 >= txn.desc_x0 - 3
        and txn.y - line.y <= line.size * 2.6
    )


def build_table(lines: list[Line], today: date) -> Table:
    txns: list[_Txn] = []
    current: _Txn | None = None
    for line in lines:
        txn = _start_txn(line)
        if txn is not None:
            txns.append(txn)
            current = txn
            continue
        if current is not None and _continues(current, line):
            text, amounts = _split_amounts(line.cells)
            if amounts and current.amounts:
                current = None
                continue
            current.desc.extend(c.text for c in text)
            current.amounts.extend(amounts)
            current.y = line.y
            continue
        current = None

    kept = [
        t for t in txns if t.amounts and not _SKIP.search(' '.join(t.desc).strip() or 'balance')
    ]
    if not kept:
        raise ImportError_(
            'No transactions were found in that PDF. A scanned statement has no text to read; '
            'download the statement or a CSV from online banking instead.'
        )

    full = [d for d in _full_dates(lines) if d <= today + timedelta(days=62)]
    end = max(full) if full else today

    header = _header(lines)
    sizes = sorted(t.size for t in kept)
    tolerance = max(6.0, sizes[len(sizes) // 2] * 1.5)
    edges = [a.x1 for t in kept for a in t.amounts]
    centres = _clusters(edges, tolerance)
    columns: list[tuple[str, float]] = []
    named: set[str] = set()
    for name, x1 in header:
        if name not in named:
            columns.append((name, x1))
            named.add(name)
    for c in centres:
        if not any(abs(c - x) <= tolerance * 2 for _n, x in columns):
            columns.append(('', c))
    columns.sort(key=lambda col: col[1])
    names = [n or f'Amount {i + 1}' for i, (n, _x) in enumerate(columns)]

    body: list[list[str]] = []
    for t in kept:
        if t.when is not None:
            when = t.when
        else:
            assert t.yearless is not None
            when = _with_year(*t.yearless, end)
        cells = [''] * len(columns)
        for a in t.amounts:
            idx = min(range(len(columns)), key=lambda i: abs(columns[i][1] - a.x1))
            cells[idx] = _amount(a.text) or ''
        body.append([when.isoformat(), ' '.join(' '.join(t.desc).split()), *cells])

    # A column marked CR for credits shows debits unsigned, and the reverse.
    for i, name in enumerate(names):
        values = [r[2 + i] for r in body if r[2 + i]]
        credits = any(v.startswith('+') for v in values)
        debits = any(v.startswith('-') for v in values)
        for r in body:
            v = r[2 + i]
            if not v:
                continue
            if name != 'Balance' and credits and not debits and v[0] not in '+-':
                v = '-' + v
            r[2 + i] = v.lstrip('+')

    table = [['Date', 'Description', *names], *body]
    return Table(table, guess_mapping(table[0]))


def guess_mapping(header: list[str]) -> Mapping | None:
    """The mapping for a table this module wrote, when its header names the columns."""
    if header[:2] != ['Date', 'Description']:
        return None
    find = {name: i for i, name in enumerate(header)}
    if 'Debit' in find and 'Credit' in find:
        return pdf_mapping(amount_col=None, debit_col=find['Debit'], credit_col=find['Credit'])
    if 'Amount' in find:
        return pdf_mapping(amount_col=find['Amount'])
    return None


def pdf_mapping(**columns: int | None) -> Mapping:
    return Mapping(
        has_header=True,
        date_col=0,
        date_format='YYYY-MM-DD',
        description_cols=(1,),
        **columns,  # type: ignore[arg-type]
    )


def to_table(data: bytes, today: date) -> Table:
    return build_table(lines_from_chars(extract_chars(data)), today)
