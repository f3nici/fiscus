"""The user's tax position: year to date and projected to 30 June."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from fiscus.domain import tax
from fiscus.models import Account, Payslip, TaxDeduction, User
from fiscus.money import round_div
from fiscus.services import ledger, pay
from fiscus.services.access import owned


@dataclass(frozen=True)
class TaxView:
    year: tax.TaxYear
    ytd: tax.Position
    projected: tax.Position
    set_aside_actual_cents: int
    days_elapsed: int
    days_in_year: int

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
    ytd = tax.position(year, gross, withheld, deductions)

    # Rest of the year: scheduled pays, with gross and withholding taken from the
    # schedule, or scaled from this year's payslips when the schedule has none.
    ratio_gross = (
        gross / sum(p.net_cents for p in slips)
        if slips and sum(p.net_cents for p in slips)
        else None
    )
    ratio_tax = withheld / gross if gross else None
    proj_gross, proj_tax = gross, withheld
    first = today + timedelta(days=1)
    for item in pay.items(db, user, first, fy_end, today):
        if item.slip is not None:
            proj_gross += item.slip.gross_cents
            proj_tax += item.slip.tax_withheld_cents
            continue
        ps, net = item.schedule, item.net_cents
        if ps is not None:
            # A varying pay's gross and tax scale with its average, not the guess.
            g = ps.gross_cents
            if g is not None and ps.varies and ps.net_cents:
                g = round(g * net / ps.net_cents)
            if g is None:
                g = round(net * ratio_gross) if ratio_gross else net
            w = ps.tax_withheld_cents
            if w is not None and ps.varies and ps.gross_cents:
                w = round(w * g / ps.gross_cents)
            if w is None:
                w = round(g * ratio_tax) if ratio_tax else g - net
            proj_gross += g
            proj_tax += w
    elapsed = (today - fy_start).days + 1
    total_days = (fy_end - fy_start).days + 1
    projected = tax.position(year, proj_gross, proj_tax, deductions)
    set_aside = ledger.balances(db, user, today)
    actual = sum(
        set_aside.get(a.id, 0)
        for a in db.execute(owned(Account, user).where(Account.is_set_aside.is_(True))).scalars()
    )
    return TaxView(year, ytd, projected, actual, elapsed, total_days)
