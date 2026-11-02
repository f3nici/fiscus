"""Tax estimate. Every figure on the page is labelled an estimate."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from fiscus.domain import tax
from fiscus.domain.tax import TaxConfigError
from fiscus.forms import FormError, int_or, iso_date, money, req_date
from fiscus.models import Account, Goal, TaxPayment, User
from fiscus.money import fmt
from fiscus.services import goals, ledger, taxes
from fiscus.services.access import get_owned, maybe_owned
from fiscus.web import DB, ctx, current_user, redirect, render

router = APIRouter()
# Quarters offered for a BAS payment: this one and the seven before it.
QUARTERS_SHOWN = 8


@router.get('/tax')
def tax_page(request: Request, user: User = Depends(current_user), db: Session = DB) -> Response:
    c = ctx(request)
    today = c.clock.today()
    try:
        view = taxes.view(db, user, today, c.tax_dir)
    except TaxConfigError as exc:
        return render(
            request, 'error.html', {'title': 'No tax table', 'message': str(exc)}, status_code=500
        )
    editing = maybe_owned(db, TaxPayment, int_or(request.query_params.get('payment')), user)
    tax_goal = goals.tax_goal(db, user)
    accounts = ledger.accounts(db, user)
    quarters = [tax.quarter_before(today, n) for n in range(QUARTERS_SHOWN)]
    if editing is not None and editing.quarter not in quarters:
        quarters.append(editing.quarter)
    # This financial year's and last year's: April to June is paid in July.
    last_year = tax.fy_bounds(view.year.start.replace(year=view.year.start.year - 1))[0]
    shown = [
        (p, _from_label(taxes.taken_from(db, user, p)), tax.fy_label(p.quarter))
        for p in taxes.payments(db, user, last_year)
    ]
    if editing is not None:
        source = taxes.taken_from(db, user, editing)
        paid_from = _key(source)
    else:
        set_aside = next((a for a in accounts if a.is_set_aside), None)
        spending = ledger.spending_account(db, user, accounts)
        paid_from = (
            'goal'
            if tax_goal
            else f'account:{set_aside.id}'
            if set_aside
            else f'account:{spending}'
            if spending
            else ''
        )
    return render(
        request,
        'tax/index.html',
        {
            'v': view,
            'payments': shown,
            'editing': editing,
            'tax_goal': tax_goal,
            'accounts': accounts,
            'quarters': quarters,
            'quarter_label': tax.quarter_label,
            'paid_from': paid_from,
            'default_quarter': editing.quarter if editing else tax.quarter_before(today),
        },
    )


def _from_label(source: Goal | Account | None) -> str:
    if isinstance(source, Goal):
        return f'out of {source.name}'
    if isinstance(source, Account):
        return f'from {source.name}'
    return 'not through Fiscus'


def _key(source: Goal | Account | None) -> str:
    if isinstance(source, Goal):
        return 'goal'
    if isinstance(source, Account):
        return f'account:{source.id}'
    return ''


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


def _source(db: Session, user: User, value: str) -> tuple[Goal | None, Account | None]:
    """Where a BAS payment comes from: 'goal' (the tax goal), 'account:<id>',
    or '' for neither. Raises FormError."""
    if value == 'goal':
        goal = goals.tax_goal(db, user)
        if goal is None:
            raise FormError('Mark a goal as your tax money first, or choose an account')
        return goal, None
    kind, _, aid = value.partition(':')
    if kind == 'account':
        acct = maybe_owned(db, Account, int_or(aid), user)
        if acct is None:
            raise FormError('Choose an account')
        return None, acct
    return None, None


def _quarter(value: str) -> date:
    q = iso_date(value)
    if q is None or tax.quarter_of(q) != q:
        raise FormError('Choose the quarter it is for')
    return q


@router.post('/tax/payments')
@router.post('/tax/payments/{payment_id}')
def save_payment(
    request: Request,
    payment_id: int | None = None,
    amount: str = Form(...),
    paid_on: str = Form(''),
    quarter: str = Form(...),
    paid_from: str = Form(''),
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    """A BAS payment: income tax paid ahead for a quarter (D75)."""
    row = get_owned(db, TaxPayment, payment_id, user) if payment_id else None
    c = ctx(request)
    today = c.clock.today()
    back = f'/tax?payment={row.id}#bas' if row else '/tax#bas'
    try:
        cents = money(amount, 'Amount', allow_negative=False)
        if cents == 0:
            raise FormError('Enter an amount')
        day = req_date(paid_on, 'Paid on') if paid_on.strip() else today
        q = _quarter(quarter)
        goal, acct = _source(db, user, paid_from)
        if goal is not None:
            room = taxes.goal_room(db, user, goal, row)
            if cents > room:
                raise FormError(
                    f'{goal.name} holds {fmt(max(0, room))}, so that is the most to take out'
                    ' of it. Pay the rest from an account as a second payment.'
                )
    except FormError as exc:
        return redirect(back, flash=str(exc), request=request)
    before = ledger.balances(db, user, today)
    if row is None:
        row = taxes.new_payment(db, c.clock, user, cents=cents, day=day, quarter=q)
    taxes.pay_tax(db, c.clock, user, row, cents=cents, day=day, quarter=q, goal=goal, account=acct)
    flash = (
        f'{fmt(cents)} paid with your BAS for {tax.quarter_label(q)}, in {tax.fy_label(q)}'
        f'{ledger.now_at(db, user, before, today)}'
    )
    return redirect('/tax#bas', flash=flash, request=request)


@router.post('/tax/payments/{payment_id}/delete')
def delete_payment(
    payment_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    row = get_owned(db, TaxPayment, payment_id, user)
    today = ctx(request).clock.today()
    before = ledger.balances(db, user, today)
    taxes.unpay_tax(db, user, row)
    flash = f'BAS payment removed{ledger.now_at(db, user, before, today)}'
    return redirect('/tax#bas', flash=flash, request=request)
