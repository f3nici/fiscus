"""Accounts."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fiscus.clock import utcnow_naive
from fiscus.forms import FormError, choice, money, text
from fiscus.models import ACCOUNT_TYPES, Account, Import, Loan, Transaction, User
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


def _form(
    request: Request, db: Session, user: User, account: Account | None, error: str = ''
) -> Response:
    loans = db.execute(owned(Loan, user)).scalars().all()
    return render(
        request,
        'accounts/form.html',
        {
            'account': account,
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
    return redirect(f'/accounts/{acct.id}', flash='Account saved', request=request)


@router.get('/accounts/{account_id}')
def show_account(
    account_id: int,
    request: Request,
    page: int = 1,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    acct = get_owned(db, Account, account_id, user)
    page = max(1, page)
    txns = (
        db.execute(
            owned(Transaction, user)
            .where(Transaction.account_id == acct.id)
            .order_by(Transaction.date.desc(), Transaction.id.desc())
            .offset((page - 1) * 50)
            .limit(51)
        )
        .scalars()
        .all()
    )
    count = (
        db.scalar(
            select(func.count()).select_from(Transaction).where(Transaction.account_id == acct.id)
        )
        or 0
    )
    imports = (
        db.execute(
            owned(Import, user)
            .where(Import.account_id == acct.id)
            .order_by(Import.id.desc())
            .limit(10)
        )
        .scalars()
        .all()
    )
    cats = {c.id: c for c in ledger.categories(db, user)}
    return render(
        request,
        'accounts/show.html',
        {
            'account': acct,
            'balance': ledger.balances(db, user).get(acct.id, 0),
            'txns': txns[:50],
            'more': len(txns) > 50,
            'page': page,
            'count': count,
            'imports': imports,
            'cats': cats,
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
    return redirect('/accounts', flash=f'Deleted {acct.name} and its transactions', request=request)
