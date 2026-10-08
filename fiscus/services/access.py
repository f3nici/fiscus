"""Ownership. Every per-user read goes through here.

`owned` builds a SELECT already filtered to the user; `get_owned` fetches one
row by id and raises 404 (never 403) when it belongs to someone else, so ids
cannot be probed. Bills are the exception, being shared: they are reached
through the user's BillShare.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from fiscus.models import Bill, BillShare, User

# SQLite integers are 64-bit; a bigger id cannot be a row and would not bind.
_ID_LIMIT = 2**63


def in_range(ident: int) -> bool:
    """Whether `ident` could be a row id at all."""
    return -_ID_LIMIT <= ident < _ID_LIMIT


def owned[T](model: type[T], user: User) -> Select[T]:
    col: Any = model.user_id  # type: ignore[attr-defined]
    return select(model).where(col == user.id)


def get_owned[T](db: Session, model: type[T], ident: int | None, user: User) -> T:
    if ident is None or not in_range(ident):
        raise HTTPException(status_code=404)
    obj = db.get(model, ident)
    if obj is None or getattr(obj, 'user_id', None) != user.id:
        raise HTTPException(status_code=404)
    return obj


def maybe_owned[T](db: Session, model: type[T], ident: int | None, user: User) -> T | None:
    if ident in (None, 0):
        return None
    return get_owned(db, model, ident, user)


def visible_bill(db: Session, bill_id: int, user: User) -> tuple[Bill, BillShare]:
    if not in_range(bill_id):
        raise HTTPException(status_code=404)
    share = db.execute(
        select(BillShare).where(BillShare.bill_id == bill_id, BillShare.user_id == user.id)
    ).scalar_one_or_none()
    if share is None:
        raise HTTPException(status_code=404)
    return share.bill, share


def owned_bill(db: Session, bill_id: int, user: User) -> Bill:
    bill = db.get(Bill, bill_id) if in_range(bill_id) else None
    if bill is None or bill.owner_id != user.id:
        # A participant sees a 404 for edits too: they cannot change the bill.
        raise HTTPException(status_code=404)
    return bill
