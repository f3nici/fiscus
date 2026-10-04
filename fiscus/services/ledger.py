"""Balances, spending summaries and categorisation over one user's transactions."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fiscus.domain.rules import Matcher, RuleSpec
from fiscus.models import Account, Category, Rule, Transaction, User
from fiscus.services.access import owned


def accounts(db: Session, user: User, *, include_archived: bool = False) -> list[Account]:
    q = owned(Account, user).order_by(Account.name)
    if not include_archived:
        q = q.where(Account.archived.is_(False))
    return list(db.execute(q).scalars())


def balances(db: Session, user: User, as_of: date | None = None) -> dict[int, int]:
    """Opening balance plus every transaction up to and including `as_of`."""
    accts = accounts(db, user, include_archived=True)
    q = (
        select(Transaction.account_id, func.coalesce(func.sum(Transaction.amount_cents), 0))
        .where(Transaction.user_id == user.id)
        .group_by(Transaction.account_id)
    )
    if as_of is not None:
        q = q.where(Transaction.date <= as_of)
    sums = {aid: int(total) for aid, total in db.execute(q)}
    return {a.id: a.opening_balance_cents + sums.get(a.id, 0) for a in accts}


def set_balance(db: Session, user: User, acct: Account, cents: int, today: date) -> None:
    """Make the account's balance today `cents` by moving its opening balance."""
    current = balances(db, user, today).get(acct.id, 0)
    acct.opening_balance_cents += cents - current
    acct.balance_updated_on = today
    db.flush()


def daily_balances(
    db: Session, user: User, account_ids: list[int], start: date, end: date
) -> dict[date, int]:
    """Combined end-of-day balance of the given accounts for each day in [start, end]."""
    if not account_ids:
        return {start + timedelta(days=i): 0 for i in range((end - start).days + 1)}
    opening = sum(
        a.opening_balance_cents
        for a in accounts(db, user, include_archived=True)
        if a.id in account_ids
    )
    before = db.scalar(
        select(func.coalesce(func.sum(Transaction.amount_cents), 0)).where(
            Transaction.user_id == user.id,
            Transaction.account_id.in_(account_ids),
            Transaction.date < start,
        )
    )
    running = opening + int(before or 0)
    moves: dict[date, int] = defaultdict(int)
    for d, amt in db.execute(
        select(Transaction.date, Transaction.amount_cents).where(
            Transaction.user_id == user.id,
            Transaction.account_id.in_(account_ids),
            Transaction.date >= start,
            Transaction.date <= end,
        )
    ):
        moves[d] += amt
    out: dict[date, int] = {}
    day = start
    while day <= end:
        running += moves.get(day, 0)
        out[day] = running
        day += timedelta(days=1)
    return out


def categories(db: Session, user: User) -> list[Category]:
    return list(db.execute(owned(Category, user).order_by(Category.name)).scalars())


def category_named(db: Session, user: User, name: str) -> Category:
    """This person's category by name, ignoring case; made if missing."""
    name = name.strip()
    cat = db.execute(
        owned(Category, user).where(func.lower(Category.name) == name.lower())
    ).scalar_one_or_none()
    if cat is None:
        cat = Category(user_id=user.id, name=name[:80])
        db.add(cat)
        db.flush()
    return cat


def spend_by_category(db: Session, user: User, start: date, end: date) -> dict[int | None, int]:
    """Net movement per category in [start, end], transfers excluded. Spend is negative."""
    transfer_ids = {c.id for c in categories(db, user) if c.is_transfer}
    rows = db.execute(
        select(Transaction.category_id, func.sum(Transaction.amount_cents))
        .where(Transaction.user_id == user.id, Transaction.date >= start, Transaction.date <= end)
        .group_by(Transaction.category_id)
    )
    return {cid: int(total) for cid, total in rows if cid not in transfer_ids}


def matcher_for(db: Session, user: User) -> Matcher:
    rules = db.execute(owned(Rule, user)).scalars().all()
    return Matcher(
        [RuleSpec(r.id, r.pattern, r.is_regex, r.category_id, r.priority) for r in rules]
    )


def apply_rules(
    db: Session, user: User, txns: list[Transaction], *, overwrite: bool = False
) -> int:
    m = matcher_for(db, user)
    changed = 0
    for t in txns:
        if t.category_id is not None and not overwrite:
            continue
        cat = m.category_for(t.description)
        if cat is not None and cat != t.category_id:
            t.category_id = cat
            changed += 1
    return changed


def rerun_rules(db: Session, user: User, *, overwrite: bool) -> int:
    q = owned(Transaction, user)
    if not overwrite:
        q = q.where(Transaction.category_id.is_(None))
    return apply_rules(db, user, list(db.execute(q).scalars()), overwrite=overwrite)
