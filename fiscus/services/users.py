"""Users and their starting data."""

from __future__ import annotations

import re
import secrets

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fiscus.clock import Clock, utcnow_naive
from fiscus.models import Category, User, UserSettings, WebAuthnCredential

USERNAME = re.compile(r'^[a-z0-9][a-z0-9._-]{1,31}$')

DEFAULT_CATEGORIES: list[tuple[str, dict[str, bool]]] = [
    ('Salary', {'is_income': True}),
    ('Business income', {'is_income': True, 'is_business': True}),
    ('Business expenses', {'is_business': True, 'is_deductible': True}),
    ('Groceries', {}),
    ('Dining out', {}),
    ('Utilities', {}),
    ('Housing', {}),
    ('Insurance', {}),
    ('Transport', {}),
    ('Health', {}),
    ('Entertainment', {}),
    ('Shopping', {}),
    ('Subscriptions', {}),
    ('Transfers', {'is_transfer': True}),
    ('Loan repayments', {'is_transfer': True}),
    ('Other', {}),
]


class UserError(ValueError):
    pass


def normalise_username(raw: str) -> str:
    name = raw.strip().lower()
    if not USERNAME.match(name):
        raise UserError(
            'Usernames are 2 to 32 characters: lowercase letters, numbers, dot, dash or underscore'
        )
    return name


def create_user(
    db: Session,
    clock: Clock,
    username: str,
    display_name: str,
    *,
    is_admin: bool = False,
    user_handle: bytes | None = None,
) -> User:
    name = normalise_username(username)
    if db.execute(select(User).where(User.username == name)).scalar_one_or_none():
        raise UserError('That username is taken')
    user = User(
        username=name,
        display_name=(display_name.strip() or name)[:120],
        is_admin=is_admin,
        is_active=True,
        user_handle=user_handle or secrets.token_bytes(32),
        created_at=utcnow_naive(clock),
        data_version=0,
    )
    db.add(user)
    db.flush()
    db.add(UserSettings(user_id=user.id))
    for cat_name, flags in DEFAULT_CATEGORIES:
        db.add(Category(user_id=user.id, name=cat_name, **flags))
    db.flush()
    return user


def passkey_count(db: Session, user: User) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(WebAuthnCredential)
            .where(WebAuthnCredential.user_id == user.id)
        )
        or 0
    )


def settings_for(db: Session, user: User) -> UserSettings:
    s = db.get(UserSettings, user.id)
    if s is None:
        s = UserSettings(user_id=user.id)
        db.add(s)
        db.flush()
    return s
