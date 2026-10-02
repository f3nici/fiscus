"""Square: connect a seller account, sync it, and see unpaid invoices."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from fiscus.forms import FormError, req_date
from fiscus.models import Account, Category, User
from fiscus.services import audit, ledger
from fiscus.services import square as svc
from fiscus.services.access import get_owned, owned
from fiscus.web import client_ip, ctx, current_user, get_db, redirect, render

router = APIRouter()


@router.get('/square')
def square_page(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    today = ctx(request).clock.today()
    return render(
        request,
        'square.html',
        {
            'conn': svc.connection(db, user),
            'start_default': svc.default_start(today),
            'accounts': ledger.accounts(db, user),
            'cats': db.execute(owned(Category, user).order_by(Category.name)).scalars().all(),
            'invoices': svc.invoices(db, user),
        },
    )


@router.post('/square')
def save(
    request: Request,
    enabled: bool = Form(False),
    token: str = Form(''),
    account_id: int = Form(0),
    income_category_id: int = Form(0),
    fee_category_id: int = Form(0),
    payout_category_id: int = Form(0),
    start_date: str = Form(''),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    c = ctx(request)
    # Someone else's account or category is a 404, as everywhere.
    acct = get_owned(db, Account, account_id, user) if account_id else None
    cats = {
        field: get_owned(db, Category, cid, user).id if cid else None
        for field, cid in (
            ('income_category_id', income_category_id),
            ('fee_category_id', fee_category_id),
            ('payout_category_id', payout_category_id),
        )
    }
    try:
        start = req_date(start_date, 'Start date')
    except FormError as exc:
        return redirect('/square', flash=str(exc), request=request)
    conn = svc.connection_or_new(db, user, c.clock.today())
    token = token.strip()
    if token and (len(token) > 255 or not token.isprintable() or ' ' in token):
        return redirect('/square', flash='That does not look like an access token', request=request)
    if enabled and acct is None:
        return redirect('/square', flash='Choose the account Square goes into', request=request)
    if enabled and not (token or conn.access_token):
        return redirect('/square', flash='Paste your Square access token', request=request)
    if token and token != conn.access_token:
        conn.access_token = token
        audit.record(db, c.clock, 'square.token_set', actor_id=user.id, ip=client_ip(request))
    if acct is not None and conn.account_id not in (None, acct.id):
        conn.synced_until = None  # a new account starts from the start date
    if start != conn.start_date:
        conn.synced_until = None
    conn.enabled = enabled
    conn.account_id = acct.id if acct else None
    conn.start_date = start
    for field, cid in cats.items():
        setattr(conn, field, cid)
    return redirect('/square', flash='Square settings saved', request=request)


@router.post('/square/sync')
def sync_now(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    try:
        summary = svc.sync(db, ctx(request).clock, user)
    except svc.SquareError as exc:
        return redirect('/square', flash=str(exc), request=request)
    return redirect('/square', flash=f'Synced: {summary}', request=request)


@router.post('/square/disconnect')
def disconnect(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    c = ctx(request)
    conn = svc.connection(db, user)
    if conn is not None:
        conn.enabled = False
        conn.access_token = ''
        svc.clear_invoices(db, user)
        audit.record(db, c.clock, 'square.disconnected', actor_id=user.id, ip=client_ip(request))
    return redirect(
        '/square',
        flash='Square disconnected. Transactions already synced stay.',
        request=request,
    )
