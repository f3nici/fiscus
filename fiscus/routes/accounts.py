"""Accounts."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from fiscus.clock import utcnow_naive
from fiscus.forms import FormError, choice, money, text
from fiscus.models import ACCOUNT_TYPES, Account, Loan, User
from fiscus.services import ledger
from fiscus.services.access import get_owned, maybe_owned, owned
from fiscus.web import ctx, current_user, get_db, redirect, render

router = APIRouter()

TYPE_LABELS = {
    'transaction': 'Transaction',
    'offset': 'Offset',
    'savings': 'Savings',
    'credit_card': 'Credit card',
}


@router.get('/accounts')
def list_accounts(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    accts = ledger.accounts(db, user, include_archived=True)
    bal = ledger.balances(db, user)
    loans = {loan.id: loan for loan in db.execute(owned(Loan, user)).scalars()}
    return render(
        request,
        'accounts/list.html',
        {
            'accounts': accts,
            'balances': bal,
            'loans': loans,
            'labels': TYPE_LABELS,
            'total': sum(bal.get(a.id, 0) for a in accts if not a.archived),
        },
    )


@router.post('/accounts/balances')
async def update_balances(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    """Type in what every account is at today. Blank fields are left alone."""
    form = await request.form()
    today = ctx(request).clock.today()
    changed = 0
    for acct in ledger.accounts(db, user):
        raw = form.get(f'balance_{acct.id}')
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            cents = money(raw, acct.name)
        except FormError as exc:
            return redirect('/accounts', flash=str(exc), request=request)
        ledger.set_balance(db, user, acct, cents, today)
        changed += 1
    msg = (
        f'Updated {changed} balance{"" if changed == 1 else "s"}'
        if changed
        else 'Nothing to update'
    )
    return redirect('/accounts', flash=msg, request=request)


@router.post('/accounts/{account_id}/balance')
def update_balance(
    account_id: int,
    request: Request,
    balance: str = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    acct = get_owned(db, Account, account_id, user)
    try:
        cents = money(balance, 'Balance')
    except FormError as exc:
        return redirect(f'/accounts/{acct.id}', flash=str(exc), request=request)
    ledger.set_balance(db, user, acct, cents, ctx(request).clock.today())
    return redirect(f'/accounts/{acct.id}', flash=f'{acct.name} balance updated', request=request)


def _form(
    request: Request, db: Session, user: User, account: Account | None, error: str = ''
) -> Response:
    loans = db.execute(owned(Loan, user)).scalars().all()
    return render(
        request,
        'accounts/form.html',
        {
            'account': account,
            'balance': ledger.balances(db, user).get(account.id, 0) if account else 0,
            'types': ACCOUNT_TYPES,
            'labels': TYPE_LABELS,
            'loans': loans,
            'error': error,
        },
        status_code=400 if error else 200,
    )


@router.get('/accounts/new')
def new_account(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    return _form(request, db, user, None)


@router.get('/accounts/{account_id}/edit')
def edit_account(
    account_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    return _form(request, db, user, get_owned(db, Account, account_id, user))


@router.post('/accounts')
@router.post('/accounts/{account_id}')
def save_account(
    request: Request,
    account_id: int | None = None,
    name: str = Form(...),
    type: str = Form(...),
    opening_balance: str = Form('0'),
    balance: str = Form(''),
    is_set_aside: bool = Form(False),
    loan_id: int = Form(0),
    archived: bool = Form(False),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    acct = get_owned(db, Account, account_id, user) if account_id else None
    try:
        clean_name = text(name, 'Name', 80)
        clean_type = choice(type, ACCOUNT_TYPES, 'Type')
        opening = money(opening_balance, 'Opening balance')
        current = money(balance, 'Balance') if balance.strip() else None
        loan = maybe_owned(db, Loan, loan_id, user)
        if loan is not None and clean_type != 'offset':
            raise FormError('Only an offset account can be linked to a loan')
    except FormError as exc:
        return _form(request, db, user, acct, str(exc))
    if acct is None:
        acct = Account(user_id=user.id, created_at=utcnow_naive(ctx(request).clock))
        db.add(acct)
    acct.name = clean_name
    acct.type = clean_type
    acct.opening_balance_cents = opening
    acct.is_set_aside = is_set_aside
    acct.loan_id = loan.id if loan else None
    acct.archived = archived
    db.flush()
    today = ctx(request).clock.today()
    if current is not None and (
        account_id is None or current != ledger.balances(db, user, today).get(acct.id, 0)
    ):
        ledger.set_balance(db, user, acct, current, today)
    return redirect(f'/accounts/{acct.id}', flash='Account saved', request=request)


@router.get('/accounts/{account_id}')
def show_account(
    account_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    acct = get_owned(db, Account, account_id, user)
    return render(
        request,
        'accounts/show.html',
        {
            'account': acct,
            'balance': ledger.balances(db, user).get(acct.id, 0),
            'labels': TYPE_LABELS,
        },
    )


@router.post('/accounts/{account_id}/delete')
def delete_account(
    account_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    acct = get_owned(db, Account, account_id, user)
    db.delete(acct)
    return redirect('/accounts', flash=f'Deleted {acct.name}', request=request)
