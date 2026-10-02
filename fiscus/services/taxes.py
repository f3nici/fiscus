"""The user's tax position: year to date and projected to 30 June."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fiscus.domain import recurrence, tax
from fiscus.models import Account, Category, PaySchedule, Payslip, Transaction, User
from fiscus.services import ledger
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
    def should_set_aside_cents(self) -> int:
        """Tax owing on the year so far, beyond what salary withholding covered."""
        return max(0, self.ytd.owing_cents)

    @property
    def set_aside_gap_cents(self) -> int:
        return self.set_aside_actual_cents - self.should_set_aside_cents


def _business(db: Session, user: User, start: date, end: date) -> tuple[int, int]:
    cats = {c.id: c for c in db.execute(owned(Category, user)).scalars()}
    income = 0
    expenses = 0
    rows = db.execute(
        select(Transaction.category_id, func.sum(Transaction.amount_cents))
        .where(Transaction.user_id == user.id, Transaction.date >= start, Transaction.date <= end)
        .group_by(Transaction.category_id)
    )
    for cid, total in rows:
        cat = cats.get(cid) if cid else None
        if cat is None or not cat.is_business:
            continue
        if cat.is_income:
            income += int(total)
        elif cat.is_deductible:
            expenses += -int(total)
    return income, expenses


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
    b_income, b_exp = _business(db, user, fy_start, today)
    ytd = tax.position(year, gross, withheld, b_income, b_exp)

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
    for ps in db.execute(owned(PaySchedule, user)).scalars():
        for _d in recurrence.occurrences(
            ps.anchor_date, ps.frequency, first, fy_end, until=ps.end_date
        ):
            g = ps.gross_cents
            if g is None:
                g = round(ps.net_cents * ratio_gross) if ratio_gross else ps.net_cents
            w = ps.tax_withheld_cents
            if w is None:
                w = round(g * ratio_tax) if ratio_tax else g - ps.net_cents
            proj_gross += g
            proj_tax += w
    elapsed = (today - fy_start).days + 1
    total_days = (fy_end - fy_start).days + 1
    scale = total_days / elapsed if elapsed > 0 else 1
    projected = tax.position(
        year, proj_gross, proj_tax, round(b_income * scale), round(b_exp * scale)
    )
    set_aside = ledger.balances(db, user, today)
    actual = sum(
        set_aside.get(a.id, 0)
        for a in db.execute(owned(Account, user).where(Account.is_set_aside.is_(True))).scalars()
    )
    return TaxView(year, ytd, projected, actual, elapsed, total_days)
