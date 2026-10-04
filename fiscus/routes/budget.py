"""The budget: set aside per category, take spending off by hand, see what is left."""

from __future__ import annotations

import re
import uuid
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.orm import Session

from fiscus.clock import utcnow_naive
from fiscus.domain import recurrence
from fiscus.domain.csvimport import MAX_BYTES, ImportError_
from fiscus.forms import FormError, money
from fiscus.models import BudgetSpend, Category, Loan, User
from fiscus.money import fmt
from fiscus.services import budget_import as importer
from fiscus.services import budgeting, ledger
from fiscus.services.access import get_owned, owned
from fiscus.services.users import settings_for
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
        start, end = budgeting.period_starting_in(db, user, date.fromisoformat(month + '-01'))
    except ValueError:
        start, end = budgeting.period(db, user, today)
    rows = budgeting.rows(db, user, start, end, today)
    budgeted = {r.category.id for r in rows if r.has_budget}
    accts = ledger.accounts(db, user)
    bal = ledger.balances(db, user, today)
    # Money that can be spent: not the tax set-aside, not a loan offset's job to guess.
    spendable = sum(bal.get(a.id, 0) for a in accts if not a.is_set_aside)
    summary = budgeting.summary(rows, budgeting.expected_pay(db, user, start, end, today))
    cats = ledger.categories(db, user)
    return render(
        request,
        'budget/index.html',
        {
            'rows': rows,
            'summary': summary,
            'start': start,
            'end': end,
            'current': start <= today <= end,
            'prev': recurrence.add_months(start, -1),
            'next': recurrence.add_months(start, 1),
            'start_day': budgeting.start_day(db, user),
            'spends': budgeting.spends(db, user, start, end),
            'cat_names': {cat.id: cat.name for cat in cats},
            'spend_cats': [r.category for r in rows if r.has_budget and not r.category.is_income],
            'spendable': spendable,
            'has_accounts': bool(accts),
            'unbudgeted': [cat for cat in cats if cat.id not in budgeted and not cat.is_transfer],
        },
    )


def _back(back: str) -> str:
    return back if back.startswith('/budget') else '/budget'


@router.post('/budget')
def save_budget(
    request: Request,
    category_id: int = Form(0),
    new_category: str = Form(''),
    amount: str = Form(...),
    back: str = Form('/budget'),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    target = _back(back)
    if category_id:
        cat = get_owned(db, Category, category_id, user)
    elif new_category.strip():
        cat = ledger.category_named(db, user, new_category)
    else:
        return redirect(target, flash='Choose or name a category', request=request)
    try:
        cents = money(amount, 'Amount', allow_negative=False)
    except FormError as exc:
        return redirect(target, flash=str(exc), request=request)
    total, from_bills = budgeting.set_total(
        db, user, cat, cents, ctx(request).clock.today(), use_in_forecast=False
    )
    if from_bills and cents < from_bills:
        msg = f'Your bills in {cat.name} come to {fmt(from_bills)} a month, so that is its budget'
    elif total == 0:
        msg = f'Budget for {cat.name} removed'
    else:
        msg = f'Budget for {cat.name} saved'
    return redirect(target, flash=msg, request=request)


@router.post('/budget/spend')
def add_spend(
    request: Request,
    category_id: int = Form(...),
    amount: str = Form(...),
    note: str = Form(''),
    on: str = Form(''),
    back: str = Form('/budget'),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    cat = get_owned(db, Category, category_id, user)
    target = _back(back)
    c = ctx(request)
    try:
        cents = money(amount, 'Amount')
        if cents == 0:
            raise FormError('Enter an amount')
        when = date.fromisoformat(on) if on else c.clock.today()
    except FormError as exc:
        return redirect(target, flash=str(exc), request=request)
    except ValueError:
        return redirect(target, flash='That date is not valid', request=request)
    db.add(
        BudgetSpend(
            user_id=user.id,
            category_id=cat.id,
            date=when,
            amount_cents=cents,
            note=note.strip()[:120],
            created_at=utcnow_naive(c.clock),
        )
    )
    db.flush()
    verb = 'earned in' if cat.is_income else 'off'
    return redirect(target, flash=f'{fmt(abs(cents))} {verb} {cat.name}', request=request)


@router.post('/budget/spend/{spend_id}/delete')
def delete_spend(
    spend_id: int,
    request: Request,
    back: str = Form('/budget'),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    row = get_owned(db, BudgetSpend, spend_id, user)
    db.delete(row)
    return redirect(_back(back), flash='Entry removed', request=request)


@router.post('/budget/start-day')
def save_start_day(
    request: Request,
    start_day: int = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    if not 1 <= start_day <= 31:
        return redirect('/budget', flash='Pick a day from 1 to 31', request=request)
    settings_for(db, user).budget_start_day = start_day
    db.flush()
    return redirect(
        '/budget', flash=f'Your budget month now starts on day {start_day}', request=request
    )


# ------------------------------------------------------- spreadsheet import

STAGED = re.compile(r'^budget-(\d+)-([0-9a-f]{32})\.(xlsx|csv)$')


@router.get('/budget/template.csv')
def budget_template() -> Response:
    return PlainTextResponse(
        importer.template_csv(),
        media_type='text/csv',
        headers={'Content-Disposition': 'attachment; filename="fiscus-budget-template.csv"'},
    )


@router.post('/budget/import')
async def budget_upload(
    request: Request, file: UploadFile = File(...), user: User = Depends(current_user)
) -> Response:
    c = ctx(request)
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        return redirect('/budget', flash='That file is larger than 10 MB', request=request)
    kind = 'xlsx' if data[:4] == b'PK\x03\x04' else 'csv'
    name = f'budget-{user.id}-{uuid.uuid4().hex}.{kind}'
    (c.settings.upload_dir / name).write_bytes(data)
    return redirect(f'/budget/import/{name}', request=request)


def _staged(request: Request, user: User, name: str) -> bytes:
    m = STAGED.match(name)
    if m is None or int(m.group(1)) != user.id:
        raise HTTPException(status_code=404)
    path = ctx(request).settings.upload_dir / name
    if not path.exists():
        raise HTTPException(status_code=404)
    return path.read_bytes()


def _has_loan(db: Session, user: User) -> bool:
    return db.execute(owned(Loan, user).limit(1)).first() is not None


@router.get('/budget/import/{name}')
def budget_preview(
    name: str,
    request: Request,
    sheet: str = '',
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    data = _staged(request, user, name)
    try:
        parsed = importer.parse(data, name, sheet or None, has_loan=_has_loan(db, user))
    except ImportError_ as exc:
        (ctx(request).settings.upload_dir / name).unlink(missing_ok=True)
        return redirect('/budget', flash=str(exc), request=request)
    cats = ledger.categories(db, user)
    by_name = {c.name.casefold(): c for c in cats}
    today = ctx(request).clock.today()
    start, end = budgeting.period(db, user, today)
    budgets = {r.category.id: r.budget_cents for r in budgeting.rows(db, user, start, end, today)}
    current = {
        line.index: budgets.get(by_name[line.name.casefold()].id)
        for line in parsed.lines
        if line.name.casefold() in by_name
    }
    return render(
        request,
        'budget/import.html',
        {
            'name': name,
            'parsed': parsed,
            'cats': cats,
            'current': current,
            'included': sum(line.monthly_cents or 0 for line in parsed.lines if line.include),
        },
    )


@router.post('/budget/import/{name}/apply')
async def budget_apply(
    name: str,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    _staged(request, user, name)
    form = await request.form()

    def f(key: str) -> str:
        v = form.get(key)
        return v.strip() if isinstance(v, str) else ''

    chosen: list[importer.Chosen] = []
    try:
        count = int(f('count') or 0)
        for i in range(min(count, 2000)):
            if f(f'include_{i}') != 'on':
                continue
            label = f(f'name_{i}') or 'Line'
            category = f(f'category_{i}')[:80] or label
            cents = money(f(f'monthly_{i}'), f'{label}: monthly amount', allow_negative=False)
            if cents:
                chosen.append(importer.Chosen(category, cents))
    except (FormError, ValueError) as exc:
        return redirect(f'/budget/import/{name}', flash=str(exc), request=request)
    if not chosen:
        return redirect(f'/budget/import/{name}', flash='Tick at least one line', request=request)
    n, total = importer.apply(
        db,
        user,
        chosen,
        ctx(request).clock.today(),
        replace=f('mode') != 'add',
        use_in_forecast=f('use_in_forecast') == 'on',
    )
    (ctx(request).settings.upload_dir / name).unlink(missing_ok=True)
    return redirect(
        '/budget',
        flash=f'Imported {len(chosen)} lines into {n} budget categories, {fmt(total)} a month',
        request=request,
    )
