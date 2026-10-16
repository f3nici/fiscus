"""Tax estimate. Every figure on the page is labelled an estimate."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from fiscus.domain import tax
from fiscus.domain.tax import TaxConfigError
from fiscus.forms import FormError, money
from fiscus.models import User
from fiscus.services import taxes
from fiscus.web import DB, ctx, current_user, redirect, render

router = APIRouter()


@router.get('/tax')
def tax_page(request: Request, user: User = Depends(current_user), db: Session = DB) -> Response:
    c = ctx(request)
    try:
        view = taxes.view(db, user, c.clock.today(), c.tax_dir)
    except TaxConfigError as exc:
        return render(
            request, 'error.html', {'title': 'No tax table', 'message': str(exc)}, status_code=500
        )
    return render(request, 'tax/index.html', {'v': view})


@router.post('/tax/deductions')
def save_deductions(
    request: Request,
    amount: str = Form(''),
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    label = tax.fy_label(ctx(request).clock.today())
    try:
        cents = money(amount, 'Deductions', allow_negative=False) if amount.strip() else 0
    except FormError as exc:
        return redirect('/tax', flash=str(exc), request=request)
    taxes.set_deductions(db, user, label, cents)
    return redirect('/tax', flash=f'Deductions for {label} saved', request=request)
