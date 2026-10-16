"""One-time setup links: bootstrap, onboarding and recovery all land here."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from fiscus.models import User
from fiscus.routes.auth import _json, limit, set_session_cookie
from fiscus.services import auth as svc
from fiscus.services.users import UserError, normalise_username
from fiscus.web import DB, client_ip, ctx, render

router = APIRouter()


@router.get('/setup/{token}')
def setup_page(token: str, request: Request, db: Session = DB) -> Response:
    limit(request, 'setup_ip', client_ip(request))
    c = ctx(request)
    link = svc.find_link(db, c.clock, token)
    if link is None:
        return render(
            request,
            'error.html',
            {
                'title': 'Link expired',
                'message': 'This setup link has expired or was already used. '
                'Ask your Fiscus admin for a new one.',
            },
            status_code=410,
        )
    user = db.get(User, link.user_id) if link.user_id else None
    return render(
        request,
        'setup.html',
        {
            'link': link,
            'target': user,
            'token': token,
            'passkeys_allowed': c.settings.passkeys_allowed,
        },
    )


@router.post('/setup/{token}/options')
async def setup_options(token: str, request: Request, db: Session = DB) -> Response:
    body = await _json(request)
    limit(request, 'register_ip', client_ip(request))
    c = ctx(request)
    link = svc.find_link(db, c.clock, token)
    if link is None:
        return JSONResponse({'error': 'This setup link has expired.'}, status_code=410)
    if link.purpose == 'bootstrap':
        try:
            username = normalise_username(str(body.get('username') or ''))
        except UserError as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)
        display = str(body.get('display_name') or '').strip() or username
        handle = secrets.token_bytes(32)
        out = svc.registration_options(
            db,
            c.settings,
            c.clock,
            user=None,
            username=username,
            display_name=display,
            user_handle=handle,
            link=link,
            data={'username': username, 'display_name': display},
        )
    else:
        user = db.get(User, link.user_id)
        assert user is not None
        out = svc.registration_options(
            db,
            c.settings,
            c.clock,
            user=user,
            username=user.username,
            display_name=user.display_name,
            user_handle=user.user_handle,
            link=link,
        )
    return JSONResponse(out)


@router.post('/setup/{token}/verify')
async def setup_verify(token: str, request: Request, db: Session = DB) -> Response:
    body = await _json(request)
    ip = client_ip(request)
    limit(request, 'register_ip', ip)
    c = ctx(request)
    try:
        user, link = svc.complete_setup(
            db,
            c.settings,
            c.clock,
            token,
            str(body.get('handle', '')),
            body.get('credential') or {},
            str(body.get('name') or 'Passkey'),
            ip,
        )
    except (svc.AuthError, UserError) as exc:
        return JSONResponse({'error': str(exc)}, status_code=400)
    session = svc.start_session(
        db, c.settings, c.clock, user, 'setup', ip, request.headers.get('user-agent', '')
    )
    target = '/settings?recovered=1' if link.purpose == 'recovery' else '/'
    resp = JSONResponse({'ok': True, 'redirect': target})
    set_session_cookie(request, resp, session)
    return resp
