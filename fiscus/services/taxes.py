"""The user's tax position: year to date and projected to 30 June."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from fiscus.domain import tax
from fiscus.domain.pay import OpenPayday
from fiscus.models import Account, Goal, Payslip, TaxDeduction, User
from fiscus.money import round_div
from fiscus.services import goals, ledger, pay
from fiscus.services.access import owned


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
        """Projected tax still to pay at 30 June, beyond what withholding covers."""
        return max(0, self.projected.owing_cents)

    @property
    def should_set_aside_cents(self) -> int:
        """The year-end amount saved evenly over the year, as far as today."""
        return round_div(self.year_end_set_aside_cents * self.days_elapsed, self.days_in_year)

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
    first = today + timedelta(days=1)
    for item in pay.items(db, user, first, fy_end, today):
        if item.slip is not None:
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
    elapsed = (today - fy_start).days + 1
    total_days = (fy_end - fy_start).days + 1
    projected = tax.position(year, proj_gross, proj_tax, deductions)
    ytd = tax.share_of(projected, gross, withheld)
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
