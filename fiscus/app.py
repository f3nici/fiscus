"""Application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from fiscus import __version__
from fiscus.clock import Clock
from fiscus.config import Settings, load_settings
from fiscus.db import make_engine, make_sessionmaker, run_migrations
from fiscus.domain.tax import CONFIG_DIR
from fiscus.security.ratelimit import RateLimiter
from fiscus.services import auth as auth_service
from fiscus.services import versioning
from fiscus.web import (
    AppContext,
    LoginRequired,
    RateLimited,
    csrf_guard,
    ensure_csrf_cookie,
    make_templates,
    render,
)

log = logging.getLogger('fiscus')
STATIC_DIR = Path(__file__).parent / 'static'

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"
)


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    migrate: bool = True,
    tax_dir: Path | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    if migrate:
        run_migrations(settings.database_url)
    versioning.install()
    engine = make_engine(settings.database_url)
    context = AppContext(
        settings=settings,
        engine=engine,
        sessions=make_sessionmaker(engine),
        clock=clock or Clock(settings.tz),
        limiter=RateLimiter(),
        templates=make_templates(),
        tax_dir=tax_dir or settings.tax_dir or CONFIG_DIR,
    )

    if not settings.passkeys_allowed:
        log.error(
            'FISCUS_ORIGIN is %s. Passkeys need https:// (or localhost for development), '
            'so passkey sign-in and registration are refused until it is fixed.',
            settings.origin,
        )

    with context.sessions() as db:
        link = auth_service.ensure_bootstrap(db, settings, context.clock)
        db.commit()
    if link is not None:
        log.warning(
            '\n\n  No users yet. Register the first admin passkey at:\n\n    %s\n\n'
            '  This bootstrap link expires %s UTC and is replaced on every restart\n'
            '  until somebody uses it.\n',
            link.url,
            link.expires_at.isoformat(timespec='minutes'),
        )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        task = None
        if settings.run_jobs:
            from fiscus.jobs import run_forever

            task = asyncio.create_task(run_forever(context))
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            engine.dispose()

    app = FastAPI(
        title='Fiscus',
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
        dependencies=[Depends(csrf_guard)],
    )
    app.state.ctx = context

    @app.middleware('http')
    async def head_as_get(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Uptime checks and some proxies probe with HEAD. Answer it as a GET
        # without a body rather than with 405.
        if request.method != 'HEAD':
            return await call_next(request)
        request.scope['method'] = 'GET'
        response = await call_next(request)
        return Response(
            status_code=response.status_code,
            headers={k: v for k, v in response.headers.items() if k.lower() != 'content-length'},
        )

    @app.middleware('http')
    async def headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        ensure_csrf_cookie(request)
        response = await call_next(request)
        path = request.url.path
        response.headers.setdefault('Content-Security-Policy', CSP)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        if settings.cookie_secure:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        if path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-cache'
        elif path in ('/sw.js', '/manifest.webmanifest'):
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        else:
            # Financial data is never stored by the browser or the service worker.
            response.headers['Cache-Control'] = 'no-store'
            response.headers['Pragma'] = 'no-cache'
        if getattr(request.state, 'csrf_new', False):
            response.set_cookie(
                settings.csrf_cookie,
                request.state.csrf,
                httponly=True,
                secure=settings.cookie_secure,
                samesite='strict',
                path='/',
            )
        return response

    @app.exception_handler(LoginRequired)
    async def _login(request: Request, _exc: LoginRequired) -> Response:
        if request.headers.get('hx-request') == 'true':
            return Response(status_code=401, headers={'HX-Redirect': '/login'})
        if request.url.path.startswith('/api/') or 'application/json' in request.headers.get(
            'accept', ''
        ):
            return JSONResponse({'error': 'Not signed in'}, status_code=401)
        target = request.url.path
        if request.url.query:
            target += '?' + request.url.query
        nxt = f'?next={target}' if target not in ('/', '/login') else ''
        return RedirectResponse(f'/login{nxt}', status_code=303)

    @app.exception_handler(RateLimited)
    async def _limited(request: Request, exc: RateLimited) -> Response:
        msg = f'Too many attempts. Try again in {exc.retry_after} seconds.'
        headers = {'Retry-After': str(exc.retry_after)}
        if 'application/json' in request.headers.get('accept', '') or request.headers.get(
            'content-type', ''
        ).startswith('application/json'):
            return JSONResponse({'error': msg}, status_code=429, headers=headers)
        return render(
            request,
            'error.html',
            {'title': 'Slow down', 'message': msg},
            status_code=429,
            headers=headers,
        )

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException) -> Response:
        message = exc.detail if isinstance(exc.detail, str) else 'Something went wrong'
        if exc.status_code == 404:
            message = 'That page does not exist, or it is not yours.'
        if request.url.path.startswith('/api/') or request.headers.get(
            'content-type', ''
        ).startswith('application/json'):
            return JSONResponse({'error': message}, status_code=exc.status_code)
        return render(
            request,
            'error.html',
            {
                'title': {404: 'Not found', 403: 'Refused'}.get(exc.status_code, 'Error'),
                'message': message,
            },
            status_code=exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> Response:
        fields = sorted({str(e['loc'][-1]) for e in exc.errors() if e.get('loc')})
        message = 'Check these fields: ' + ', '.join(fields) if fields else 'Check the form'
        if request.headers.get('content-type', '').startswith('application/json'):
            return JSONResponse({'error': message}, status_code=422)
        return render(
            request, 'error.html', {'title': 'Check the form', 'message': message}, status_code=422
        )

    app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')

    from fiscus.routes import (
        accounts,
        admin,
        bills,
        budget,
        calendar,
        dashboard,
        forecast,
        health,
        housing,
        imports,
        loans,
        pay,
        pwa,
        settings_routes,
        setup,
        tax,
        transactions,
    )
    from fiscus.routes import auth as auth_routes
    from fiscus.routes import categories as category_routes

    for module in (
        health,
        pwa,
        auth_routes,
        setup,
        admin,
        settings_routes,
        dashboard,
        accounts,
        imports,
        transactions,
        category_routes,
        bills,
        pay,
        calendar,
        loans,
        forecast,
        budget,
        housing,
        tax,
    ):
        app.include_router(module.router)

    return app


def main_app() -> FastAPI:
    """Entry point for `uvicorn fiscus.app:main_app --factory`."""
    logging.basicConfig(
        level=load_settings().log_level.upper(),
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    return create_app()
