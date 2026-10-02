"""Monthly budget against actual."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from fiscus.domain import recurrence
from fiscus.forms import FormError, money
from fiscus.models import Budget, Category, User
from fiscus.services import budgeting, ledger
from fiscus.services.access import get_owned, owned
from fiscus.web import ctx, current_user, get_db, redirect, render

router = APIRouter()


@router.get('/budget')
def budget_page(
    request: Request,
    month: str = '',
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    c = ctx(request)
    today = c.clock.today()
    try:
        focus = date.fromisoformat(month + '-01') if month else today.replace(day=1)
    except ValueError:
        focus = today.replace(day=1)
    rows = budgeting.rows(db, user, focus, today)
    budgeted = {r.category.id for r in rows if r.budget}
    return render(
        request,
        'budget/index.html',
        {
            'rows': rows,
            'totals': budgeting.totals(rows),
            'month': focus,
            'prev': recurrence.add_months(focus, -1),
            'next': recurrence.add_months(focus, 1),
            'unbudgeted': [
                cat
                for cat in ledger.categories(db, user)
                if cat.id not in budgeted and not cat.is_transfer
            ],
        },
    )


@router.post('/budget')
def save_budget(
    request: Request,
    category_id: int = Form(...),
    amount: str = Form(...),
    use_in_forecast: bool = Form(False),
    back: str = Form('/budget'),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    cat = get_owned(db, Category, category_id, user)
    target = back if back.startswith('/budget') else '/budget'
    try:
        cents = money(amount, 'Amount', allow_negative=False)
    except FormError as exc:
        return redirect(target, flash=str(exc), request=request)
    row = db.execute(owned(Budget, user).where(Budget.category_id == cat.id)).scalar_one_or_none()
    if cents == 0:
        if row is not None:
            db.delete(row)
        return redirect(target, flash=f'Budget for {cat.name} removed', request=request)
    if row is None:
        row = Budget(user_id=user.id, category_id=cat.id, monthly_cents=cents)
        db.add(row)
    row.monthly_cents = cents
    row.use_in_forecast = use_in_forecast
    return redirect(target, flash=f'Budget for {cat.name} saved', request=request)
