"""Savings goals: money set aside inside an account for something."""

from __future__ import annotations

import contextlib
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from fiscus.clock import utcnow_naive
from fiscus.domain.tax import TaxConfigError
from fiscus.forms import FormError, money, opt_date, text
from fiscus.models import Account, Category, Goal, GoalMove, User
from fiscus.money import fmt
from fiscus.services import budgeting, ledger, taxes
from fiscus.services import goals as service
from fiscus.services.access import get_owned, maybe_owned
from fiscus.web import DB, ctx, current_user, redirect, render

router = APIRouter()


def _lines(db: Session, user: User) -> list[Category]:
    """Categories a goal can be saved from: spending ones."""
    return [c for c in ledger.categories(db, user) if not c.is_income and not c.is_transfer]


def _back(back: str, goal: Goal | None = None) -> str:
    for ok in ('/goals', '/budget', '/accounts'):
        if back.startswith(ok):
            return back
    return f'/goals/{goal.id}' if goal else '/goals'


@router.get('/goals')
def list_goals(request: Request, user: User = Depends(current_user), db: Session = DB) -> Response:
    today = ctx(request).clock.today()
    accts = ledger.accounts(db, user)
    return render(
        request,
        'goals/index.html',
        {
            'views': service.views(db, user, today, include_archived=True),
            'accounts': accts,
            'lines': _lines(db, user),
            'saves_into': _saves_into(db, user),
            'account_names': {
                a.id: a.name for a in ledger.accounts(db, user, include_archived=True)
            },
        },
    )


@router.get('/goals/{goal_id}')
def show_goal(
    goal_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    goal = get_owned(db, Goal, goal_id, user)
    c = ctx(request)
    today = c.clock.today()
    view = next(v for v in service.views(db, user, today, include_archived=True) if v.goal is goal)
    tax_view = None
    if goal.is_tax and not goal.archived:
        with contextlib.suppress(TaxConfigError):
            tax_view = taxes.view(db, user, today, c.tax_dir)
    return render(
        request,
        'goals/show.html',
        {
            'v': view,
            'goal': goal,
            'moves': sorted(goal.moves, key=lambda m: (m.date, m.id), reverse=True),
            'accounts': ledger.accounts(db, user),
            'account': db.get(Account, goal.account_id) if goal.account_id else None,
            'lines': _lines(db, user),
            'saves_into': _saves_into(db, user),
            'goal_lines': service.lines(db, user, goal),
            'tax': tax_view,
        },
    )


def _fields(
    db: Session, user: User, name: str, target: str, target_date: str, account_id: int
) -> tuple[str, int | None, date | None, Account | None]:
    clean = text(name, 'Name', 80)
    cents = money(target, 'Target', allow_negative=False) if target.strip() else None
    when = opt_date(target_date, 'Save by')
    if when is not None and cents is None:
        raise FormError('Give a target amount to save by a date')
    return clean, cents or None, when, maybe_owned(db, Account, account_id, user)


def _saves_into(db: Session, user: User) -> dict[int, str]:
    """The open goal each budget line saves into, by name."""
    return {cid: g.name for cid, g in service.by_category(db, user).items()}


def _chosen(db: Session, user: User, category_ids: list[int]) -> list[Category]:
    """The budget lines ticked to save from. Someone else's are ignored."""
    out = []
    for cid in dict.fromkeys(category_ids):
        cat = maybe_owned(db, Category, cid, user)
        if cat is None:
            continue
        if cat.is_income or cat.is_transfer:
            raise FormError('Choose a spending category to save from')
        out.append(cat)
    return out


@router.post('/goals')
def create_goal(
    request: Request,
    name: str = Form(...),
    target: str = Form(''),
    target_date: str = Form(''),
    account_id: int = Form(0),
    saved: str = Form(''),
    keep_aside: bool = Form(False),
    is_tax: bool = Form(False),
    category_id: Annotated[list[int], Form()] = [],  # noqa: B006
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    c = ctx(request)
    try:
        clean, cents, when, acct = _fields(db, user, name, target, target_date, account_id)
        chosen = _chosen(db, user, category_id)
        saved_cents = money(saved, 'Already saved', allow_negative=False) if saved.strip() else 0
    except FormError as exc:
        return redirect('/goals', flash=str(exc), request=request)
    goal = Goal(
        user_id=user.id,
        name=clean,
        account_id=acct.id if acct else None,
        target_cents=cents,
        target_date=when,
        keep_aside=keep_aside,
        created_at=utcnow_naive(c.clock),
    )
    db.add(goal)
    db.flush()
    service.set_tax(db, user, goal, is_tax)
    service.set_lines(db, user, goal, chosen)
    if saved_cents:
        # An opening amount, not this budget month's saving: dated before it.
        start, _end = budgeting.period(db, user, c.clock.today())
        service.move(
            db, c.clock, user, goal, saved_cents, start - timedelta(days=1), 'Already saved'
        )
    return redirect(f'/goals/{goal.id}', flash=f'{clean} added', request=request)


@router.post('/goals/{goal_id}')
def save_goal(
    goal_id: int,
    request: Request,
    name: str = Form(...),
    target: str = Form(''),
    target_date: str = Form(''),
    account_id: int = Form(0),
    holds: str = Form(''),
    keep_aside: bool = Form(False),
    is_tax: bool = Form(False),
    category_id: Annotated[list[int], Form()] = [],  # noqa: B006
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    goal = get_owned(db, Goal, goal_id, user)
    c = ctx(request)
    try:
        clean, cents, when, acct = _fields(db, user, name, target, target_date, account_id)
        chosen = _chosen(db, user, category_id)
        holds_cents = money(holds, 'Holds now', allow_negative=False) if holds.strip() else None
    except FormError as exc:
        return redirect(f'/goals/{goal.id}', flash=str(exc), request=request)
    goal.name = clean
    goal.target_cents = cents
    goal.target_date = when
    goal.account_id = acct.id if acct else None
    goal.keep_aside = keep_aside
    service.set_tax(db, user, goal, is_tax)
    service.set_lines(db, user, goal, chosen)
    if holds_cents is not None:
        service.set_held(db, c.clock, user, goal, holds_cents, c.clock.today())
    return redirect(f'/goals/{goal.id}', flash=f'{clean} saved', request=request)


@router.post('/goals/{goal_id}/move')
def add_move(
    goal_id: int,
    request: Request,
    amount: str = Form(...),
    direction: str = Form('in'),
    note: str = Form(''),
    on: str = Form(''),
    back: str = Form(''),
    as_spent: bool = Form(False),
    category_id: int = Form(0),
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    goal = get_owned(db, Goal, goal_id, user)
    # The line it is spent from: the one named, else the goal's only line.
    saved_from = service.lines(db, user, goal)
    line = next((c for c in saved_from if c.id == category_id), None)
    if line is None and not category_id and len(saved_from) == 1:
        line = saved_from[0]
    c = ctx(request)
    target = _back(back, goal)
    try:
        cents = money(amount, 'Amount', allow_negative=False)
        if cents == 0:
            raise FormError('Enter an amount')
        when = opt_date(on, 'Date') or c.clock.today()
    except FormError as exc:
        return redirect(target, flash=str(exc), request=request)
    if direction == 'out':
        held = service.held(db, user).get(goal.id, 0)
        if cents > held:
            return redirect(
                target,
                flash=f'{goal.name} holds {fmt(max(0, held))}, so that is the most to take out',
                request=request,
            )
        today = c.clock.today()
        before = ledger.balances(db, user, today)
        row = service.take_out(db, c.clock, user, goal, cents, when, note)
        msg = f'{fmt(cents)} out of {goal.name}{ledger.now_at(db, user, before, today)}'
        has = ledger.held(db, user, row, today)
        if has is not None:
            msg += (
                f'. The balance typed in for {has[0]} on {has[1].strftime("%d %b")} already has it'
            )
    elif as_spent and line is not None:
        service.put_aside(db, c.clock, user, goal, line, cents, when, note)
        msg = f'{fmt(cents)} put aside into {goal.name}, spent from {line.name}'
    else:
        service.move(db, c.clock, user, goal, cents, when, note)
        msg = f'{fmt(cents)} into {goal.name}'
    return redirect(target, flash=msg, request=request)


@router.post('/goals/{goal_id}/moves/{move_id}/delete')
def delete_move(
    goal_id: int,
    move_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    goal = get_owned(db, Goal, goal_id, user)
    row = get_owned(db, GoalMove, move_id, user)
    if row.goal_id != goal.id:
        raise HTTPException(status_code=404)
    service.remove_move(db, user, row)
    return redirect(f'/goals/{goal.id}', flash='Entry removed', request=request)


@router.post('/goals/{goal_id}/done')
def toggle_done(
    goal_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    goal = get_owned(db, Goal, goal_id, user)
    goal.archived = not goal.archived
    db.flush()
    msg = (
        f'{goal.name} is done. What it held is free again'
        if goal.archived
        else f'{goal.name} reopened'
    )
    return redirect(f'/goals/{goal.id}', flash=msg, request=request)


@router.post('/goals/{goal_id}/delete')
def delete_goal(
    goal_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    goal = get_owned(db, Goal, goal_id, user)
    service.delete_goal(db, user, goal)
    return redirect('/goals', flash=f'Deleted {goal.name}', request=request)
