"""Balances, spending summaries and categorisation over one user's transactions."""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fiscus.domain.rules import Matcher, RuleSpec
from fiscus.models import Account, AccountBalance, Budget, Category, Rule, Transaction, User
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
    """Make the account's balance today `cents` by moving its opening balance,
    and keep it as that day's figure in the account's history."""
    current = balances(db, user, today).get(acct.id, 0)
    acct.opening_balance_cents += cents - current
    acct.balance_updated_on = today
    row = db.execute(
        owned(AccountBalance, user).where(
            AccountBalance.account_id == acct.id, AccountBalance.date == today
        )
    ).scalar_one_or_none()
    if row is None:
        db.add(AccountBalance(user_id=user.id, account_id=acct.id, date=today, balance_cents=cents))
    else:
        row.balance_cents = cents
    db.flush()


def balance_history(
    db: Session, user: User, account_ids: list[int], start: date, end: date
) -> tuple[dict[date, int], date]:
    """The accounts' combined end-of-day balance for each day in [start, end],
    as known on the day, and the first day all of them are known.

    An account's balance is its last typed-in figure on or before the day,
    with any transactions after it added. Before its first one the account is
    unknown and left out. An account never typed in (transactions only) is its
    opening balance and transactions, as before.
    """
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    out = dict.fromkeys(days, 0)
    known_from = start
    if not account_ids:
        return out, known_from
    accts = {a.id: a for a in accounts(db, user, include_archived=True) if a.id in account_ids}
    records: dict[int, list[tuple[date, int]]] = defaultdict(list)
    for r in db.execute(
        owned(AccountBalance, user)
        .where(AccountBalance.account_id.in_(account_ids), AccountBalance.date <= end)
        .order_by(AccountBalance.date)
    ).scalars():
        records[r.account_id].append((r.date, r.balance_cents))
    moves: dict[int, list[tuple[date, int]]] = defaultdict(list)
    for aid, d, amt in db.execute(
        select(Transaction.account_id, Transaction.date, Transaction.amount_cents)
        .where(
            Transaction.user_id == user.id,
            Transaction.account_id.in_(account_ids),
            Transaction.date <= end,
        )
        .order_by(Transaction.date)
    ):
        moves[aid].append((d, amt))
    for aid, acct in accts.items():
        moved = _Running(moves[aid])
        recs = records.get(aid)
        if not recs:
            for day in days:
                out[day] += acct.opening_balance_cents + moved.to(day)
            continue
        rec_dates = [d for d, _ in recs]
        known_from = max(known_from, rec_dates[0])
        for day in days:
            i = bisect_right(rec_dates, day) - 1
            if i < 0:
                continue
            on, cents = recs[i]
            out[day] += cents + moved.to(day) - moved.to(on)
    return out, known_from


class _Running:
    """Transactions added up to any day, from a date-ordered list."""

    def __init__(self, moves: list[tuple[date, int]]) -> None:
        self.dates = [d for d, _ in moves]
        self.totals = [0]
        for _d, amt in moves:
            self.totals.append(self.totals[-1] + amt)

    def to(self, day: date) -> int:
        return self.totals[bisect_right(self.dates, day)]


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


def category_exists(db: Session, user: User, name: str, besides: Category | None = None) -> bool:
    """Whether this person has another category by this name, ignoring case."""
    q = owned(Category, user).where(func.lower(Category.name) == name.strip().lower())
    if besides is not None:
        q = q.where(Category.id != besides.id)
    return db.execute(q.limit(1)).first() is not None


def move_aside(db: Session, user: User, name: str, besides: Category) -> Category | None:
    """Free `name` for `besides` when another category has it but is no longer
    in the budget: that one is renamed "<name> (old)", keeping its history.
    Returns the category in the way when it is still a budget line."""
    other = db.execute(
        owned(Category, user).where(
            func.lower(Category.name) == name.strip().lower(), Category.id != besides.id
        )
    ).scalar_one_or_none()
    if other is None:
        return None
    if db.execute(owned(Budget, user).where(Budget.category_id == other.id)).first():
        return other
    base = other.name[:70]
    aside, n = f'{base} (old)', 2
    while category_exists(db, user, aside):
        aside, n = f'{base} (old {n})', n + 1
    other.name = aside
    db.flush()
    return None


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
