"""Payslip PDFs: the pay date, employer, gross, tax withheld and net.

A payslip is read once into values that fill in the payslip form, which the
person checks and saves. Nothing is stored from the file.

Lines and cells come from the statement reader. A figure is found by its
label: the value after a colon in the same cell ("Net Pay: $618.85"), else the
first amount to its right on the line (THIS PAY comes before YTD), else an
amount just below it (boxed layouts). Labels are tried most specific first.
When no gross label is found, the TOTAL line under an earnings heading is used.
Parsing bytes is the only work; no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date

from fiscus.domain.pdftext import Cell, Line, extract_chars, is_pdf, lines_from_chars
from fiscus.domain.uploads import ImportError_
from fiscus.forms import EARLIEST, LATEST
from fiscus.money import MoneyError, parse_cents

MAX_PAGES = 5

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
    )
}
_DATE = re.compile(
    r'(?<!\d)(?:(?P<d1>\d{1,2})[/.-](?P<m1>\d{1,2})[/.-](?P<y1>\d{4}|\d{2})'
    r'|(?P<y2>\d{4})-(?P<m2>\d{2})-(?P<d2>\d{2})'
    r'|(?P<d3>\d{1,2})[ -](?P<mon3>[A-Za-z]{3,9})\.?[ ,-]+(?P<y3>\d{4})'
    r'|(?P<mon4>[A-Za-z]{3,9})\.? (?P<d4>\d{1,2}),? (?P<y4>\d{4}))(?!\d)'
)
_AMOUNT = re.compile(r'(?<![\d.,])\(?-?\$?\s?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}\)?(?![\d%])')
_WHOLE_AMOUNT = re.compile(r'^\(?-?\$?\s?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}\)?$')


def _labels(*names: str) -> list[re.Pattern[str]]:
    return [re.compile(r'^' + n + r'\b\s*:?\s*', re.IGNORECASE) for n in names]


_PAY_DATE = _labels(
    r'payment date', r'pay date', r'date paid', r'paid on', r'payment on', r'pay day', r'payday'
)
_PERIOD = _labels(
    r'pay period', r'period ending', r'period end(?:ing)?(?: date)?', r'week ending', r'period'
)
_GROSS = _labels(
    r'total earnings',
    r'gross earnings',
    r'total gross(?: pay| earnings)?',
    r'gross pay',
    r'gross wages',
    r'gross income',
    r'gross',
)
_TAX = _labels(
    r'payg(?: tax)? withholding',
    r'payg tax',
    r'tax withheld',
    r'total tax',
    r'payg',
    r'income tax',
    r'withholding tax',
    r'tax',
)
_NET = _labels(
    r'net pay',
    r'net payment',
    r'net amount',
    r'net wages',
    r'net income',
    r'total net(?: pay)?',
    r'take home(?: pay)?',
    r'net',
)
_EMPLOYER = _labels(r'paid by', r'employer name', r'employer', r'business name', r'company')
_EARNINGS_HEADING = re.compile(r'^(salary|wages|earnings|payments|salary (&|and) wages)\b', re.I)
_TOTAL = re.compile(r'^total\b', re.IGNORECASE)
_COMPANY = re.compile(r'\b(pty\.? ?ltd|limited|ltd|incorporated|inc\.?)\b', re.IGNORECASE)


@dataclass(frozen=True)
class ReadPayslip:
    pay_date: date | None
    period_end: date | None
    employer: str
    gross_cents: int | None
    tax_withheld_cents: int | None
    net_cents: int | None

    def notes(self) -> list[str]:
        """What the person should check before saving."""
        out: list[str] = []
        missing = [
            label
            for label, value in (
                ('the pay date', self.pay_date),
                ('gross pay', self.gross_cents),
                ('tax withheld', self.tax_withheld_cents),
                ('net pay', self.net_cents),
            )
            if value is None
        ]
        if missing:
            out.append('Could not find ' + _join(missing) + '. Fill it in from the payslip.')
        if self.pay_date is None and self.period_end is not None:
            out.append(
                'The pay date is the end of the pay period. Change it if you were paid later.'
            )
        g, t, n = self.gross_cents, self.tax_withheld_cents, self.net_cents
        if g is not None and t is not None and t > g:
            out.append('Tax withheld is more than gross pay. Check both figures.')
        elif g is not None and t is not None and n is not None and g - t != n:
            out.append(
                'Gross less tax is not the net pay, so something else was taken out '
                '(salary sacrifice or other deductions). Check the figures.'
            )
        return out


def _join(items: Sequence[str]) -> str:
    if len(items) == 1:
        return items[0]
    return ', '.join(items[:-1]) + ' or ' + items[-1]


# --------------------------------------------------------------- values


def dates_in(text: str) -> list[date]:
    found: list[date] = []
    for m in _DATE.finditer(text):
        g = m.groupdict()
        try:
            if g['d1']:
                y = int(g['y1'])
                found.append(date(y + 2000 if y < 100 else y, int(g['m1']), int(g['d1'])))
            elif g['y2']:
                found.append(date(int(g['y2']), int(g['m2']), int(g['d2'])))
            else:
                mon = _MONTHS.get((g['mon3'] or g['mon4'])[:3].lower())
                if mon is None:
                    continue
                found.append(date(int(g['y3'] or g['y4']), mon, int(g['d3'] or g['d4'])))
        except ValueError:
            continue
    # A misread year (0202) is no date a payslip has.
    return [d for d in found if EARLIEST <= d <= LATEST]


def _cents(text: str) -> int | None:
    m = _AMOUNT.search(text)
    if m is None:
        return None
    try:
        return abs(parse_cents(m.group(0)))
    except MoneyError:
        return None


def _is_amount(cell: Cell) -> bool:
    return bool(_WHOLE_AMOUNT.match(cell.text.strip()))


# --------------------------------------------------------------- labels


def _hits(
    lines: list[Line], labels: list[re.Pattern[str]]
) -> Iterator[tuple[int, int, re.Match[str]]]:
    """(line index, cell index, match) for each label, most specific label first."""
    for pat in labels:
        for li, line in enumerate(lines):
            for ci, cell in enumerate(line.cells):
                m = pat.match(cell.text.strip())
                if m:
                    yield li, ci, m


def _below(lines: list[Line], li: int, cell: Cell, want: str) -> Cell | None:
    """A cell on one of the next two lines overlapping this one's columns."""
    for nxt in lines[li + 1 : li + 3]:
        if nxt.page != lines[li].page:
            return None
        for c in nxt.cells:
            if c.x0 <= cell.x1 + 4 and c.x1 >= cell.x0 - 4:
                if want == 'amount' and not _is_amount(c):
                    continue
                return c
    return None


def _amount_for(lines: list[Line], labels: list[re.Pattern[str]]) -> int | None:
    for li, ci, m in _hits(lines, labels):
        cell = lines[li].cells[ci]
        rest = cell.text.strip()[m.end() :]
        if rest:
            # "Gross: $100.00" holds its value; "Gross Deductions" is another label.
            cents = _cents(rest) if _AMOUNT.match(rest) else None
            if cents is not None:
                return cents
            continue
        for later in lines[li].cells[ci + 1 :]:
            if _is_amount(later):
                return _cents(later.text)
        under = _below(lines, li, cell, 'amount')
        if under is not None:
            return _cents(under.text)
    return None


def _date_for(lines: list[Line], labels: list[re.Pattern[str]]) -> date | None:
    for li, ci, m in _hits(lines, labels):
        cell = lines[li].cells[ci]
        found = dates_in(cell.text.strip()[m.end() :])
        if not found:
            for later in lines[li].cells[ci + 1 : ci + 2]:
                found = dates_in(later.text)
        if not found:
            under = _below(lines, li, cell, 'text')
            found = dates_in(under.text) if under else []
        if found:
            return found[-1]  # a period "19/09/2026 - 25/09/2026" ends on the last
    return None


def _employer(lines: list[Line]) -> str:
    for li, ci, m in _hits(lines, _EMPLOYER):
        cell = lines[li].cells[ci]
        rest = cell.text.strip()[m.end() :].strip()
        if rest:
            return rest[:120]
        under = _below(lines, li, cell, 'text')
        if under is not None and not _is_amount(under):
            return under.text.strip()[:120]
    for line in lines:
        for cell in line.cells:
            if _COMPANY.search(cell.text) and not cell.text.lower().startswith('abn'):
                return cell.text.strip()[:120]
    return ''


def _section_total(lines: list[Line]) -> int | None:
    """The TOTAL under an earnings heading, for payslips without a gross label."""
    for li, line in enumerate(lines):
        first = line.cells[0].text.strip() if line.cells else ''
        if not _EARNINGS_HEADING.match(first) or any(_is_amount(c) for c in line.cells):
            continue
        for nxt in lines[li + 1 :]:
            if any(_TOTAL.match(c.text.strip()) for c in nxt.cells):
                amounts = [c for c in nxt.cells if _is_amount(c)]
                return _cents(amounts[0].text) if amounts else None
    return None


# --------------------------------------------------------------- reading


def read_lines(lines: list[Line]) -> ReadPayslip:
    if not lines:
        raise ImportError_(
            'There is no text in that PDF. A scanned or photographed payslip cannot be read; '
            'download the PDF from your employer or payroll app instead.'
        )
    gross = _amount_for(lines, _GROSS)
    if gross is None:
        gross = _section_total(lines)
    slip = ReadPayslip(
        pay_date=_date_for(lines, _PAY_DATE),
        period_end=_date_for(lines, _PERIOD),
        employer=_employer(lines),
        gross_cents=gross,
        tax_withheld_cents=_amount_for(lines, _TAX),
        net_cents=_amount_for(lines, _NET),
    )
    if slip.gross_cents is None and slip.net_cents is None:
        raise ImportError_(
            'That does not look like a payslip: no gross or net pay was found in it.'
        )
    return slip


def read(data: bytes) -> ReadPayslip:
    if not is_pdf(data):
        raise ImportError_('That is not a PDF. Upload the payslip as a PDF.')
    return read_lines(lines_from_chars(extract_chars(data, max_pages=MAX_PAGES)))
