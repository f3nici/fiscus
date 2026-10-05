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
from fiscus.domain.budget_import import MORTGAGE
from fiscus.domain.csvimport import MAX_BYTES, ImportError_
from fiscus.forms import FormError, money, text
from fiscus.models import Budget, BudgetGroup, BudgetSpend, Category, Loan, User
from fiscus.money import fmt, round_div
from fiscus.services import budget_groups, budgeting, goals, ledger, loans
from fiscus.services import budget_import as importer
from fiscus.services.access import get_owned, maybe_owned, owned
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
    # Money that can be spent: not the tax set-aside, not a loan offset's job to
    # guess, and not what goals keep aside inside the accounts.
    spend_accts = [a for a in accts if not a.is_set_aside]
    in_goals = goals.held_outside(db, user, spend_accts)
    spendable = sum(bal.get(a.id, 0) for a in spend_accts) - in_goals
    loan_lines = loans.budget_lines(db, user, start, end, today)
    summary = budgeting.summary(
        rows,
        budgeting.expected_pay(db, user, start, end, today),
        loan_cents=sum(x.budget_cents for x in loan_lines),
        loan_paid_cents=sum(x.paid_cents for x in loan_lines),
    )
    cats = ledger.categories(db, user)
    return render(
        request,
        'budget/index.html',
        {
            'rows': rows,
            'sections': budget_groups.sections(db, user, rows),
            'groups': budget_groups.groups(db, user),
            'loan_lines': loan_lines,
            'in_goals': in_goals,
            'goal_for': goals.by_category(db, user),
            'goal_held': goals.held(db, user),
            # A mortgage line of its own would count the loan twice.
            'doubled': {
                r.category.id
                for r in rows
                if loan_lines and r.has_budget and MORTGAGE.search(r.category.name)
            },
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
            'cycles': budgeting.CYCLES,
            'bill_cycles': budgeting.bill_cycles(db, user),
            'spread_pay': budgeting.spread_pay(db, user, start, end, today),
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
    cycle_months: int = Form(1),
    saved: str = Form(''),
    group_id: int = Form(-1),
    back: str = Form('/budget'),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    target = _back(back)
    group = maybe_owned(db, BudgetGroup, group_id, user) if group_id > 0 else None
    if cycle_months not in budgeting.CYCLES:
        return redirect(target, flash='Choose how often it is spent', request=request)
    if category_id:
        cat = get_owned(db, Category, category_id, user)
    elif new_category.strip():
        cat = ledger.category_named(db, user, new_category)
    else:
        return redirect(target, flash='Choose or name a category', request=request)
    if cat.is_income:
        cycle_months = 1
    try:
        cents = money(amount, 'Amount', allow_negative=False)
        saved_cents = money(saved, 'Saved so far') if saved.strip() else None
    except FormError as exc:
        return redirect(target, flash=str(exc), request=request)
    if group_id >= 0:
        budget_groups.put_in(db, user, cat, group)
    today = ctx(request).clock.today()
    total, from_bills = budgeting.set_total(
        db,
        user,
        cat,
        cents,
        today,
        use_in_forecast=False,
        cycle_months=cycle_months,
        saved_cents=saved_cents if cycle_months > 1 else None,
        today=today,
    )
    every = 'a month' if cycle_months == 1 else budgeting.CYCLES[cycle_months].lower()
    if total == 0 and cycle_months == 1:
        msg = f'Budget for {cat.name} removed'
    elif cents < from_bills * cycle_months:
        msg = (
            f'Budget for {cat.name} saved. Your bills in it come to'
            f' {fmt(from_bills * cycle_months)} {every}, more than this'
        )
    elif cycle_months > 1:
        msg = f'Saving up {fmt(total)} a month for {cat.name}, spent {every}'
    else:
        msg = f'Budget for {cat.name} saved'
    return redirect(target, flash=msg, request=request)


def _step(direction: str) -> int:
    return -1 if direction == 'up' else 1


@router.post('/budget/groups')
def add_group(
    request: Request,
    name: str = Form(...),
    back: str = Form('/budget'),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    try:
        clean = text(name, 'Group name', 80)
    except FormError as exc:
        return redirect(_back(back), flash=str(exc), request=request)
    budget_groups.add_group(db, user, clean)
    return redirect(_back(back), flash=f'Group {clean} added', request=request)


@router.post('/budget/groups/{group_id}')
def rename_group(
    group_id: int,
    request: Request,
    name: str = Form(...),
    back: str = Form('/budget'),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    group = get_owned(db, BudgetGroup, group_id, user)
    try:
        group.name = text(name, 'Group name', 80)
    except FormError as exc:
        return redirect(_back(back), flash=str(exc), request=request)
    db.flush()
    return redirect(_back(back), flash=f'Group renamed to {group.name}', request=request)


@router.post('/budget/groups/{group_id}/move')
def move_group(
    group_id: int,
    request: Request,
    direction: str = Form('down'),
    back: str = Form('/budget'),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    group = get_owned(db, BudgetGroup, group_id, user)
    budget_groups.move_group(db, user, group, _step(direction))
    return redirect(f'{_back(back)}#group-{group.id}', request=request)


@router.post('/budget/groups/{group_id}/delete')
def delete_group(
    group_id: int,
    request: Request,
    back: str = Form('/budget'),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    group = get_owned(db, BudgetGroup, group_id, user)
    budget_groups.delete_group(db, user, group)
    return redirect(
        _back(back), flash=f'Group {group.name} removed; its lines are kept', request=request
    )


@router.post('/budget/{category_id}/move')
def move_line(
    category_id: int,
    request: Request,
    direction: str = Form('down'),
    back: str = Form('/budget'),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    cat = get_owned(db, Category, category_id, user)
    budget_groups.move_line(db, user, cat, _step(direction))
    return redirect(f'{_back(back)}#line-{cat.id}', request=request)


@router.post('/budget/{category_id}/remove')
def remove_budget(
    category_id: int,
    request: Request,
    back: str = Form('/budget'),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    cat = get_owned(db, Category, category_id, user)
    row = db.execute(owned(Budget, user).where(Budget.category_id == cat.id)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404)
    db.delete(row)
    db.flush()
    return redirect(_back(back), flash=f'{cat.name} removed from the budget', request=request)


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
    goals.remove_spend(db, user, row)
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
            'cycles': budgeting.CYCLES,
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
            cents = money(f(f'amount_{i}'), f'{label}: amount', allow_negative=False)
            cycle = int(f(f'cycle_{i}') or 1)
            if cycle not in budgeting.CYCLES:
                raise FormError(f'{label}: choose how often it is spent')
            if cents:
                chosen.append(importer.Chosen(category, round_div(cents, cycle), cycle))
    except (FormError, ValueError) as exc:
        return redirect(f'/budget/import/{name}', flash=str(exc), request=request)
    if not chosen:
        return redirect(f'/budget/import/{name}', flash='Tick at least one line', request=request)
    mode = f('mode')
    cleared = importer.clear(db, user) if mode == 'clear' else 0
    n, total = importer.apply(
        db,
        user,
        chosen,
        ctx(request).clock.today(),
        replace=mode != 'add',
        use_in_forecast=f('use_in_forecast') == 'on',
    )
    (ctx(request).settings.upload_dir / name).unlink(missing_ok=True)
    msg = f'Imported {len(chosen)} lines into {n} budget categories, {fmt(total)} a month'
    if mode == 'clear':
        msg = f'Old budget cleared ({cleared} lines). {msg}'
    return redirect('/budget', flash=msg, request=request)
