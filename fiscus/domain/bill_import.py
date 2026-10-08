"""Bills from a file: parsing an import and diffing it against the bills you own.

Fields: name, amount, frequency, category, due_date (optional), shared_with
(a username, optional), split_percent (the other person's share, optional).
Bills are keyed by name, case-insensitively, so a re-import can say what was
added, changed and removed before anything is applied.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from fiscus.domain.uploads import ImportError_, decode, read_rows
from fiscus.money import MoneyError, parse_cents

FIELDS = ('name', 'amount', 'frequency', 'category', 'due_date', 'shared_with', 'split_percent')
REQUIRED = ('name', 'amount', 'frequency')
FREQ_ALIASES = {
    'week': 'weekly',
    'weekly': 'weekly',
    'fortnight': 'fortnightly',
    'fortnightly': 'fortnightly',
    'month': 'monthly',
    'monthly': 'monthly',
    'bimonthly': 'bimonthly',
    'bi-monthly': 'bimonthly',
    'bi monthly': 'bimonthly',
    '2 monthly': 'bimonthly',
    'every 2 months': 'bimonthly',
    'quarter': 'quarterly',
    'quarterly': 'quarterly',
    'year': 'yearly',
    'yearly': 'yearly',
    'annual': 'yearly',
    'annually': 'yearly',
    'one-off': 'one_off',
    'one_off': 'one_off',
    'once': 'one_off',
}

TEMPLATE_ROWS: list[dict[str, str]] = [
    {
        'name': 'Council rates',
        'amount': '520.00',
        'frequency': 'quarterly',
        'category': 'Housing',
        'due_date': '2026-11-15',
        'shared_with': '',
        'split_percent': '',
    },
    {
        'name': 'Home insurance',
        'amount': '1850.00',
        'frequency': 'yearly',
        'category': 'Insurance',
        'due_date': '2027-03-01',
        'shared_with': 'partner',
        'split_percent': '50',
    },
    {
        'name': 'Water',
        'amount': '310.00',
        'frequency': 'quarterly',
        'category': 'Utilities',
        'due_date': '',
        'shared_with': 'partner',
        'split_percent': '50',
    },
]


@dataclass(frozen=True)
class BillSpec:
    name: str
    amount_cents: int
    frequency: str
    # None: the file has no such column, so an existing bill keeps its own.
    category: str | None
    due_date: date | None
    shared_with: str | None
    split_bp: int
    # An existing bill split in a way a file cannot say (fixed amounts, or more
    # than two people): an import leaves its shares alone.
    keep_shares: bool = False

    def key(self) -> str:
        return self.name.casefold()


@dataclass
class ParsedBills:
    items: list[BillSpec]
    errors: list[str]


def template_csv() -> str:
    lines = [','.join(FIELDS)]
    for row in TEMPLATE_ROWS:
        lines.append(','.join(row[f] for f in FIELDS))
    return '\n'.join(lines) + '\n'


def template_json() -> str:
    rows = []
    for row in TEMPLATE_ROWS:
        rows.append(
            {
                'name': row['name'],
                'amount': row['amount'],
                'frequency': row['frequency'],
                'category': row['category'],
                'due_date': row['due_date'] or None,
                'shared_with': row['shared_with'] or None,
                'split_percent': float(row['split_percent']) if row['split_percent'] else None,
            }
        )
    return json.dumps({'bills': rows}, indent=2) + '\n'


def _item(raw: dict[str, Any], where: str) -> BillSpec:
    name = str(raw.get('name') or '').strip()
    if not name:
        raise ValueError(f'{where}: name is required')
    try:
        amount = parse_cents(str(raw.get('amount') or ''), allow_negative=False)
    except MoneyError as exc:
        raise ValueError(f'{where}: amount: {exc}') from exc
    freq_raw = str(raw.get('frequency') or '').strip().lower()
    freq = FREQ_ALIASES.get(freq_raw)
    if freq is None:
        raise ValueError(
            f'{where}: frequency "{freq_raw}" is not one of weekly, fortnightly, '
            'monthly, bimonthly, quarterly, yearly, one-off'
        )
    due_raw = str(raw.get('due_date') or '').strip()
    due: date | None = None
    if due_raw:
        try:
            due = date.fromisoformat(due_raw)
        except ValueError as exc:
            raise ValueError(f'{where}: due_date must be YYYY-MM-DD') from exc
    if freq == 'one_off' and due is None:
        raise ValueError(f'{where}: a one-off bill needs a due_date')
    shared = str(raw.get('shared_with') or '').strip().lower() if 'shared_with' in raw else None
    split_raw = raw.get('split_percent')
    split_bp = 0
    if shared:
        try:
            pct = Decimal(str(split_raw)) if split_raw not in (None, '') else Decimal(50)
        except InvalidOperation as exc:
            raise ValueError(f'{where}: split_percent must be a number') from exc
        if not pct.is_finite() or not 0 < pct < 100:
            raise ValueError(f'{where}: split_percent must be between 0 and 100')
        split_bp = int((pct * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    category = str(raw.get('category') or '').strip()[:80] if 'category' in raw else None
    return BillSpec(name[:120], amount, freq, category, due, shared, split_bp)


def parse_json(data: bytes) -> ParsedBills:
    try:
        doc = json.loads(decode(data))
    except (json.JSONDecodeError, ImportError_) as exc:
        raise ImportError_(f'That is not valid JSON: {exc}') from exc
    # "items" is what the housing export used; old files still import.
    rows = (doc.get('bills') or doc.get('items')) if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        raise ImportError_('Expected {"bills": [...]} or a list of bills')
    return _collect(
        [(r if isinstance(r, dict) else {}, f'Bill {i + 1}') for i, r in enumerate(rows)]
    )


def csv_header(data: bytes) -> tuple[list[str], list[list[str]]]:
    rows = read_rows(decode(data))
    if not rows:
        raise ImportError_('That file is empty')
    return [h.strip() for h in rows[0]], rows[1:]


def default_mapping(header: list[str]) -> dict[str, int | None]:
    lowered = [h.strip().lower().replace(' ', '_') for h in header]
    return {f: (lowered.index(f) if f in lowered else None) for f in FIELDS}


def mapping_complete(mapping: dict[str, int | None]) -> bool:
    return all(mapping.get(f) is not None for f in REQUIRED)


def parse_csv(data: bytes, mapping: dict[str, int | None] | None = None) -> ParsedBills:
    header, body = csv_header(data)
    mapping = mapping or default_mapping(header)
    if not mapping_complete(mapping):
        raise ImportError_('Map the name, amount and frequency columns')
    rows = []
    for i, r in enumerate(body):
        # A field not mapped is left out, so the bill keeps what it has.
        raw = {f: (r[idx] if idx < len(r) else '') for f, idx in mapping.items() if idx is not None}
        rows.append((raw, f'Row {i + 2}'))
    return _collect(rows)


def _collect(rows: list[tuple[dict[str, Any], str]]) -> ParsedBills:
    items: list[BillSpec] = []
    errors: list[str] = []
    seen: set[str] = set()
    for raw, where in rows:
        try:
            item = _item(raw, where)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if item.key() in seen:
            errors.append(f'{where}: "{item.name}" appears twice')
            continue
        seen.add(item.key())
        items.append(item)
    return ParsedBills(items, errors)


@dataclass
class Diff:
    added: list[BillSpec]
    changed: list[tuple[BillSpec, BillSpec]]  # (existing, incoming)
    removed: list[BillSpec]
    unchanged: list[BillSpec]

    @property
    def empty(self) -> bool:
        return not (self.added or self.changed or self.removed)


def _same(old: BillSpec, new: BillSpec) -> bool:
    """Whether applying `new` would leave the bill as it is. Anything the file
    does not say (no column, or no due date) keeps the bill's own."""
    if (old.amount_cents, old.frequency) != (new.amount_cents, new.frequency):
        return False
    if new.category is not None and new.category.casefold() != (old.category or '').casefold():
        return False
    if new.due_date is not None and new.due_date != old.due_date:
        return False
    if new.shared_with is None or old.keep_shares:
        return True
    return (old.shared_with, old.split_bp) == (new.shared_with, new.split_bp)


def diff(existing: list[BillSpec], incoming: list[BillSpec]) -> Diff:
    old = {i.key(): i for i in existing}
    new = {i.key(): i for i in incoming}
    added = [i for k, i in new.items() if k not in old]
    removed = [i for k, i in old.items() if k not in new]
    changed = []
    unchanged = []
    for k, i in new.items():
        if k in old:
            if _same(old[k], i):
                unchanged.append(i)
            else:
                changed.append((old[k], i))
    return Diff(added, changed, removed, unchanged)
