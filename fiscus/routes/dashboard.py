"""The dashboard."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from fiscus.domain.tax import TaxConfigError
from fiscus.models import Loan, User
from fiscus.services import (
    bills,
    budgeting,
    calendar_events,
    forecasting,
    goals,
    ledger,
    loans,
    pay,
    taxes,
)
from fiscus.services.access import owned
from fiscus.web import ctx, current_user, get_db, render

router = APIRouter()


def sparkline(values: list[int], width: int = 300, height: int = 60) -> dict[str, object]:
    """SVG polyline points, drawn on the server so the dashboard needs no chart script."""
    if not values:
        return {'points': '', 'zero': None, 'width': width, 'height': height}
    step = max(1, len(values) // width)
    vals = values[::step]
    lo, hi = min([*vals, 0]), max([*vals, 0])
    span = (hi - lo) or 1
    n = max(1, len(vals) - 1)
    pts = ' '.join(
        f'{i * width / n:.1f},{height - (v - lo) * height / span:.1f}' for i, v in enumerate(vals)
    )
    zero = height - (0 - lo) * height / span
    return {'points': pts, 'zero': f'{zero:.1f}', 'width': width, 'height': height}


@router.get('/')
def dashboard(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    c = ctx(request)
    today = c.clock.today()
    accts = ledger.accounts(db, user)
    bal = ledger.balances(db, user, today)
    upcoming = [
        d
        for d in calendar_events.grid(db, c.clock, user, 'agenda', today).days
        if d.date <= today + timedelta(days=14)
    ]
    start, end = budgeting.period(db, user, today)
    rows = budgeting.rows(db, user, start, end, today)
    loan_lines = loans.budget_lines(db, user, start, end, today)
    summary = budgeting.summary(
        rows,
        budgeting.expected_pay(db, user, start, end, today),
        loan_cents=sum(x.budget_cents for x in loan_lines),
        loan_paid_cents=sum(x.paid_cents for x in loan_lines),
    )
    base = forecasting.base(db, c.clock, user, 3)
    try:
        tax_view = taxes.view(db, user, today, c.tax_dir)
    except TaxConfigError:
        tax_view = None
    loan_rows = [
        loans.summarise(db, user, loan, today) for loan in db.execute(owned(Loan, user)).scalars()
    ]
    put_in = pay.to_put_in(db, user, today)
    overdue = bills.overdue(db, user, today)
    has_any = bool(accts) or bool(
        db.execute(select(Loan.id).where(Loan.user_id == user.id)).first()
    )
    return render(
        request,
        'dashboard.html',
        {
            'accounts': accts,
            'put_in': put_in,
            'overdue': overdue,
            'balances': bal,
            'total': sum(bal.get(a.id, 0) for a in accts),
            'spending': [r for r in rows if r.has_budget and not r.category.is_income][:8],
            'budget_end': end,
            'upcoming': upcoming,
            'budget': summary,
            'over': [r for r in rows if r.state == 'over'][:4],
            'forecast': base,
            'spark': sparkline(base.combined),
            'tax': tax_view,
            'goals': goals.views(db, user, today),
            'loans': loan_rows,
            'has_any': has_any,
        },
    )


@router.get('/more')
def more(request: Request, user: User = Depends(current_user)) -> Response:
    return render(request, 'more.html')
