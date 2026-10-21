"""The dashboard."""

from __future__ import annotations

import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from fiscus.domain.forecast import ForecastResult
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
from fiscus.web import DB, ctx, current_user, render

router = APIRouter()


def chart(base: ForecastResult, free: list[int]) -> str:
    """The dashboard's chart: free to spend over the next three months, drawn
    by forecast-chart.js from a data attribute."""
    data = {'dates': [d.isoformat() for d in base.dates], 'free': free}
    return json.dumps(data, separators=(',', ':'))


@router.get('/')
def dashboard(request: Request, user: User = Depends(current_user), db: Session = DB) -> Response:
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
    rows = budgeting.rows(db, user, start, end)
    loan_lines = loans.budget_lines(db, user, start, end, today)
    summary = budgeting.summary(
        rows,
        budgeting.expected_pay(db, user, start, end, today),
        loan_cents=sum(x.budget_cents for x in loan_lines),
        loan_paid_cents=sum(x.paid_cents for x in loan_lines),
    )
    base = forecasting.base(db, c.clock, user, 3)
    spare = forecasting.free_to_spend(db, user, today, base)
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
            'free': spare,
            'chart': chart(base, spare.days),
            'tax': tax_view,
            'goals': goals.views(db, user, today),
            'loans': loan_rows,
            'has_any': has_any,
        },
    )


# The menu as a page, where the menu button goes if its script never ran; it
# opens the menu as a panel otherwise (D67).
@router.get('/more')
def more(request: Request, user: User = Depends(current_user)) -> Response:
    return render(request, 'more.html')
