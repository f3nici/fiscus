"""Request plumbing: the app context, dependencies, CSRF and rendering."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from fiscus import __version__
from fiscus.clock import Clock, utcnow_naive
from fiscus.config import Settings
from fiscus.models import AuthSession, User
from fiscus.money import fmt, fmt_plain, fmt_short
from fiscus.security.ratelimit import RateLimiter
from fiscus.security.tokens import new_token, same, token_hash

TEMPLATE_DIR = Path(__file__).parent / 'templates'
UNSAFE = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})
SLIDE_EVERY = timedelta(minutes=5)


@dataclass
class AppContext:
    settings: Settings
    engine: Engine
    sessions: sessionmaker[Session]
    clock: Clock
    limiter: RateLimiter
    templates: Jinja2Templates
    tax_dir: Path


@dataclass(frozen=True)
class UserView:
    """What templates see of the signed-in user. Survives a rolled-back session."""

    id: int
    username: str
    display_name: str
    is_admin: bool


def user_view(user: User) -> UserView:
    return UserView(
        user.id,
        user.username,
        user.display_name,
        user.is_admin,
    )


class LoginRequired(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after)
        self.retry_after = retry_after


def ctx(request: Request) -> AppContext:
    c: AppContext = request.app.state.ctx
    return c


def get_db(request: Request) -> Iterator[Session]:
    db = ctx(request).sessions()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def client_ip(request: Request) -> str:
    # uvicorn rewrites request.client from X-Forwarded-For only when the
    # immediate peer is in FISCUS_TRUSTED_PROXIES, so this is safe to trust.
    return request.client.host if request.client else ''


def load_session(request: Request, db: Session) -> tuple[AuthSession, User] | None:
    c = ctx(request)
    token = request.cookies.get(c.settings.session_cookie)
    if not token:
        return None
    row = db.execute(
        select(AuthSession).where(AuthSession.token_hash == token_hash(token))
    ).scalar_one_or_none()
    if row is None:
        return None
    now = utcnow_naive(c.clock)
    if row.expires_at <= now or row.absolute_expires_at <= now:
        db.delete(row)
        return None
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        db.delete(row)
        return None
    if now - row.last_seen_at >= SLIDE_EVERY:
        row.last_seen_at = now
        row.expires_at = min(
            now + timedelta(days=c.settings.session_idle_days), row.absolute_expires_at
        )
    return row, user


def optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    cached: User | None = getattr(request.state, 'user', None)
    if cached is not None:
        return cached
    found = load_session(request, db)
    if found is None:
        return None
    request.state.user = found[1]
    request.state.user_view = user_view(found[1])
    request.state.auth_session = found[0]
    return found[1]


def current_user(user: User | None = Depends(optional_user)) -> User:
    if user is None:
        raise LoginRequired
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=404)
    return user


# ---------------------------------------------------------------------- CSRF


def _origin_ok(request: Request, settings: Settings) -> bool:
    origin = request.headers.get('origin')
    if origin:
        return origin.rstrip('/') == settings.origin
    referer = request.headers.get('referer')
    if referer:
        parts = urlsplit(referer)
        return f'{parts.scheme}://{parts.netloc}' == settings.origin
    # Neither header: a non-browser client. The token check below still applies.
    return True


async def csrf_guard(request: Request) -> None:
    if request.method not in UNSAFE:
        return
    settings = ctx(request).settings
    if not _origin_ok(request, settings):
        raise HTTPException(status_code=403, detail='Cross-origin request refused')
    expected = request.cookies.get(settings.csrf_cookie, '')
    sent = request.headers.get('x-csrf-token', '')
    if not sent:
        ctype = request.headers.get('content-type', '')
        if ctype.startswith(('application/x-www-form-urlencoded', 'multipart/form-data')):
            form = await request.form()
            value = form.get('csrf_token')
            sent = value if isinstance(value, str) else ''
    if not expected or not sent or not same(expected, sent):
        raise HTTPException(status_code=403, detail='Your form expired. Reload and try again.')


def ensure_csrf_cookie(request: Request) -> str:
    settings = ctx(request).settings
    existing = request.cookies.get(settings.csrf_cookie)
    if existing and len(existing) >= 32:
        request.state.csrf = existing
        request.state.csrf_new = False
        return existing
    token = new_token()
    request.state.csrf = token
    request.state.csrf_new = True
    return token


# ---------------------------------------------------------------- rendering


def _date_fmt(d: date | None, style: str = 'short') -> str:
    if d is None:
        return ''
    if style == 'long':
        return f'{d.strftime("%A")} {d.day} {d.strftime("%B %Y")}'
    if style == 'month':
        return d.strftime('%B %Y')
    if style == 'dm':
        return f'{d.day} {d.strftime("%b")}'
    return f'{d.day} {d.strftime("%b %Y")}'


def make_templates() -> Jinja2Templates:
    t = Jinja2Templates(directory=str(TEMPLATE_DIR))
    env = t.env
    env.filters['money'] = fmt
    env.filters['money_signed'] = lambda c: fmt(c, sign=True)
    env.filters['money_plain'] = fmt_plain
    env.filters['money_short'] = fmt_short
    env.filters['d'] = _date_fmt
    env.filters['tojson_attr'] = lambda v: json.dumps(v, separators=(',', ':'))
    env.globals['version'] = __version__
    return t


def render(
    request: Request,
    template: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    c = ctx(request)
    data: dict[str, Any] = {
        'request': request,
        'user': getattr(request.state, 'user_view', None),
        'csrf_token': getattr(request.state, 'csrf', ''),
        'today': c.clock.today(),
        'settings': c.settings,
        'flash': unquote(request.cookies.get('fiscus_flash', '')),
        'path': request.url.path,
        'is_htmx': request.headers.get('hx-request') == 'true',
    }
    data.update(context or {})
    resp = c.templates.TemplateResponse(
        request, template, data, status_code=status_code, headers=headers
    )
    if data['flash']:
        resp.delete_cookie('fiscus_flash', path='/')
    return resp


def local_path(target: object, fallback: str = '/') -> str:
    """`target` when it is a path on this site, else `fallback`. Never another
    origin: no `//host`, no backslashes a browser might read as slashes, and no
    control characters, which browsers drop (so `/<tab>/host` is `//host`)."""
    if not isinstance(target, str) or not target.startswith('/'):
        return fallback
    if '\\' in target or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in target):
        return fallback
    parts = urlsplit(target)
    if target.startswith('//') or parts.scheme or parts.netloc:
        return fallback
    return target


def redirect(url: str, *, flash: str | None = None, request: Request | None = None) -> Response:
    if request is not None and request.headers.get('hx-request') == 'true':
        resp: Response = Response(status_code=204, headers={'HX-Location': url})
    else:
        resp = RedirectResponse(url, status_code=303)
    if flash:
        resp.set_cookie(
            'fiscus_flash', quote(flash[:300]), max_age=60, path='/', httponly=True, samesite='lax'
        )
    return resp
