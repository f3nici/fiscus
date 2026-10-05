"""Savings goals: what each holds, the split of an account, and their budget lines."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fiscus.clock import Clock, utcnow_naive
from fiscus.domain import goals as calc
from fiscus.models import Account, Goal, GoalMove, User
from fiscus.money import fmt
from fiscus.services import budgeting
from fiscus.services.access import owned


def goals(db: Session, user: User, *, include_archived: bool = False) -> list[Goal]:
    q = owned(Goal, user).order_by(Goal.archived, Goal.target_date.is_(None), Goal.target_date)
    if not include_archived:
        q = q.where(Goal.archived.is_(False))
    return list(db.execute(q.order_by(Goal.name)).scalars())


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
    per_month_cents: int  # what the budget asks for this budget month
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


@dataclass(frozen=True)
class BudgetGoal:
    """A goal's line in a budget month: an even share of what was left to save
    at the start of the month, and what has gone in during it."""

    goal: Goal
    budget_cents: int
    paid_cents: int
    held_cents: int
    months_left: int

    @property
    def remaining_cents(self) -> int:
        return self.budget_cents - self.paid_cents


def budget_lines(db: Session, user: User, start: date, end: date) -> list[BudgetGoal]:
    """Every goal with a target and a date still ahead of the month's start."""
    first = budgeting.start_day(db, user)
    before = held(db, user, as_of=start - timedelta(days=1))
    during = held(db, user, as_of=end, since=start)
    out = []
    for g in goals(db, user):
        if g.target_cents is None or g.target_date is None:
            continue
        months = calc.months_left(start, g.target_date, first)
        need = calc.per_month(g.target_cents - before.get(g.id, 0), months)
        paid = during.get(g.id, 0)
        if need or paid:
            out.append(BudgetGoal(g, need, paid, before.get(g.id, 0) + paid, months))
    return out


def held_outside(db: Session, user: User, accounts: list[Account]) -> int:
    """What open goals kept aside hold, in `accounts` or in no account: money
    that is in the accounts but not free to spend."""
    ids = {a.id for a in accounts}
    now = held(db, user)
    return sum(
        now.get(g.id, 0)
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
