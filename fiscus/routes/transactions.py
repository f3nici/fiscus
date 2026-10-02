"""Transactions: browse, recategorise, add by hand, export."""

from __future__ import annotations

import csv
import io
import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session

from fiscus.clock import utcnow_naive
from fiscus.domain.csvimport import csv_safe, row_hash
from fiscus.forms import FormError, money, req_date, text
from fiscus.models import Account, BillPayment, Category, Transaction, User
from fiscus.money import fmt_plain
from fiscus.services import audit, bills, ledger
from fiscus.services.access import get_owned, maybe_owned, owned
from fiscus.web import client_ip, ctx, current_user, get_db, redirect, render

router = APIRouter()
PAGE = 100


@router.get('/transactions')
def list_txns(
    request: Request,
    q: str = '',
    account_id: int = 0,
    category_id: str = '',
    page: int = 1,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    page = max(1, page)
    query = owned(Transaction, user)
    if q.strip():
        query = query.where(Transaction.description.ilike(f'%{q.strip()}%'))
    if account_id:
        query = query.where(Transaction.account_id == account_id)
    if category_id == 'none':
        query = query.where(Transaction.category_id.is_(None))
    elif category_id.isdigit():
        query = query.where(Transaction.category_id == int(category_id))
    txns = (
        db.execute(
            query.order_by(Transaction.date.desc(), Transaction.id.desc())
            .offset((page - 1) * PAGE)
            .limit(PAGE + 1)
        )
        .scalars()
        .all()
    )
    return render(
        request,
        'transactions/list.html',
        {
            'txns': txns[:PAGE],
            'more': len(txns) > PAGE,
            'page': page,
            'q': q,
            'account_id': account_id,
            'category_id': category_id,
            'accounts': {a.id: a for a in ledger.accounts(db, user, include_archived=True)},
            'cats': ledger.categories(db, user),
        },
    )


@router.post('/transactions/{txn_id}/category')
def set_category(
    txn_id: int,
    request: Request,
    category_id: int = Form(0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    txn = get_owned(db, Transaction, txn_id, user)
    cat = maybe_owned(db, Category, category_id, user)
    txn.category_id = cat.id if cat else None
    if request.headers.get('hx-request') == 'true':
        return render(request, 'transactions/_category_saved.html', {'txn': txn, 'cat': cat})
    return redirect(request.headers.get('referer') or '/transactions', request=request)


@router.post('/transactions')
def add_manual(
    request: Request,
    account_id: int = Form(...),
    date: str = Form(...),
    amount: str = Form(...),
    description: str = Form(...),
    category_id: int = Form(0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    c = ctx(request)
    acct = get_owned(db, Account, account_id, user)
    try:
        when = req_date(date, 'Date')
        cents = money(amount, 'Amount')
        desc = text(description, 'Description', 500)
    except FormError as exc:
        return redirect(f'/accounts/{acct.id}', flash=str(exc), request=request)
    cat = maybe_owned(db, Category, category_id, user)
    txn = Transaction(
        user_id=user.id,
        account_id=acct.id,
        date=when,
        amount_cents=cents,
        description=desc,
        category_id=cat.id if cat else None,
        # Manual rows never collide with an import's hash.
        dedup_hash=row_hash(acct.id, when, cents, f'manual:{uuid.uuid4().hex}:{desc}', 0),
        created_at=utcnow_naive(c.clock),
    )
    db.add(txn)
    db.flush()
    if cat is None:
        ledger.apply_rules(db, user, [txn])
    bills.auto_match(db, c.clock, user, [txn])
    return redirect(f'/accounts/{acct.id}', flash='Transaction added', request=request)


@router.post('/transactions/{txn_id}/delete')
def delete_manual(
    txn_id: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    txn = get_owned(db, Transaction, txn_id, user)
    if txn.import_id is not None:
        return redirect(
            f'/accounts/{txn.account_id}',
            flash='Imported transactions are removed by undoing their import',
            request=request,
        )
    for pay in db.execute(
        owned(BillPayment, user).where(BillPayment.transaction_id == txn.id)
    ).scalars():
        db.delete(pay)
    db.delete(txn)
    return redirect(f'/accounts/{txn.account_id}', flash='Transaction deleted', request=request)


@router.get('/transactions/export.csv')
def export_csv(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    c = ctx(request)
    accts = {a.id: a.name for a in ledger.accounts(db, user, include_archived=True)}
    cats = {cat.id: cat.name for cat in ledger.categories(db, user)}
    rows = (
        db.execute(owned(Transaction, user).order_by(Transaction.date, Transaction.id))
        .scalars()
        .all()
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['date', 'account', 'amount', 'description', 'category'])
    for t in rows:
        w.writerow(
            [
                t.date.isoformat(),
                csv_safe(accts.get(t.account_id, '')),
                fmt_plain(t.amount_cents),
                csv_safe(t.description),
                csv_safe(cats.get(t.category_id, '') if t.category_id else ''),
            ]
        )
    audit.record(
        db, c.clock, 'export.transactions', actor_id=user.id, ip=client_ip(request), rows=len(rows)
    )
    name = f'fiscus-transactions-{c.clock.today().isoformat()}.csv'
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{name}"'},
    )
