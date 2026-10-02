"""Health and readiness for Docker and the reverse proxy."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from fiscus import __version__
from fiscus.web import ctx

router = APIRouter()


@router.get('/healthz')
def healthz() -> JSONResponse:
    return JSONResponse({'status': 'ok'})


@router.get('/readyz')
def readyz(request: Request) -> JSONResponse:
    c = ctx(request)
    checks = {'database': False, 'migrations': False, 'data_dir_writable': False}
    try:
        with c.engine.connect() as conn:
            conn.execute(text('SELECT 1'))
            checks['database'] = True
            checks['migrations'] = (
                conn.execute(text('SELECT version_num FROM alembic_version')).first() is not None
            )
    except Exception:  # noqa: S110
        pass
    probe = c.settings.data_dir / '.write-probe'
    try:
        probe.write_text('ok')
        probe.unlink()
        checks['data_dir_writable'] = True
    except OSError:
        pass
    ok = all(checks.values())
    return JSONResponse(
        {
            'status': 'ready' if ok else 'not ready',
            'version': __version__,
            'build': c.settings.build_hash,
            'checks': checks,
        },
        status_code=200 if ok else 503,
    )
