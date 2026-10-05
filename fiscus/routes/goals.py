"""Savings goals: money set aside inside an account for something."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from fiscus.clock import utcnow_naive
from fiscus.forms import FormError, money, opt_date, text
from fiscus.models import Account, Goal, GoalMove, User
from fiscus.money import fmt
from fiscus.services import budgeting, ledger
from fiscus.services import goals as service
from fiscus.services.access import get_owned, maybe_owned
from fiscus.web import ctx, current_user, get_db, redirect, render

router = APIRouter()


def _back(back: str, goal: Goal | None = None) -> str:
    for ok in ('/goals', '/budget', '/accounts'):
        if back.startswith(ok):
            return back
    return f'/goals/{goal.id}' if goal else '/goals'


@router.get('/goals')
def list_goals(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    today = ctx(request).clock.today()
    accts = ledger.accounts(db, user)
    return render(
        request,
        'goals/index.html',
        {
            'views': service.views(db, user, today, include_archived=True),
            'accounts': accts,
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
    db: Session = Depends(get_db),
) -> Response:
    goal = get_owned(db, Goal, goal_id, user)
    today = ctx(request).clock.today()
    view = next(v for v in service.views(db, user, today, include_archived=True) if v.goal is goal)
    return render(
        request,
        'goals/show.html',
        {
            'v': view,
            'goal': goal,
            'moves': sorted(goal.moves, key=lambda m: (m.date, m.id), reverse=True),
            'accounts': ledger.accounts(db, user),
            'account': db.get(Account, goal.account_id) if goal.account_id else None,
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


@router.post('/goals')
def create_goal(
    request: Request,
    name: str = Form(...),
    target: str = Form(''),
    target_date: str = Form(''),
    account_id: int = Form(0),
    saved: str = Form(''),
    keep_aside: bool = Form(False),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    c = ctx(request)
    try:
        clean, cents, when, acct = _fields(db, user, name, target, target_date, account_id)
        saved_cents = money(saved, 'Already saved') if saved.strip() else 0
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
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    goal = get_owned(db, Goal, goal_id, user)
    c = ctx(request)
    try:
        clean, cents, when, acct = _fields(db, user, name, target, target_date, account_id)
        holds_cents = money(holds, 'Holds now') if holds.strip() else None
    except FormError as exc:
        return redirect(f'/goals/{goal.id}', flash=str(exc), request=request)
    goal.name = clean
    goal.target_cents = cents
    goal.target_date = when
    goal.account_id = acct.id if acct else None
    goal.keep_aside = keep_aside
    db.flush()
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
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    goal = get_owned(db, Goal, goal_id, user)
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
        cents = -cents
    service.move(db, c.clock, user, goal, cents, when, note)
    verb = 'into' if cents > 0 else 'out of'
    return redirect(target, flash=f'{fmt(abs(cents))} {verb} {goal.name}', request=request)


@router.post('/goals/{goal_id}/moves/{move_id}/delete')
def delete_move(
    goal_id: int,
    move_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    goal = get_owned(db, Goal, goal_id, user)
    row = get_owned(db, GoalMove, move_id, user)
    if row.goal_id != goal.id:
        raise HTTPException(status_code=404)
    db.delete(row)
    return redirect(f'/goals/{goal.id}', flash='Entry removed', request=request)


@router.post('/goals/{goal_id}/done')
def toggle_done(
    goal_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
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
    db: Session = Depends(get_db),
) -> Response:
    goal = get_owned(db, Goal, goal_id, user)
    db.delete(goal)
    return redirect('/goals', flash=f'Deleted {goal.name}', request=request)
