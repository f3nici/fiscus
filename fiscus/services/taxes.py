"""The user's tax position: year to date and projected to 30 June."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fiscus.clock import Clock, utcnow_naive
from fiscus.domain import tax
from fiscus.domain.pay import OpenPayday
from fiscus.models import Account, Goal, GoalMove, Payslip, TaxDeduction, TaxPayment, User
from fiscus.money import round_div
from fiscus.services import goals, ledger, pay
from fiscus.services.access import maybe_owned, owned


@dataclass(frozen=True)
class TaxView:
    year: tax.TaxYear
    ytd: tax.Position
    projected: tax.Position
    set_aside_actual_cents: int
    days_elapsed: int
    days_in_year: int
    # The goal holding tax money, and what of the actual figure it adds.
    goal: Goal | None = None
    goal_cents: int = 0
    # Paydays this year with no payslip: not in these figures.
    missing: tuple[OpenPayday, ...] = ()
    # Schedules projected with nothing withheld, for want of any figures.
    untaxed: tuple[str, ...] = ()

    @property
    def year_end_set_aside_cents(self) -> int:
        """Projected tax still to pay at 30 June, beyond what withholding and
        the BAS payments for the year cover."""
        return max(0, self.projected.owing_cents)

    @property
    def should_set_aside_cents(self) -> int:
        """What withholding will not cover, saved evenly over the year, as far
        as today, less what was paid with a BAS for the year (D75)."""
        evenly = round_div(max(0, self.projected.due_cents) * self.days_elapsed, self.days_in_year)
        return max(0, evenly - self.projected.paid_cents)

    @property
    def left_to_save_per_week_cents(self) -> int:
        """What still has to go in each week to reach the year-end amount by 30 June."""
        weeks = max(1, (self.days_in_year - self.days_elapsed + 6) // 7)
        short = self.year_end_set_aside_cents - self.set_aside_actual_cents
        return max(0, round_div(short, weeks))

    @property
    def set_aside_gap_cents(self) -> int:
        return self.set_aside_actual_cents - self.should_set_aside_cents


def deductions_row(db: Session, user: User, label: str) -> TaxDeduction | None:
    return db.execute(owned(TaxDeduction, user).where(TaxDeduction.year == label)).scalar()


def set_deductions(db: Session, user: User, label: str, cents: int) -> None:
    row = deductions_row(db, user, label)
    if row is None:
        db.add(TaxDeduction(user_id=user.id, year=label, amount_cents=cents))
    else:
        row.amount_cents = cents
    db.flush()


def view(db: Session, user: User, today: date, tax_dir: Path | None = None) -> TaxView:
    fy_start, fy_end = tax.fy_bounds(today)
    year = tax.load_year(tax.fy_label(today), tax_dir)

    slips = (
        db.execute(owned(Payslip, user).where(Payslip.date >= fy_start, Payslip.date <= today))
        .scalars()
        .all()
    )
    gross = sum(p.gross_cents for p in slips)
    withheld = sum(p.tax_withheld_cents for p in slips)
    row = deductions_row(db, user, year.label)
    deductions = row.amount_cents if row else 0

    # Rest of the year: each payday's payslip once it is in, else the
    # schedule's expected pay with its gross and tax (tax.gross_and_tax).
    per_job = _recent_slips(db, user, today)
    proj_gross, proj_tax = gross, withheld
    untaxed: list[str] = []
    expected = pay.schedules(db, user, today)
    # From today: a payday today with no payslip yet is expected, not missing.
    for item in pay.items(db, user, today, fy_end, today, expected):
        if item.slip is not None:
            if item.slip.date <= today:
                continue  # already in the year to date
            proj_gross += item.slip.gross_cents
            proj_tax += item.slip.tax_withheld_cents
            continue
        ps = item.schedule
        if ps is None:
            continue
        figures = tax.PayFigures(ps.gross_cents, ps.tax_withheld_cents, ps.net_cents, ps.varies)
        g, w, known = tax.gross_and_tax(item.net_cents, figures, per_job.get(ps.id))
        proj_gross += g
        proj_tax += w
        if not known and ps.name not in untaxed:
            untaxed.append(ps.name)
    # Irregular pay has no paydays: a typical month for each month left, from
    # tomorrow, as the forecast has it (or each month as it went last year,
    # D77); today's payments are in the year to date (D74). Its own gross and
    # tax are for a typical month.
    for e in expected:
        ps = e.schedule
        if not pay.is_irregular(ps):
            continue
        last = fy_end if ps.end_date is None else min(fy_end, ps.end_date)
        first = max(today + timedelta(days=1), ps.anchor_date)
        net = sum(c for _d, c in e.spread(first, last))
        if net <= 0:
            continue
        typed = ps.net_cents > 0
        figures = tax.PayFigures(
            ps.gross_cents if typed else None,
            ps.tax_withheld_cents if typed else None,
            ps.net_cents,
            varies=True,
        )
        g, w, known = tax.gross_and_tax(net, figures, per_job.get(ps.id))
        proj_gross += g
        proj_tax += w
        if not known and ps.name not in untaxed:
            untaxed.append(ps.name)
    elapsed = (today - fy_start).days + 1
    total_days = (fy_end - fy_start).days + 1
    projected = tax.position(
        year, proj_gross, proj_tax, deductions, paid_for(db, user, fy_start, fy_end)
    )
    ytd = tax.share_of(projected, gross, withheld, paid_for(db, user, fy_start, fy_end, today))
    bal = ledger.balances(db, user, today)
    set_aside = db.execute(
        owned(Account, user).where(Account.is_set_aside.is_(True), Account.archived.is_(False))
    ).scalars()
    ids = {a.id for a in set_aside}
    actual = sum(bal.get(i, 0) for i in ids)
    # The tax goal counts too, unless it sits in a set-aside account whose
    # balance already holds it.
    goal = goals.tax_goal(db, user)
    goal_cents = 0
    if goal is not None and goal.account_id not in ids:
        goal_cents = max(0, goals.held(db, user).get(goal.id, 0))
    return TaxView(
        year,
        ytd,
        projected,
        actual + goal_cents,
        elapsed,
        total_days,
        goal,
        goal_cents,
        missing=tuple(pay.open_paydays(db, user, fy_start, today - timedelta(days=1))),
        untaxed=tuple(untaxed),
    )


def _recent_slips(db: Session, user: User, today: date) -> dict[int, tuple[int, int, int]]:
    """Each schedule's payslips over the last year added up: (gross, withheld, net)."""
    out: dict[int, tuple[int, int, int]] = {}
    for s in db.execute(
        owned(Payslip, user).where(
            Payslip.schedule_id.is_not(None),
            Payslip.date > today - timedelta(days=365),
            Payslip.date <= today,
            Payslip.net_cents > 0,
        )
    ).scalars():
        if s.schedule_id is None:
            continue
        g, w, n = out.get(s.schedule_id, (0, 0, 0))
        out[s.schedule_id] = (g + s.gross_cents, w + s.tax_withheld_cents, n + s.net_cents)
    return out


# ------------------------------------------------------------ BAS payments


def paid_for(
    db: Session, user: User, fy_start: date, fy_end: date, as_of: date | None = None
) -> int:
    """Tax paid with a BAS for the quarters of a financial year (D75), paid
    by the end of `as_of` when given."""
    q = select(func.coalesce(func.sum(TaxPayment.amount_cents), 0)).where(
        TaxPayment.user_id == user.id,
        TaxPayment.quarter >= fy_start,
        TaxPayment.quarter <= fy_end,
    )
    if as_of is not None:
        q = q.where(TaxPayment.date <= as_of)
    return int(db.scalar(q) or 0)


def payments(db: Session, user: User, since: date) -> list[TaxPayment]:
    """BAS payments for the quarters from `since`, the latest quarter first."""
    return list(
        db.execute(
            owned(TaxPayment, user)
            .where(TaxPayment.quarter >= since)
            .order_by(TaxPayment.quarter.desc(), TaxPayment.date.desc(), TaxPayment.id.desc())
        ).scalars()
    )


def taken_from(db: Session, user: User, row: TaxPayment) -> Goal | Account | None:
    """Where a BAS payment came from: the goal it was taken out of, the
    account it came off, or neither."""
    move = maybe_owned(db, GoalMove, row.goal_move_id, user)
    if move is not None:
        return db.get(Goal, move.goal_id)
    txn = ledger.linked(db, user, row)
    return db.get(Account, txn.account_id) if txn is not None else None


def goal_room(db: Session, user: User, goal: Goal, row: TaxPayment | None) -> int:
    """The most a BAS payment can take out of `goal`: what it holds, and what
    this payment took out of it already."""
    room = goals.held(db, user).get(goal.id, 0)
    move = maybe_owned(db, GoalMove, row.goal_move_id if row else None, user)
    if move is not None and move.goal_id == goal.id:
        room -= move.amount_cents
    return room


def new_payment(
    db: Session, clock: Clock, user: User, *, cents: int, day: date, quarter: date
) -> TaxPayment:
    row = TaxPayment(
        user_id=user.id,
        date=day,
        quarter=quarter,
        amount_cents=cents,
        created_at=utcnow_naive(clock),
    )
    db.add(row)
    db.flush()
    return row


def pay_tax(
    db: Session,
    clock: Clock,
    user: User,
    row: TaxPayment,
    *,
    cents: int,
    day: date,
    quarter: date,
    goal: Goal | None,
    account: Account | None,
) -> None:
    """Record a BAS payment, new or changed (D75). Out of `goal` it is a goal
    move taking the money out, which comes off the goal's account (D73); from
    `account` it writes a transaction there; with neither it moves nothing.
    A changed one keeps its goal move or transaction, and so when it was put
    in (D45), while it stays where it was. A new `row` is in the session
    already (`new_payment`)."""
    row.date, row.quarter, row.amount_cents = day, quarter, cents
    db.flush()
    what = f'BAS {tax.quarter_label(quarter)}'
    move = maybe_owned(db, GoalMove, row.goal_move_id, user)
    if move is not None and (goal is None or move.goal_id != goal.id):
        row.goal_move_id = None
        db.delete(move)
        db.flush()
        move = None
    if goal is None:
        ledger.follow(
            db,
            clock,
            user,
            row,
            account_id=account.id if account else None,
            day=day,
            cents=-cents if account else 0,
            description=what,
        )
        return
    ledger.follow(db, clock, user, row, account_id=None, day=day, cents=0, description=what)
    if move is None:
        move = goals.take_out(db, clock, user, goal, cents, day, what)
        row.goal_move_id = move.id
        db.flush()
        return
    move.date, move.amount_cents, move.note = day, -cents, what
    ledger.follow(
        db, clock, user, move, account_id=goal.account_id, day=day, cents=-cents, description=what
    )


def unpay_tax(db: Session, user: User, row: TaxPayment) -> None:
    """Remove a BAS payment, with the goal move or transaction it made."""
    move = maybe_owned(db, GoalMove, row.goal_move_id, user)
    row.goal_move_id = None
    db.flush()
    if move is not None:
        db.delete(move)
    db.delete(row)
    db.flush()
