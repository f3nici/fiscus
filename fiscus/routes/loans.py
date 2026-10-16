"""Home loans, rate history and the offset."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from fiscus.forms import FormError, choice, money, rate, req_date, text
from fiscus.models import Account, Loan, LoanRate, User
from fiscus.services import ledger, loans
from fiscus.services.access import get_owned, maybe_owned, owned
from fiscus.web import DB, ctx, current_user, redirect, render

router = APIRouter()
LOAN_FREQS = ('monthly', 'fortnightly', 'weekly')


@router.get('/loans')
def list_loans(request: Request, user: User = Depends(current_user), db: Session = DB) -> Response:
    c = ctx(request)
    found = db.execute(owned(Loan, user).order_by(Loan.name)).scalars().all()
    # One loan: straight into it.
    if len(found) == 1:
        return redirect(f'/loans/{found[0].id}', request=request)
    rows = [loans.summarise(db, user, loan, c.clock.today()) for loan in found]
    return render(request, 'loans/list.html', {'rows': rows})


def _form(
    request: Request, db: Session, user: User, loan: Loan | None, error: str = ''
) -> Response:
    return render(
        request,
        'loans/form.html',
        {
            'loan': loan,
            'freqs': LOAN_FREQS,
            'accounts': ledger.accounts(db, user),
            'error': error,
        },
        status_code=400 if error else 200,
    )


@router.get('/loans/new')
def new_loan(request: Request, user: User = Depends(current_user), db: Session = DB) -> Response:
    return _form(request, db, user, None)


@router.get('/loans/{loan_id}/edit')
def edit_loan(
    loan_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    return _form(request, db, user, get_owned(db, Loan, loan_id, user))


@router.post('/loans')
@router.post('/loans/{loan_id}')
def save_loan(
    request: Request,
    loan_id: int | None = None,
    name: str = Form(...),
    principal: str = Form(...),
    start_date: str = Form(...),
    term_years: int = Form(30),
    repayment_frequency: str = Form('monthly'),
    initial_rate: str = Form(''),
    balance: str = Form(...),
    balance_date: str = Form(...),
    repayment_override: str = Form(''),
    repayment_account_id: int = Form(0),
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    loan = get_owned(db, Loan, loan_id, user) if loan_id else None
    try:
        values = dict(
            name=text(name, 'Name', 80),
            principal_cents=money(principal, 'Amount borrowed', allow_negative=False),
            start_date=req_date(start_date, 'Start date'),
            term_months=max(1, min(40, term_years)) * 12,
            repayment_frequency=choice(repayment_frequency, LOAN_FREQS, 'Repayments'),
            balance_cents=money(balance, 'Balance owing', allow_negative=False),
            balance_date=req_date(balance_date, 'Balance as at'),
            # Nothing, or zero, is the repayment worked out from the loan.
            repayment_override_cents=(
                money(repayment_override, 'Repayment', allow_negative=False) or None
                if repayment_override.strip()
                else None
            ),
        )
        first_rate = rate(initial_rate, 'Interest rate') if loan is None else None
    except FormError as exc:
        return _form(request, db, user, loan, str(exc))
    acct = maybe_owned(db, Account, repayment_account_id, user)
    if loan is None:
        loan = Loan(user_id=user.id, **values)
        db.add(loan)
        db.flush()
        db.add(
            LoanRate(
                user_id=user.id,
                loan_id=loan.id,
                effective_date=loan.start_date,
                rate=first_rate or '0',
            )
        )
    else:
        for k, v in values.items():
            setattr(loan, k, v)
    loan.repayment_account_id = acct.id if acct else None
    db.flush()
    return redirect(f'/loans/{loan.id}', flash='Loan saved', request=request)


@router.get('/loans/{loan_id}')
def show_loan(
    loan_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    c = ctx(request)
    loan = get_owned(db, Loan, loan_id, user)
    summary = loans.summarise(db, user, loan, c.clock.today())
    offsets = [a for a in ledger.accounts(db, user) if a.id in summary.offset_ids]
    return render(
        request,
        'loans/show.html',
        {
            'loan': loan,
            's': summary,
            'offsets': offsets,
            'candidates': [
                a for a in ledger.accounts(db, user) if a.type == 'offset' and a.loan_id is None
            ],
        },
    )


@router.post('/loans/{loan_id}/rates')
def add_rate(
    loan_id: int,
    request: Request,
    effective_date: str = Form(...),
    new_rate: str = Form(...),
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    loan = get_owned(db, Loan, loan_id, user)
    try:
        when = req_date(effective_date, 'Effective date')
        value = rate(new_rate, 'Rate')
    except FormError as exc:
        return redirect(f'/loans/{loan.id}', flash=str(exc), request=request)
    existing = next((r for r in loan.rates if r.effective_date == when), None)
    if existing is not None:
        existing.rate = value
    else:
        db.add(LoanRate(user_id=user.id, loan_id=loan.id, effective_date=when, rate=value))
    return redirect(
        f'/loans/{loan.id}', flash=f'Rate {value}% from {when.isoformat()} saved', request=request
    )


@router.post('/loans/{loan_id}/rates/{rate_id}/delete')
def delete_rate(
    loan_id: int,
    rate_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    loan = get_owned(db, Loan, loan_id, user)
    r = get_owned(db, LoanRate, rate_id, user)
    if r.loan_id != loan.id or len(loan.rates) <= 1:
        return redirect(
            f'/loans/{loan.id}', flash='A loan keeps at least one rate', request=request
        )
    db.delete(r)
    return redirect(f'/loans/{loan.id}', flash='Rate removed', request=request)


@router.post('/loans/{loan_id}/offsets')
def link_offset(
    loan_id: int,
    request: Request,
    account_id: int = Form(...),
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    loan = get_owned(db, Loan, loan_id, user)
    acct = get_owned(db, Account, account_id, user)
    acct.type = 'offset'
    acct.loan_id = loan.id
    return redirect(
        f'/loans/{loan.id}', flash=f'{acct.name} now offsets {loan.name}', request=request
    )


@router.post('/loans/{loan_id}/offsets/{account_id}/unlink')
def unlink_offset(
    loan_id: int,
    account_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    loan = get_owned(db, Loan, loan_id, user)
    acct = get_owned(db, Account, account_id, user)
    if acct.loan_id == loan.id:
        acct.loan_id = None
    return redirect(f'/loans/{loan.id}', flash='Offset unlinked', request=request)


@router.post('/loans/{loan_id}/delete')
def delete_loan(
    loan_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    db.delete(get_owned(db, Loan, loan_id, user))
    return redirect('/loans', flash='Loan deleted', request=request)
