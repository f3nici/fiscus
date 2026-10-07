"""Bills: list, create and edit (owner), mark paid, match and unmatch (each participant)."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from fiscus.clock import utcnow_naive
from fiscus.domain import recurrence, splits
from fiscus.forms import (
    FormError,
    choice,
    ends_after,
    int_or,
    money,
    opt_date,
    pct_bp,
    req_date,
    text,
)
from fiscus.models import (
    FREQUENCIES,
    SPLIT_MODES,
    Account,
    Bill,
    BillShare,
    Category,
    Transaction,
    User,
)
from fiscus.services import bills as svc
from fiscus.services import ledger
from fiscus.services.access import get_owned, maybe_owned, owned_bill, visible_bill
from fiscus.web import ctx, current_user, get_db, local_path, redirect, render

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
    db: Session = Depends(get_db),
) -> Response:
    c = ctx(request)
    today = c.clock.today()
    shares = svc.user_shares(db, user, active_only=False)
    cats = {cat.id: cat.name for cat in ledger.categories(db, user)}
    if category in cats:
        shares = [s for s in shares if s.category_id == category]
    upcoming = svc.occurrences(db, user, today, today + timedelta(days=45), today)
    overdue = svc.overdue(db, user, today)
    nxt: dict[int, svc.Occurrence] = {}
    for o in upcoming:
        nxt.setdefault(o.bill.id, o)
    # Due further out than the list looks: just the date.
    later: dict[int, date] = {}
    for s in shares:
        b = s.bill
        if b.active and b.id not in nxt:
            due = recurrence.next_on_or_after(b.next_due, b.frequency, today)
            if due is not None and (b.end_date is None or due <= b.end_date):
                later[b.id] = due
    amounts = {s.bill_id: svc.share_amounts(s.bill)[user.id] for s in shares}
    monthly = {
        s.bill_id: recurrence.monthly_equivalent(amounts[s.bill_id], s.bill.frequency)
        for s in shares
    }
    owners = {u.id: u for u in db.execute(select(User)).scalars()}
    return render(
        request,
        'bills/list.html',
        {
            'shares': shares,
            'next': nxt,
            'later': later,
            'overdue': overdue,
            'amounts': amounts,
            'monthly': monthly,
            'monthly_total': sum(monthly[s.bill_id] for s in shares if s.bill.active),
            'cats': cats,
            'category': cats.get(category),
            'freq': FREQ_LABELS,
            'owners': owners,
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
    others = (
        db.execute(
            select(User)
            .where(User.is_active.is_(True), User.id != user.id)
            .order_by(User.display_name)
        )
        .scalars()
        .all()
    )
    shares = {s.user_id: s for s in bill.shares} if bill else {}
    return render(
        request,
        'bills/form.html',
        {
            'bill': bill,
            'shares': shares,
            'others': others,
            'freq': FREQ_LABELS,
            'modes': SPLIT_MODES,
            'accounts': ledger.accounts(db, user),
            'cats': ledger.categories(db, user),
            'error': error,
            'values': values or {},
            'prefill_date': request.query_params.get('date', ''),
        },
        status_code=400 if error else 200,
    )


@router.get('/bills/new')
def new_bill(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    return _form(request, db, user, None)


@router.get('/bills/{bill_id}/edit')
def edit_bill(
    bill_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    return _form(request, db, user, owned_bill(db, bill_id, user))


@router.post('/bills/rematch')
def rematch(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    c = ctx(request)
    since = c.clock.today() - timedelta(days=120)
    txns = list(
        db.execute(
            select(Transaction).where(Transaction.user_id == user.id, Transaction.date >= since)
        ).scalars()
    )
    n = svc.auto_match(db, c.clock, user, txns)
    return redirect('/bills', flash=f'Matched {n} payments', request=request)


@router.post('/bills')
@router.post('/bills/{bill_id}')
async def save_bill(
    request: Request,
    bill_id: int | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    c = ctx(request)
    bill = owned_bill(db, bill_id, user) if bill_id else None
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
        mode = choice(f('split_mode', 'equal'), SPLIT_MODES, 'Split')
        participants = [int_or(v) for v in form.getlist('participants') if int_or(v) > 0]
        valid_others = {
            u.id for u in db.execute(select(User).where(User.is_active.is_(True))).scalars()
        }
        participants = [
            p for p in dict.fromkeys(participants) if p in valid_others and p != user.id
        ]
        everyone = [user.id, *participants]

        def share_value(uid: int) -> int:
            raw = f(f'split_{uid}')
            if mode == 'percent':
                return pct_bp(raw or '0', 'Split percentage')
            if mode == 'fixed':
                return money(raw or '0', 'Split amount', allow_negative=False)
            return 0

        values = {uid: share_value(uid) for uid in everyone}
        if mode != 'equal' and len(everyone) == 1:
            mode = 'equal'
        splits.compute(amount, mode, [splits.ShareSpec(u, v) for u, v in values.items()], user.id)
        tolerance = max(0, min(50, int_or(f('match_tolerance_pct'), 5)))
        window = max(0, min(31, int_or(f('match_window_days'), 3)))
    except (FormError, splits.SplitError, ValueError) as exc:
        return _form(request, db, user, bill, str(exc), dict(form))
    acct = maybe_owned(db, Account, int_or(f('account_id')), user)
    cat = maybe_owned(db, Category, int_or(f('category_id')), user)
    if bill is None:
        bill = Bill(
            owner_id=user.id,
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
    bill.split_mode = mode
    bill.match_tolerance_pct = tolerance
    bill.match_window_days = window
    bill.description_hint = f('description_hint').strip()[:120]
    bill.active = ('active' in form) if bill_id else True
    db.flush()
    existing = {s.user_id: s for s in bill.shares}
    for uid, s in existing.items():
        if uid not in values:
            bill.shares.remove(s)
    for uid, value in values.items():
        share = existing.get(uid)
        if share is None:
            share = BillShare(user_id=uid)
            bill.shares.append(share)
        share.split_value = value
        if uid == user.id:
            share.account_id = acct.id if acct else None
            share.category_id = cat.id if cat else None
    db.flush()
    return redirect(f'/bills/{bill.id}', flash='Bill saved', request=request)


@router.get('/bills/{bill_id}')
def show_bill(
    bill_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    c = ctx(request)
    today = c.clock.today()
    bill, share = visible_bill(db, bill_id, user)
    occ = [
        o
        for o in svc.occurrences(
            db, user, today - timedelta(days=200), today + timedelta(days=200), today
        )
        if o.bill.id == bill.id
    ]
    people = {u.id: u for u in db.execute(select(User)).scalars()}
    return render(
        request,
        'bills/show.html',
        {
            'bill': bill,
            'share': share,
            'occ': occ,
            'amounts': svc.share_amounts(bill),
            'people': people,
            'freq': FREQ_LABELS,
            'is_owner': bill.owner_id == user.id,
            'accounts': ledger.accounts(db, user),
            'cats': ledger.categories(db, user),
        },
    )


@router.post('/bills/{bill_id}/my-share')
async def my_share(
    bill_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    """A participant picks which of their own accounts and categories the share uses."""
    _bill, share = visible_bill(db, bill_id, user)
    form = await request.form()
    a = form.get('account_id')
    k = form.get('category_id')
    acct = maybe_owned(db, Account, int_or(a), user)
    cat = maybe_owned(db, Category, int_or(k), user)
    share.account_id = acct.id if acct else None
    share.category_id = cat.id if cat else None
    return redirect(f'/bills/{bill_id}', flash='Saved', request=request)


def _occ(db: Session, user: User, bill: Bill, due_raw: str) -> date:
    try:
        due = date.fromisoformat(due_raw)
    except ValueError:
        raise HTTPException(status_code=404) from None
    if not svc.is_occurrence(bill, due):
        raise HTTPException(status_code=404)
    return due


@router.get('/bills/{bill_id}/occurrence/{due}')
def occurrence(
    bill_id: int,
    due: str,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    """The calendar's event panel: details and quick actions."""
    c = ctx(request)
    bill, _share = visible_bill(db, bill_id, user)
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
    return render(
        request,
        'bills/_occurrence.html',
        {
            'o': o,
            'txn': txn,
            'candidates': svc.match_candidates(db, user, o) if o.payment is None else [],
            'is_owner': bill.owner_id == user.id,
            'expected': svc.expected_out(bill, user.id),
        },
    )


@router.post('/bills/{bill_id}/occurrence/{due}/paid')
async def mark_paid(
    bill_id: int,
    due: str,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    c = ctx(request)
    bill, _share = visible_bill(db, bill_id, user)
    when = _occ(db, user, bill, due)
    form = await request.form()
    t = form.get('transaction_id')
    txn = get_owned(db, Transaction, int(t), user) if isinstance(t, str) and t.isdigit() else None
    svc.mark_paid(db, c.clock, user, bill, when, txn)
    return redirect(
        local_path(form.get('back'), f'/bills/{bill.id}'),
        flash=f'{bill.name} marked paid',
        request=request,
    )


@router.post('/bills/{bill_id}/occurrence/{due}/unpaid')
async def mark_unpaid(
    bill_id: int,
    due: str,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    bill, _share = visible_bill(db, bill_id, user)
    when = _occ(db, user, bill, due)
    svc.unmark(db, user, bill, when)
    form = await request.form()
    return redirect(
        local_path(form.get('back'), f'/bills/{bill.id}'),
        flash=f'{bill.name} marked unpaid',
        request=request,
    )


@router.post('/bills/{bill_id}/delete')
def delete_bill(
    bill_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    bill = owned_bill(db, bill_id, user)
    db.delete(bill)
    return redirect('/bills', flash='Bill deleted', request=request)
