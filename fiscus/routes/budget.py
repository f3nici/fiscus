"""Monthly budget against actual."""

from __future__ import annotations

import re
import uuid
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.orm import Session

from fiscus.domain import recurrence
from fiscus.domain.csvimport import MAX_BYTES, ImportError_
from fiscus.forms import FormError, money
from fiscus.models import Category, Loan, User
from fiscus.money import fmt
from fiscus.services import budget_import as importer
from fiscus.services import budgeting, ledger
from fiscus.services.access import get_owned, owned
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
        focus = date.fromisoformat(month + '-01') if month else today.replace(day=1)
    except ValueError:
        focus = today.replace(day=1)
    rows = budgeting.rows(db, user, focus, today)
    budgeted = {r.category.id for r in rows if r.has_budget}
    return render(
        request,
        'budget/index.html',
        {
            'rows': rows,
            'totals': budgeting.totals(rows),
            'month': focus,
            'prev': recurrence.add_months(focus, -1),
            'next': recurrence.add_months(focus, 1),
            'unbudgeted': [
                cat
                for cat in ledger.categories(db, user)
                if cat.id not in budgeted and not cat.is_transfer
            ],
        },
    )


@router.post('/budget')
def save_budget(
    request: Request,
    category_id: int = Form(...),
    amount: str = Form(...),
    use_in_forecast: bool = Form(False),
    back: str = Form('/budget'),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    cat = get_owned(db, Category, category_id, user)
    target = back if back.startswith('/budget') else '/budget'
    try:
        cents = money(amount, 'Amount', allow_negative=False)
    except FormError as exc:
        return redirect(target, flash=str(exc), request=request)
    total, from_bills = budgeting.set_total(
        db, user, cat, cents, ctx(request).clock.today(), use_in_forecast=use_in_forecast
    )
    if from_bills and cents < from_bills:
        msg = f'Your bills in {cat.name} come to {fmt(from_bills)} a month, so that is its budget'
    elif total == 0:
        msg = f'Budget for {cat.name} removed'
    else:
        msg = f'Budget for {cat.name} saved'
    return redirect(target, flash=msg, request=request)


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
    budgets = {r.category.id: r.budget_cents for r in budgeting.rows(db, user, today, today)}
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
