"""Bills: list, create and edit, mark paid and unpaid."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from fiscus.clock import utcnow_naive
from fiscus.domain import recurrence
from fiscus.forms import (
    FormError,
    choice,
    ends_after,
    int_or,
    iso_date,
    money,
    opt_date,
    req_date,
    text,
)
from fiscus.models import FREQUENCIES, Account, Bill, Category, Transaction, User
from fiscus.services import bills as svc
from fiscus.services import budgeting, ledger
from fiscus.services.access import get_owned, maybe_owned
from fiscus.web import DB, ctx, current_user, local_path, redirect, render

router = APIRouter()

FREQ_LABELS = {
    'weekly': 'Weekly',
    'fortnightly': 'Fortnightly',
    'monthly': 'Monthly',
    'bimonthly': 'Every 2 months',
    'quarterly': 'Quarterly',
    'yearly': 'Yearly',
    'one_off': 'One-off',
}


@router.get('/bills')
def list_bills(
    request: Request,
    category: int = 0,
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    c = ctx(request)
    today = c.clock.today()
    found = svc.user_bills(db, user, active_only=False)
    cats = {cat.id: cat.name for cat in ledger.categories(db, user)}
    if category in cats:
        found = [b for b in found if b.category_id == category]
    upcoming = svc.occurrences(db, user, today, today + timedelta(days=45), today)
    overdue = svc.overdue(db, user, today)
    nxt: dict[int, svc.Occurrence] = {}
    for o in upcoming:
        nxt.setdefault(o.bill.id, o)
    # Due further out than the list looks: just the date.
    later: dict[int, date] = {}
    for b in found:
        if b.active and b.id not in nxt:
            due = recurrence.next_on_or_after(b.next_due, b.frequency, today)
            if due is not None and (b.end_date is None or due <= b.end_date):
                later[b.id] = due
    monthly = {b.id: recurrence.monthly_equivalent(b.amount_cents, b.frequency) for b in found}
    return render(
        request,
        'bills/list.html',
        {
            'bills': found,
            'next': nxt,
            'later': later,
            'overdue': overdue,
            'monthly': monthly,
            'monthly_total': sum(monthly[b.id] for b in found if b.active),
            'cats': cats,
            'category': cats.get(category),
            'freq': FREQ_LABELS,
        },
    )


def _form(
    request: Request,
    db: Session,
    user: User,
    bill: Bill | None,
    error: str = '',
    values: dict[str, object] | None = None,
) -> Response:
    return render(
        request,
        'bills/form.html',
        {
            'bill': bill,
            'freq': FREQ_LABELS,
            'accounts': ledger.accounts(db, user),
            'cats': ledger.categories(db, user),
            'error': error,
            'values': values or {},
            'prefill_date': request.query_params.get('date', ''),
        },
        status_code=400 if error else 200,
    )


@router.get('/bills/new')
def new_bill(request: Request, user: User = Depends(current_user), db: Session = DB) -> Response:
    return _form(request, db, user, None)


@router.get('/bills/{bill_id}/edit')
def edit_bill(
    bill_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    return _form(request, db, user, get_owned(db, Bill, bill_id, user))


@router.post('/bills')
@router.post('/bills/{bill_id}')
async def save_bill(
    request: Request,
    bill_id: int | None = None,
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    c = ctx(request)
    bill = get_owned(db, Bill, bill_id, user) if bill_id else None
    form = await request.form()

    def f(key: str, default: str = '') -> str:
        v = form.get(key)
        return v if isinstance(v, str) else default

    try:
        name = text(f('name'), 'Name')
        amount = money(f('amount'), 'Amount', allow_negative=False)
        if amount == 0:
            raise FormError('Amount: enter more than zero')
        frequency = choice(f('frequency'), FREQUENCIES, 'Frequency')
        next_due = req_date(f('next_due'), 'Next due')
        end_date = ends_after(next_due, opt_date(f('end_date'), 'End date'))
    except (FormError, ValueError) as exc:
        return _form(request, db, user, bill, str(exc), dict(form))
    acct = maybe_owned(db, Account, int_or(f('account_id')), user)
    cat = maybe_owned(db, Category, int_or(f('category_id')), user)
    today = c.clock.today()
    # Saved-up lines keep what their bills gave them so far (D64).
    touched = {cat.id if cat else None, bill.category_id if bill else None}
    budgeting.settle(db, user, today, {cid for cid in touched if cid is not None})
    if bill is None:
        bill = Bill(
            user_id=user.id,
            created_at=utcnow_naive(c.clock),
            name=name,
            amount_cents=amount,
            frequency=frequency,
            next_due=next_due,
        )
        db.add(bill)
    bill.name = name
    bill.amount_cents = amount
    bill.frequency = frequency
    bill.next_due = next_due
    bill.end_date = end_date
    bill.account_id = acct.id if acct else None
    bill.category_id = cat.id if cat else None
    bill.active = ('active' in form) if bill_id else True
    db.flush()
    budgeting.follow_bills(db, user, today)
    return redirect(f'/bills/{bill.id}', flash='Bill saved', request=request)


@router.get('/bills/{bill_id}')
def show_bill(
    bill_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    c = ctx(request)
    today = c.clock.today()
    bill = get_owned(db, Bill, bill_id, user)
    occ = [
        o
        for o in svc.occurrences(
            db, user, today - timedelta(days=200), today + timedelta(days=200), today
        )
        if o.bill.id == bill.id
    ]
    return render(
        request,
        'bills/show.html',
        {
            'bill': bill,
            'occ': occ,
            'freq': FREQ_LABELS,
            'account': maybe_owned(db, Account, bill.account_id, user),
            'category': maybe_owned(db, Category, bill.category_id, user),
        },
    )


def _occ(db: Session, user: User, bill: Bill, due_raw: str) -> date:
    due = iso_date(due_raw)
    if due is None or not svc.is_occurrence(bill, due):
        raise HTTPException(status_code=404)
    return due


@router.get('/bills/{bill_id}/occurrence/{due}')
def occurrence(
    bill_id: int,
    due: str,
    request: Request,
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    """The calendar's event panel: details and quick actions."""
    c = ctx(request)
    bill = get_owned(db, Bill, bill_id, user)
    when = _occ(db, user, bill, due)
    today = c.clock.today()
    occs = [o for o in svc.occurrences(db, user, when, when, today) if o.bill.id == bill.id]
    if not occs:
        raise HTTPException(status_code=404)
    o = occs[0]
    txn = (
        db.get(Transaction, o.payment.transaction_id)
        if o.payment and o.payment.transaction_id
        else None
    )
    return render(request, 'bills/_occurrence.html', {'o': o, 'txn': txn})


@router.post('/bills/{bill_id}/occurrence/{due}/paid')
async def mark_paid(
    bill_id: int,
    due: str,
    request: Request,
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    c = ctx(request)
    bill = get_owned(db, Bill, bill_id, user)
    when = _occ(db, user, bill, due)
    form = await request.form()
    today = c.clock.today()
    before = ledger.balances(db, user, today)
    svc.mark_paid(db, c.clock, user, bill, when)
    return redirect(
        local_path(form.get('back'), f'/bills/{bill.id}'),
        flash=f'{bill.name} marked paid{ledger.now_at(db, user, before, today)}',
        request=request,
    )


@router.post('/bills/{bill_id}/occurrence/{due}/unpaid')
async def mark_unpaid(
    bill_id: int,
    due: str,
    request: Request,
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    bill = get_owned(db, Bill, bill_id, user)
    when = _occ(db, user, bill, due)
    today = ctx(request).clock.today()
    before = ledger.balances(db, user, today)
    svc.unmark(db, user, bill, when)
    form = await request.form()
    return redirect(
        local_path(form.get('back'), f'/bills/{bill.id}'),
        flash=f'{bill.name} marked unpaid{ledger.now_at(db, user, before, today)}',
        request=request,
    )


@router.post('/bills/{bill_id}/delete')
def delete_bill(
    bill_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    bill = get_owned(db, Bill, bill_id, user)
    if bill.category_id is not None:
        budgeting.settle(db, user, ctx(request).clock.today(), {bill.category_id})
    db.delete(bill)
    return redirect('/bills', flash='Bill deleted', request=request)
