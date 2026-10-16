"""Categories: the names budget lines and bills are filed under, and their flags."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fiscus.forms import FormError, text
from fiscus.models import Budget, Category, User
from fiscus.services import ledger
from fiscus.services.access import get_owned, owned
from fiscus.web import DB, current_user, redirect, render

router = APIRouter()


@router.get('/categories')
def categories(request: Request, user: User = Depends(current_user), db: Session = DB) -> Response:
    budgeted = {b.category_id for b in db.execute(owned(Budget, user)).scalars()}
    return render(
        request,
        'categories/index.html',
        {'cats': ledger.categories(db, user), 'budgeted': budgeted},
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
    db: Session = DB,
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
    db: Session = DB,
) -> Response:
    cat = get_owned(db, Category, category_id, user)
    db.delete(cat)
    db.flush()
    return redirect('/categories', flash=f'{cat.name} deleted', request=request)
