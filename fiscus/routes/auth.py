"""Sign in and out."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from fiscus.models import User
from fiscus.security import passwords
from fiscus.services import audit
from fiscus.services import auth as svc
from fiscus.web import (
    RateLimited,
    client_ip,
    ctx,
    get_db,
    optional_user,
    redirect,
    render,
)

router = APIRouter()


def limit(request: Request, bucket: str, key: str) -> None:
    wait = ctx(request).limiter.hit(bucket, key)
    if wait is not None:
        raise RateLimited(wait)


def safe_next(target: str | None) -> str:
    if not target or not target.startswith('/') or target.startswith('//') or '\\' in target:
        return '/'
    return target


def set_session_cookie(request: Request, response: Response, token: str) -> None:
    s = ctx(request).settings
    response.set_cookie(
        s.session_cookie,
        token,
        max_age=s.session_absolute_days * 86400,
        httponly=True,
        secure=s.cookie_secure,
        samesite='lax',
        path='/',
    )


@router.get('/login')
def login_page(
    request: Request, next: str = '/', user: User | None = Depends(optional_user)
) -> Response:
    if user is not None:
        return redirect(safe_next(next))
    return render(
        request,
        'login.html',
        {'next': safe_next(next), 'passkeys_allowed': ctx(request).settings.passkeys_allowed},
    )


@router.post('/auth/passkey/options')
async def passkey_options(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    body: dict[str, Any] = await _json(request)
    limit(request, 'login_ip', client_ip(request))
    username = str(body.get('username') or '').strip().lower() or None
    if username:
        limit(request, 'login_user', username)
    c = ctx(request)
    return JSONResponse(svc.authentication_options(db, c.settings, c.clock, username))


@router.post('/auth/passkey/verify')
async def passkey_verify(request: Request, db: Session = Depends(get_db)) -> Response:
    body = await _json(request)
    ip = client_ip(request)
    limit(request, 'login_ip', ip)
    c = ctx(request)
    try:
        user, _cred = svc.finish_authentication(
            db, c.settings, c.clock, str(body.get('handle', '')), body.get('credential') or {}
        )
    except svc.AuthError as exc:
        audit.record(db, c.clock, 'login.failed', ip=ip, method='passkey', reason=str(exc))
        db.commit()
        return JSONResponse({'error': str(exc)}, status_code=400)
    token = svc.start_session(
        db, c.settings, c.clock, user, 'passkey', ip, request.headers.get('user-agent', '')
    )
    resp = JSONResponse({'ok': True, 'redirect': safe_next(str(body.get('next') or '/'))})
    set_session_cookie(request, resp, token)
    return resp


@router.post('/auth/password')
def password_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form('/'),
    db: Session = Depends(get_db),
) -> Response:
    ip = client_ip(request)
    name = username.strip().lower()
    limit(request, 'login_ip', ip)
    limit(request, 'login_user', name)
    c = ctx(request)
    user = db.execute(select(User).where(User.username == name)).scalar_one_or_none()
    ok = passwords.verify(user.password_hash if user else None, password)
    if not (ok and user and user.is_active and user.password_enabled):
        audit.record(
            db,
            c.clock,
            'login.failed',
            ip=ip,
            method='password',
            subject_id=user.id if user else None,
        )
        return render(
            request,
            'login.html',
            {
                'next': safe_next(next),
                'error': 'That username and password did not work.',
                'show_password': True,
                'passkeys_allowed': c.settings.passkeys_allowed,
            },
            status_code=400,
        )
    token = svc.start_session(
        db, c.settings, c.clock, user, 'password', ip, request.headers.get('user-agent', '')
    )
    resp = redirect(safe_next(next))
    set_session_cookie(request, resp, token)
    return resp


@router.post('/auth/logout')
def logout(request: Request, db: Session = Depends(get_db)) -> Response:
    c = ctx(request)
    found = getattr(request.state, 'auth_session', None)
    if found is None:
        from fiscus.web import load_session

        loaded = load_session(request, db)
        found = loaded[0] if loaded else None
    if found is not None:
        audit.record(db, c.clock, 'logout', actor_id=found.user_id, ip=client_ip(request))
        db.delete(found)
    resp = redirect('/login')
    resp.delete_cookie(c.settings.session_cookie, path='/')
    return resp


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
