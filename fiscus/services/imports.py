"""Staging, previewing, committing and undoing a bank CSV import."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from fiscus.clock import Clock, utcnow_naive
from fiscus.config import Settings
from fiscus.domain import csvimport
from fiscus.domain.csvimport import ANZ, ImportError_, Mapping, ParsedRow
from fiscus.models import Account, BillPayment, Import, ImportMapping, Transaction, User
from fiscus.services import bills, ledger


@dataclass
class Preview:
    rows: list[tuple[ParsedRow, str, bool]]  # row, hash, is_duplicate
    errors: list[str]
    new_count: int
    duplicate_count: int


def stage(
    db: Session,
    settings: Settings,
    clock: Clock,
    user: User,
    account: Account,
    file_name: str,
    data: bytes,
) -> Import:
    text = csvimport.decode(data)  # refuses oversize and binary before anything is written
    if not csvimport.read_rows(text):
        raise ImportError_('That file has no rows')
    name = f'{uuid.uuid4().hex}.csv'
    (settings.upload_dir / name).write_bytes(data)
    imp = Import(
        user_id=user.id,
        account_id=account.id,
        file_name=Path(file_name or 'upload.csv').name[:255],
        stored_path=name,
        status='pending',
        created_at=utcnow_naive(clock),
    )
    db.add(imp)
    db.flush()
    return imp


def raw_rows(settings: Settings, imp: Import) -> list[list[str]]:
    path = settings.upload_dir / imp.stored_path
    if not imp.stored_path or not path.exists():
        raise ImportError_('The uploaded file is gone. Upload it again.')
    return csvimport.read_rows(csvimport.decode(path.read_bytes()))


def mapping_for(db: Session, settings: Settings, imp: Import) -> tuple[Mapping | None, str]:
    """ANZ if the file looks like ANZ, else the account's saved mapping, else None."""
    rows = raw_rows(settings, imp)
    if csvimport.is_anz(rows):
        return ANZ, 'anz'
    saved = db.execute(
        select(ImportMapping).where(ImportMapping.account_id == imp.account_id)
    ).scalar_one_or_none()
    if saved is not None:
        return Mapping.from_dict(saved.mapping), 'mapped'
    return None, ''


def save_mapping(db: Session, user: User, account_id: int, mapping: Mapping) -> None:
    saved = db.execute(
        select(ImportMapping).where(ImportMapping.account_id == account_id)
    ).scalar_one_or_none()
    if saved is None:
        db.add(ImportMapping(user_id=user.id, account_id=account_id, mapping=mapping.to_dict()))
    else:
        saved.mapping = mapping.to_dict()


def preview(db: Session, settings: Settings, imp: Import, mapping: Mapping) -> Preview:
    parsed = csvimport.parse(raw_rows(settings, imp), mapping)
    if not parsed.rows and parsed.errors:
        raise ImportError_('No rows could be read with that mapping. ' + parsed.errors[0])
    hashes = csvimport.dedup_hashes(imp.account_id, parsed.rows)
    existing = set(
        db.execute(
            select(Transaction.dedup_hash).where(
                Transaction.account_id == imp.account_id, Transaction.dedup_hash.in_(hashes)
            )
        ).scalars()
    )
    rows = [(r, h, h in existing) for r, h in zip(parsed.rows, hashes, strict=True)]
    dupes = sum(1 for _r, _h, d in rows if d)
    return Preview(rows, parsed.errors, len(rows) - dupes, dupes)


def commit(
    db: Session,
    settings: Settings,
    clock: Clock,
    user: User,
    imp: Import,
    mapping: Mapping,
    fmt: str,
) -> Import:
    if imp.status != 'pending':
        raise ImportError_('That import was already committed')
    pv = preview(db, settings, imp, mapping)
    now = utcnow_naive(clock)
    created: list[Transaction] = []
    for row, digest, dup in pv.rows:
        if dup:
            continue
        t = Transaction(
            user_id=user.id,
            account_id=imp.account_id,
            date=row.date,
            amount_cents=row.amount_cents,
            description=row.description,
            import_id=imp.id,
            dedup_hash=digest,
            created_at=now,
        )
        db.add(t)
        created.append(t)
    db.flush()
    ledger.apply_rules(db, user, created)
    imp.status = 'committed'
    imp.format = fmt
    imp.row_count = len(pv.rows)
    imp.imported_count = len(created)
    imp.duplicate_count = pv.duplicate_count
    imp.committed_at = now
    db.flush()
    bills.auto_match(db, clock, user, created)
    _discard_file(settings, imp)
    return imp


def undo(db: Session, settings: Settings, clock: Clock, imp: Import) -> int:
    if imp.status != 'committed':
        raise ImportError_('Only a committed import can be undone')
    ids = list(db.execute(select(Transaction.id).where(Transaction.import_id == imp.id)).scalars())
    if ids:
        db.execute(delete(BillPayment).where(BillPayment.transaction_id.in_(ids)))
        db.execute(delete(Transaction).where(Transaction.id.in_(ids)))
    # The bulk DELETE skips the ORM; changing the Import row below still bumps
    # this user's data_version, so the forecast cache is invalidated.
    imp.status = 'undone'
    imp.undone_at = utcnow_naive(clock)
    return len(ids)


def discard(db: Session, settings: Settings, imp: Import) -> None:
    _discard_file(settings, imp)
    db.delete(imp)


def _discard_file(settings: Settings, imp: Import) -> None:
    if imp.stored_path:
        (settings.upload_dir / imp.stored_path).unlink(missing_ok=True)
        imp.stored_path = ''
