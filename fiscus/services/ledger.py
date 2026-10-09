"""Balances, spending summaries and categorisation over one user's transactions."""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fiscus.clock import Clock, utcnow_naive
from fiscus.domain.rules import Matcher, RuleSpec
from fiscus.models import (
    Account,
    AccountBalance,
    BillPayment,
    Budget,
    Category,
    Payslip,
    Rule,
    Transaction,
    User,
)
from fiscus.money import fmt
from fiscus.services.access import owned
from fiscus.services.users import settings_for

# Transactions shown on an account as moving it since its balance was typed in.
MOVES_SHOWN = 30


def accounts(db: Session, user: User, *, include_archived: bool = False) -> list[Account]:
    q = owned(Account, user).order_by(Account.name)
    if not include_archived:
        q = q.where(Account.archived.is_(False))
    return list(db.execute(q).scalars())


def _last_typed(db: Session, user: User, as_of: date) -> dict[int, tuple[date, int]]:
    """Each account's last balance typed in on or before `as_of`."""
    out: dict[int, tuple[date, int]] = {}
    for r in db.execute(
        owned(AccountBalance, user)
        .where(AccountBalance.date <= as_of)
        .order_by(AccountBalance.date)
    ).scalars():
        out[r.account_id] = (r.date, r.balance_cents)
    return out


def balances(db: Session, user: User, as_of: date) -> dict[int, int]:
    """Each account's balance at the end of `as_of`: the last balance typed in
    on or before it, plus the transactions dated after that day (a typed
    balance counts everything on its own day). An account never typed in is
    its opening balance plus every transaction to `as_of`."""
    accts = accounts(db, user, include_archived=True)
    typed = _last_typed(db, user, as_of)
    out = {a.id: typed[a.id][1] if a.id in typed else a.opening_balance_cents for a in accts}
    for aid, day, total in db.execute(
        select(Transaction.account_id, Transaction.date, func.sum(Transaction.amount_cents))
        .where(Transaction.user_id == user.id, Transaction.date <= as_of)
        .group_by(Transaction.account_id, Transaction.date)
    ):
        on = typed.get(aid)
        if aid in out and (on is None or day > on[0]):
            out[aid] += int(total)
    return out


def set_balance(db: Session, user: User, acct: Account, cents: int, today: date) -> None:
    """Keep `cents` as the account's balance today, in its history."""
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


def moved_since_typed(db: Session, user: User, acct: Account, today: date) -> list[Transaction]:
    """What has moved the account since its balance was last typed in, newest
    first: bills marked paid, payslips and anything synced. Nothing for an
    account never typed in."""
    on = _last_typed(db, user, today).get(acct.id)
    if on is None:
        return []
    return list(
        db.execute(
            owned(Transaction, user)
            .where(
                Transaction.account_id == acct.id,
                Transaction.date > on[0],
                Transaction.date <= today,
            )
            .order_by(Transaction.date.desc(), Transaction.id.desc())
            .limit(MOVES_SHOWN)
        ).scalars()
    )


def spending_account(db: Session, user: User, accts: list[Account]) -> int | None:
    pref = settings_for(db, user).spending_account_id
    if pref and any(a.id == pref for a in accts):
        return pref
    for kind in ('transaction', 'offset', 'savings', 'credit_card'):
        for a in accts:
            if a.type == kind and not a.is_set_aside:
                return a.id
    return accts[0].id if accts else None


def paid_into(db: Session, user: User, account_id: int | None) -> int | None:
    """The account money for `account_id` goes in or out of: that one while it
    is open, else the spending account, as the forecast has it."""
    accts = accounts(db, user)
    if account_id is not None and any(a.id == account_id for a in accts):
        return account_id
    return spending_account(db, user, accts)


def follow(
    db: Session,
    clock: Clock,
    user: User,
    link: Payslip | BillPayment,
    *,
    account_id: int | None,
    day: date,
    cents: int,
    description: str,
    category_id: int | None = None,
) -> None:
    """Move an account's balance with a payslip or a bill marked paid by hand
    (D42): one transaction linked to it, changed when it is and deleted with it
    (the foreign key cascades). It goes into `paid_into(account_id)`. Nothing
    is written for a zero amount or a person with no accounts."""
    if isinstance(link, Payslip):
        col, key = Transaction.payslip_id, f'payslip:{link.id}'
    else:
        col, key = Transaction.bill_payment_id, f'bill-payment:{link.id}'
    txn = db.execute(owned(Transaction, user).where(col == link.id)).scalar_one_or_none()
    target = paid_into(db, user, account_id)
    if target is None or cents == 0:
        if txn is not None:
            db.delete(txn)
            db.flush()
        return
    if txn is None:
        txn = Transaction(user_id=user.id, dedup_hash=key, created_at=utcnow_naive(clock))
        if isinstance(link, Payslip):
            txn.payslip_id = link.id
        else:
            txn.bill_payment_id = link.id
        db.add(txn)
    txn.account_id = target
    txn.date = day
    txn.amount_cents = cents
    txn.description = description[:500]
    txn.category_id = category_id
    db.flush()


def now_at(db: Session, user: User, before: dict[int, int], today: date) -> str:
    """'. Everyday is now $812.40' for each account whose balance today is no
    longer `before`, for a flash message; empty when none moved."""
    after = balances(db, user, today)
    names = {a.id: a.name for a in accounts(db, user, include_archived=True)}
    moved = [
        f'{names[aid]} is now {fmt(cents)}'
        for aid, cents in after.items()
        if cents != before.get(aid, cents)
    ]
    return '. ' + ', '.join(moved) if moved else ''


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
