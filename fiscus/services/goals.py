"""Savings goals: what each holds and the split of an account. Goals are not
budget lines; what they keep aside only comes off the money free in accounts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fiscus.clock import Clock, utcnow_naive
from fiscus.domain import goals as calc
from fiscus.models import Account, BudgetSpend, Category, Goal, GoalMove, User
from fiscus.money import fmt
from fiscus.services import budgeting, ledger
from fiscus.services.access import maybe_owned, owned


def goals(db: Session, user: User, *, include_archived: bool = False) -> list[Goal]:
    q = owned(Goal, user).order_by(Goal.archived, Goal.target_date.is_(None), Goal.target_date)
    if not include_archived:
        q = q.where(Goal.archived.is_(False))
    return list(db.execute(q.order_by(Goal.name)).scalars())


def tax_goal(db: Session, user: User) -> Goal | None:
    """The open goal that holds the person's tax money, if one is marked."""
    q = owned(Goal, user).where(Goal.is_tax.is_(True), Goal.archived.is_(False))
    return db.execute(q.order_by(Goal.id)).scalars().first()


def set_tax(db: Session, user: User, goal: Goal, is_tax: bool) -> None:
    """Mark or unmark `goal` as the tax money. Only one goal is: marking one
    unmarks the rest."""
    if is_tax:
        for other in db.execute(owned(Goal, user).where(Goal.is_tax.is_(True))).scalars():
            if other is not goal:
                other.is_tax = False
    goal.is_tax = is_tax
    db.flush()


def held(
    db: Session, user: User, *, as_of: date | None = None, since: date | None = None
) -> dict[int, int]:
    """What each goal holds: its moves added up, to the end of `as_of`, from `since`."""
    q = (
        select(GoalMove.goal_id, func.sum(GoalMove.amount_cents))
        .where(GoalMove.user_id == user.id)
        .group_by(GoalMove.goal_id)
    )
    if as_of is not None:
        q = q.where(GoalMove.date <= as_of)
    if since is not None:
        q = q.where(GoalMove.date >= since)
    return {gid: int(total) for gid, total in db.execute(q)}


def move(
    db: Session, clock: Clock, user: User, goal: Goal, cents: int, day: date, note: str = ''
) -> GoalMove:
    row = GoalMove(
        user_id=user.id,
        goal_id=goal.id,
        date=day,
        amount_cents=cents,
        note=note.strip()[:120],
        created_at=utcnow_naive(clock),
    )
    db.add(row)
    db.flush()
    return row


def take_out(
    db: Session, clock: Clock, user: User, goal: Goal, cents: int, day: date, note: str = ''
) -> GoalMove:
    """Money taken out of the goal is spent (flights, the bill it saved for),
    so it comes off the goal's account, else the spending account, as spending
    taken off a budget does (D73). Holds now (`set_held`) corrects what a goal
    holds without moving any account."""
    row = move(db, clock, user, goal, -cents, day, note)
    ledger.follow(
        db,
        clock,
        user,
        row,
        account_id=goal.account_id,
        day=day,
        cents=-cents,
        description=f'{goal.name} · {row.note}' if row.note else f'Out of {goal.name}',
    )
    return row


def lines(db: Session, user: User, goal: Goal) -> list[Category]:
    """The budget lines this goal is saved from."""
    q = owned(Category, user).where(Category.goal_id == goal.id).order_by(Category.name)
    return list(db.execute(q).scalars())


def set_lines(db: Session, user: User, goal: Goal, cats: list[Category]) -> None:
    """Save the goal from exactly `cats`. A line saves into one goal, so taking
    it here takes it from any other."""
    ids = {c.id for c in cats}
    for c in lines(db, user, goal):
        if c.id not in ids:
            c.goal_id = None
    for c in cats:
        c.goal_id = goal.id
    db.flush()


def put_aside(
    db: Session,
    clock: Clock,
    user: User,
    goal: Goal,
    cat: Category,
    cents: int,
    day: date,
    note: str = '',
) -> GoalMove:
    """Money saved from one of the goal's budget lines: spent on the line and
    put into the goal at once, so the budget counts it used and the goal keeps
    it out of what is free."""
    spend = BudgetSpend(
        user_id=user.id,
        category_id=cat.id,
        date=day,
        amount_cents=cents,
        note=(note.strip() or f'Into {goal.name}')[:120],
        created_at=utcnow_naive(clock),
    )
    db.add(spend)
    db.flush()
    row = move(db, clock, user, goal, cents, day, note or f'From {cat.name} budget')
    row.spend_id = spend.id
    db.flush()
    return row


def remove_move(db: Session, user: User, row: GoalMove) -> None:
    """Remove a goal move and the budget spend recorded with it."""
    spend = maybe_owned(db, BudgetSpend, row.spend_id, user)
    db.delete(row)
    if spend is not None:
        db.delete(spend)
    db.flush()


def delete_goal(db: Session, user: User, goal: Goal) -> None:
    """Delete a goal and what was put aside into it from budget lines: both
    halves of each go, as when one entry is removed (D34). What was taken out
    of it stays off its accounts (D73)."""
    ledger.keep_taken_out(db, user, goal.id)
    for row in list(goal.moves):
        spend = maybe_owned(db, BudgetSpend, row.spend_id, user)
        if spend is not None:
            db.delete(spend)
    db.delete(goal)
    db.flush()


def remove_spend(db: Session, user: User, spend: BudgetSpend) -> None:
    """Remove a budget spend and the goal move recorded with it."""
    for row in db.execute(owned(GoalMove, user).where(GoalMove.spend_id == spend.id)).scalars():
        db.delete(row)
    db.delete(spend)
    db.flush()


def put_aside_spends(db: Session, user: User) -> set[int]:
    """The budget spends put aside into a goal, by id: they write no
    transaction, as the money stays in its account (D49)."""
    q = select(GoalMove.spend_id).where(GoalMove.user_id == user.id, GoalMove.spend_id.is_not(None))
    return {sid for sid in db.execute(q).scalars() if sid is not None}


def follow_spend(db: Session, user: User, spend: BudgetSpend, was: Category, cat: Category) -> None:
    """Keep the goal move put aside with `spend` in step once the spend is
    changed (D72): its amount and day, and its note if it named the line."""
    for row in db.execute(owned(GoalMove, user).where(GoalMove.spend_id == spend.id)).scalars():
        row.amount_cents = spend.amount_cents
        row.date = spend.date
        if row.note == f'From {was.name} budget':
            row.note = f'From {cat.name} budget'
    db.flush()


def by_category(db: Session, user: User) -> dict[int, Goal]:
    """Open goals saved from a budget line, by the line's category."""
    open_ = {g.id: g for g in goals(db, user)}
    q = owned(Category, user).where(Category.goal_id.is_not(None))
    return {
        c.id: open_[c.goal_id]
        for c in db.execute(q).scalars()
        if c.goal_id is not None and c.goal_id in open_
    }


def set_held(
    db: Session, clock: Clock, user: User, goal: Goal, cents: int, day: date
) -> GoalMove | None:
    """Make the goal hold `cents` today, by a move of the difference."""
    diff = cents - held(db, user).get(goal.id, 0)
    if diff == 0:
        return None
    return move(db, clock, user, goal, diff, day, f'Set to {fmt(cents)}')


@dataclass(frozen=True)
class GoalView:
    goal: Goal
    progress: calc.Progress
    per_month_cents: int  # a guide: what to put in this budget month to reach the target
    months_left: int

    @property
    def held_cents(self) -> int:
        return self.progress.held_cents

    @property
    def state(self) -> str:
        g = self.goal
        if g.archived:
            return 'done'
        if self.progress.reached:
            return 'reached'
        if g.target_cents is None or g.target_date is None:
            return 'open'
        if self.months_left == 0:
            return 'passed'
        return 'saving'


def views(
    db: Session, user: User, today: date, *, include_archived: bool = False
) -> list[GoalView]:
    start, _end = budgeting.period(db, user, today)
    first = budgeting.start_day(db, user)
    now = held(db, user)
    before = held(db, user, as_of=start - timedelta(days=1))
    out = []
    for g in goals(db, user, include_archived=include_archived):
        progress = calc.Progress(now.get(g.id, 0), g.target_cents)
        months = calc.months_left(start, g.target_date, first) if g.target_date else 0
        need = (
            calc.per_month((g.target_cents or 0) - before.get(g.id, 0), months)
            if g.target_cents is not None and not g.archived
            else 0
        )
        out.append(GoalView(g, progress, need, months))
    return out


def held_outside(db: Session, user: User, accounts: list[Account]) -> int:
    """What open goals kept aside hold, in `accounts` or in no account: money
    that is in the accounts but not free to spend."""
    ids = {a.id for a in accounts}
    now = held(db, user)
    # A goal below nothing (taken out past what it held) frees nothing extra.
    return sum(
        max(0, now.get(g.id, 0))
        for g in goals(db, user)
        if g.keep_aside and (g.account_id is None or g.account_id in ids)
    )


@dataclass(frozen=True)
class Split:
    """An account's balance, the goals inside it and what is left over."""

    views: list[GoalView]
    unallocated_cents: int


def splits(db: Session, user: User, balances: dict[int, int], today: date) -> dict[int, Split]:
    by_account: dict[int, list[GoalView]] = {}
    for v in views(db, user, today):
        if v.goal.account_id is not None:
            by_account.setdefault(v.goal.account_id, []).append(v)
    return {
        aid: Split(vs, calc.unallocated(balances.get(aid, 0), [v.held_cents for v in vs]))
        for aid, vs in by_account.items()
    }
