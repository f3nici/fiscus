"""Categories, rules and the fast recategorise screen."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fiscus.clock import utcnow_naive
from fiscus.domain.rules import RuleError, validate
from fiscus.forms import FormError, text
from fiscus.models import Category, Rule, Transaction, User
from fiscus.services import ledger
from fiscus.services.access import get_owned, owned
from fiscus.web import ctx, current_user, get_db, redirect, render

router = APIRouter()


@router.get('/categories')
def categories(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    cats = ledger.categories(db, user)
    counts: dict[int | None, int] = {
        cid: int(n)
        for cid, n in db.execute(
            select(Transaction.category_id, func.count())
            .where(Transaction.user_id == user.id)
            .group_by(Transaction.category_id)
        )
    }
    rules = db.execute(owned(Rule, user).order_by(Rule.priority.desc(), Rule.id)).scalars().all()
    return render(
        request,
        'categories/index.html',
        {
            'cats': cats,
            'counts': counts,
            'rules': rules,
            'names': {c.id: c.name for c in cats},
            'uncategorised': counts.get(None, 0),
        },
    )


@router.post('/categories')
@router.post('/categories/{category_id}')
def save_category(
    request: Request,
    category_id: int | None = None,
    name: str = Form(...),
    is_income: bool = Form(False),
    is_business: bool = Form(False),
    is_deductible: bool = Form(False),
    is_transfer: bool = Form(False),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    cat = get_owned(db, Category, category_id, user) if category_id else None
    try:
        clean = text(name, 'Name', 80)
    except FormError as exc:
        return redirect('/categories', flash=str(exc), request=request)
    if cat is None:
        cat = Category(user_id=user.id, name=clean)
        db.add(cat)
    cat.name = clean
    cat.is_income = is_income
    cat.is_business = is_business
    cat.is_deductible = is_deductible and not is_income
    cat.is_transfer = is_transfer and not (is_income or is_business)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return redirect(
            '/categories', flash='You already have a category with that name', request=request
        )
    return redirect('/categories', flash='Category saved', request=request)


@router.post('/categories/{category_id}/delete')
def delete_category(
    category_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    cat = get_owned(db, Category, category_id, user)
    try:
        db.delete(cat)
        db.flush()
    except IntegrityError:
        db.rollback()
        return redirect(
            '/categories', flash='That category is used by a housing item', request=request
        )
    return redirect(
        '/categories',
        flash='Category deleted; its transactions are now uncategorised',
        request=request,
    )


@router.post('/rules/rerun')
def rerun(
    request: Request,
    overwrite: bool = Form(False),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    n = ledger.rerun_rules(db, user, overwrite=overwrite)
    return redirect('/categories', flash=f'Rules re-run: {n} transactions changed', request=request)


@router.post('/rules')
@router.post('/rules/{rule_id}')
def save_rule(
    request: Request,
    rule_id: int | None = None,
    pattern: str = Form(...),
    is_regex: bool = Form(False),
    category_id: int = Form(...),
    priority: int = Form(100),
    apply_now: bool = Form(False),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    rule = get_owned(db, Rule, rule_id, user) if rule_id else None
    cat = get_owned(db, Category, category_id, user)
    try:
        clean = validate(pattern, is_regex)
    except RuleError as exc:
        return redirect(
            request.headers.get('referer') or '/categories', flash=str(exc), request=request
        )
    if rule is None:
        rule = Rule(
            user_id=user.id,
            created_at=utcnow_naive(ctx(request).clock),
            pattern=clean,
            category_id=cat.id,
        )
        db.add(rule)
    rule.pattern = clean
    rule.is_regex = is_regex
    rule.category_id = cat.id
    rule.priority = max(0, min(10000, priority))
    db.flush()
    msg = 'Rule saved'
    if apply_now:
        n = ledger.rerun_rules(db, user, overwrite=False)
        msg += f'; categorised {n} transactions'
    return redirect(request.headers.get('referer') or '/categories', flash=msg, request=request)


@router.post('/rules/{rule_id}/delete')
def delete_rule(
    rule_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    db.delete(get_owned(db, Rule, rule_id, user))
    return redirect('/categories', flash='Rule deleted', request=request)


@router.get('/recategorise')
def recategorise(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    txns = (
        db.execute(
            owned(Transaction, user)
            .where(Transaction.category_id.is_(None))
            .order_by(Transaction.date.desc())
            .limit(200)
        )
        .scalars()
        .all()
    )
    accts = {a.id: a.name for a in ledger.accounts(db, user, include_archived=True)}
    return render(
        request,
        'categories/recategorise.html',
        {'txns': txns, 'cats': ledger.categories(db, user), 'accounts': accts},
    )


@router.post('/recategorise/bulk')
async def bulk(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    cat_raw = form.get('category_id')
    cat = get_owned(
        db, Category, int(cat_raw) if isinstance(cat_raw, str) and cat_raw.isdigit() else None, user
    )
    ids = [int(v) for v in form.getlist('txn') if isinstance(v, str) and v.isdigit()]
    n = 0
    for t in db.execute(owned(Transaction, user).where(Transaction.id.in_(ids))).scalars():
        t.category_id = cat.id
        n += 1
    return redirect('/recategorise', flash=f'{n} transactions moved to {cat.name}', request=request)
