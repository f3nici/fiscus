"""Housing cost line items: parsing an import file and diffing it against what exists.

Fields: name, amount, frequency, category, due_date (optional), shared_with
(a username, optional), split_percent (the other person's share, optional).
Items are keyed by name, case-insensitively, so a re-import can say what was
added, changed and removed before anything is applied.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from fiscus.domain.csvimport import ImportError_, decode, read_rows
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
class ItemSpec:
    name: str
    amount_cents: int
    frequency: str
    category: str
    due_date: date | None
    shared_with: str
    split_bp: int

    def key(self) -> str:
        return self.name.casefold()

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d['due_date'] = self.due_date.isoformat() if self.due_date else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ItemSpec:
        return cls(
            name=d['name'],
            amount_cents=int(d['amount_cents']),
            frequency=d['frequency'],
            category=d['category'],
            due_date=date.fromisoformat(d['due_date']) if d.get('due_date') else None,
            shared_with=d.get('shared_with') or '',
            split_bp=int(d.get('split_bp') or 0),
        )


@dataclass
class ParsedItems:
    items: list[ItemSpec]
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
    return json.dumps({'items': rows}, indent=2) + '\n'


def _item(raw: dict[str, Any], where: str) -> ItemSpec:
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
            'monthly, quarterly, yearly, one-off'
        )
    due_raw = str(raw.get('due_date') or '').strip()
    due: date | None = None
    if due_raw:
        try:
            due = date.fromisoformat(due_raw)
        except ValueError as exc:
            raise ValueError(f'{where}: due_date must be YYYY-MM-DD') from exc
    if freq == 'one_off' and due is None:
        raise ValueError(f'{where}: a one-off item needs a due_date')
    shared = str(raw.get('shared_with') or '').strip().lower()
    split_raw = raw.get('split_percent')
    split_bp = 0
    if shared:
        try:
            pct = float(split_raw) if split_raw not in (None, '') else 50.0
        except (TypeError, ValueError) as exc:
            raise ValueError(f'{where}: split_percent must be a number') from exc
        if not 0 < pct < 100:
            raise ValueError(f'{where}: split_percent must be between 0 and 100')
        split_bp = round(pct * 100)
    category = str(raw.get('category') or '').strip() or 'Housing'
    return ItemSpec(name[:120], amount, freq, category[:80], due, shared, split_bp)


def parse_json(data: bytes) -> ParsedItems:
    try:
        doc = json.loads(decode(data))
    except (json.JSONDecodeError, ImportError_) as exc:
        raise ImportError_(f'That is not valid JSON: {exc}') from exc
    rows = doc.get('items') if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        raise ImportError_('Expected {"items": [...]} or a list of items')
    return _collect(
        [(r if isinstance(r, dict) else {}, f'Item {i + 1}') for i, r in enumerate(rows)]
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


def parse_csv(data: bytes, mapping: dict[str, int | None] | None = None) -> ParsedItems:
    header, body = csv_header(data)
    mapping = mapping or default_mapping(header)
    if not mapping_complete(mapping):
        raise ImportError_('Map the name, amount and frequency columns')
    rows = []
    for i, r in enumerate(body):
        raw = {
            f: (r[idx] if idx is not None and idx < len(r) else '') for f, idx in mapping.items()
        }
        rows.append((raw, f'Row {i + 2}'))
    return _collect(rows)


def _collect(rows: list[tuple[dict[str, Any], str]]) -> ParsedItems:
    items: list[ItemSpec] = []
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
    return ParsedItems(items, errors)


@dataclass
class Diff:
    added: list[ItemSpec]
    changed: list[tuple[ItemSpec, ItemSpec]]  # (existing, incoming)
    removed: list[ItemSpec]
    unchanged: list[ItemSpec]

    @property
    def empty(self) -> bool:
        return not (self.added or self.changed or self.removed)


def diff(existing: list[ItemSpec], incoming: list[ItemSpec]) -> Diff:
    old = {i.key(): i for i in existing}
    new = {i.key(): i for i in incoming}
    added = [i for k, i in new.items() if k not in old]
    removed = [i for k, i in old.items() if k not in new]
    changed = []
    unchanged = []
    for k, i in new.items():
        if k in old:
            o = old[k]
            if (
                o.amount_cents,
                o.frequency,
                o.category.casefold(),
                o.due_date,
                o.shared_with,
                o.split_bp,
            ) != (
                i.amount_cents,
                i.frequency,
                i.category.casefold(),
                i.due_date,
                i.shared_with,
                i.split_bp,
            ):
                changed.append((o, i))
            else:
                unchanged.append(i)
    return Diff(added, changed, removed, unchanged)
