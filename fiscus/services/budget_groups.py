"""How the budget page is laid out: groups of lines, each in an order set by hand."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fiscus.domain import arrange
from fiscus.models import BudgetGroup, Category, User
from fiscus.services.access import owned
from fiscus.services.budgeting import BudgetRow, order_amounts


def groups(db: Session, user: User) -> list[BudgetGroup]:
    q = owned(BudgetGroup, user).order_by(BudgetGroup.position, BudgetGroup.id)
    return list(db.execute(q).scalars())


def _key(cat: Category, monthly: int) -> tuple[bool, int, bool, int, str]:
    """Moved by hand first, in their order; then the lines never moved
    (position 0), income last, bigger budgets first, by name (D35)."""
    unmoved = cat.budget_position == 0
    return (unmoved, cat.budget_position, cat.is_income, -monthly, cat.name.lower())


@dataclass(frozen=True)
class Section:
    group: BudgetGroup | None
    rows: list[BudgetRow]

    @property
    def spending(self) -> list[BudgetRow]:
        return [r for r in self.rows if r.has_budget and not r.category.is_income]

    @property
    def budget_cents(self) -> int:
        return sum(r.budget_cents for r in self.spending)

    @property
    def spent_cents(self) -> int:
        return sum(r.actual_cents for r in self.rows if not r.category.is_income)

    @property
    def remaining_cents(self) -> int:
        return sum(r.remaining_cents for r in self.spending)

    @property
    def saved_cents(self) -> int:
        """What saved-up lines hold, which is in `remaining_cents` and can be
        more than a month's budget."""
        return sum(max(0, r.remaining_cents) for r in self.spending if r.saving)

    @property
    def over(self) -> int:
        return sum(1 for r in self.spending if r.state == 'over')

    @property
    def over_cents(self) -> int:
        """How far its lines over are over, added up."""
        return sum(r.over_cents for r in self.spending)


def sections(db: Session, user: User, rows: list[BudgetRow]) -> list[Section]:
    """Each group in order with its lines, then the lines in no group. Empty
    groups are kept so they can be filled."""
    amounts = order_amounts(db, user)
    ordered = sorted(rows, key=lambda r: _key(r.category, amounts.get(r.category.id, 0)))
    gs = groups(db, user)
    known = {g.id for g in gs}
    out = [Section(g, [r for r in ordered if r.category.budget_group_id == g.id]) for g in gs]
    rest = [r for r in ordered if r.category.budget_group_id not in known]
    if rest or not out:
        out.append(Section(None, rest))
    return out


def move_line(db: Session, user: User, cat: Category, step: int) -> None:
    """Swap a line with its neighbour among the budget lines in its group."""
    monthly = order_amounts(db, user)
    q = owned(Category, user).where(
        Category.budget_group_id.is_(None)
        if cat.budget_group_id is None
        else Category.budget_group_id == cat.budget_group_id
    )
    peers = [c for c in db.execute(q).scalars() if c.id in monthly or c.id == cat.id]
    peers.sort(key=lambda c: _key(c, monthly.get(c.id, 0)))
    order = arrange.moved([c.id for c in peers], cat.id, step)
    by_id = {c.id: c for c in peers}
    for n, cid in enumerate(order, start=1):
        by_id[cid].budget_position = n
    db.flush()


def move_group(db: Session, user: User, group: BudgetGroup, step: int) -> None:
    gs = groups(db, user)
    order = arrange.moved([g.id for g in gs], group.id, step)
    by_id = {g.id: g for g in gs}
    for n, gid in enumerate(order, start=1):
        by_id[gid].position = n
    db.flush()


def add_group(db: Session, user: User, name: str) -> BudgetGroup:
    last = db.execute(
        select(func.max(BudgetGroup.position)).where(BudgetGroup.user_id == user.id)
    ).scalar()
    group = BudgetGroup(user_id=user.id, name=name, position=(last or 0) + 1)
    db.add(group)
    db.flush()
    return group


def put_in(db: Session, user: User, cat: Category, group: BudgetGroup | None) -> None:
    """Move a line into a group (or out of all), at the end of it."""
    gid = group.id if group else None
    if cat.budget_group_id == gid:
        return
    q = select(func.max(Category.budget_position)).where(Category.user_id == user.id)
    q = q.where(
        Category.budget_group_id.is_(None) if gid is None else Category.budget_group_id == gid
    )
    cat.budget_group_id = gid
    cat.budget_position = (db.execute(q).scalar() or 0) + 1
    # Opened, so the line just put in it can be seen.
    if group is not None:
        group.collapsed = False
    db.flush()


def fold_all(db: Session, user: User, collapsed: bool) -> None:
    for g in groups(db, user):
        g.collapsed = collapsed
    db.flush()


def delete_group(db: Session, user: User, group: BudgetGroup) -> None:
    """Its lines go back to no group, keeping their order."""
    for cat in db.execute(
        owned(Category, user).where(Category.budget_group_id == group.id)
    ).scalars():
        cat.budget_group_id = None
    db.delete(group)
    db.flush()
