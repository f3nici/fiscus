"""Manifest, service worker and the offline screen."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from fiscus import __version__
from fiscus.web import ctx, render

router = APIRouter()
STATIC = Path(__file__).resolve().parent.parent / 'static'
SW_SOURCE = STATIC / 'js' / 'sw.src.js'
THEME = '#1a1a1a'


@lru_cache(maxsize=1)
def shell_assets() -> tuple[list[str], str]:
    """Every static file, plus a digest of their bytes that names the cache."""
    files = sorted(p for p in STATIC.rglob('*') if p.is_file() and p != SW_SOURCE)
    digest = hashlib.sha256()
    urls = []
    for p in files:
        rel = p.relative_to(STATIC).as_posix()
        if rel.endswith(('.txt', '.md')):
            continue
        urls.append(f'/static/{rel}')
        digest.update(rel.encode())
        digest.update(p.read_bytes())
    digest.update(SW_SOURCE.read_bytes())
    return urls, digest.hexdigest()[:12]


@router.get('/manifest.webmanifest')
def manifest() -> Response:
    body = {
        'name': 'Fiscus',
        'short_name': 'Fiscus',
        'description': 'Household finances, kept at home.',
        'id': '/',
        'start_url': '/',
        'scope': '/',
        'display': 'standalone',
        'orientation': 'portrait',
        'background_color': THEME,
        'theme_color': THEME,
        'icons': [
            {
                'src': '/static/icons/icon-192.png',
                'sizes': '192x192',
                'type': 'image/png',
                'purpose': 'any',
            },
            {
                'src': '/static/icons/icon-512.png',
                'sizes': '512x512',
                'type': 'image/png',
                'purpose': 'any',
            },
            {
                'src': '/static/icons/maskable-512.png',
                'sizes': '512x512',
                'type': 'image/png',
                'purpose': 'maskable',
            },
        ],
        'share_target': {
            'action': '/share-target',
            'method': 'POST',
            'enctype': 'multipart/form-data',
            'params': {'files': [{'name': 'file', 'accept': ['text/csv', '.csv', 'text/plain']}]},
        },
        'shortcuts': [
            {'name': 'Import CSV', 'url': '/import'},
            {'name': 'Calendar', 'url': '/calendar'},
        ],
    }
    return JSONResponse(body, media_type='application/manifest+json')


@router.get('/sw.js')
def service_worker(request: Request) -> Response:
    urls, digest = shell_assets()
    version = f'{__version__}-{ctx(request).settings.build_hash}-{digest}'
    js = (
        f'const VERSION = {json.dumps(version)};\n'
        f'const SHELL = {json.dumps(["/offline", *urls])};\n' + SW_SOURCE.read_text()
    )
    return Response(
        js, media_type='application/javascript', headers={'Service-Worker-Allowed': '/'}
    )


@router.get('/offline')
def offline(request: Request) -> Response:
    # Deliberately static: no user, no figures, nothing that could go stale.
    request.state.user_view = None
    return render(request, 'offline.html')
