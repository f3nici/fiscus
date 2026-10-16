"""Importing bills from a CSV or JSON file: mapping, preview with a diff, apply.

Registered before the bills router: these literal paths sit beside /bills/{id}.
"""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from fiscus.domain import bill_import as dom
from fiscus.domain.uploads import MAX_BYTES, ImportError_
from fiscus.forms import int_or
from fiscus.models import User
from fiscus.services import bill_import as svc
from fiscus.services.bills import user_bills
from fiscus.web import DB, ctx, current_user, redirect, render

router = APIRouter()
UPLOAD_NAME = re.compile(r'^bills-(\d+)-([0-9a-f]{32})\.(csv|json)$')


@router.get('/housing')
def housing_moved() -> Response:
    # Housing costs are bills now; old bookmarks and installed shortcuts land here.
    return RedirectResponse('/bills', status_code=308)


@router.get('/bills/template.csv')
def template_csv() -> Response:
    return PlainTextResponse(
        dom.template_csv(),
        media_type='text/csv',
        headers={'Content-Disposition': 'attachment; filename="fiscus-bills-template.csv"'},
    )


@router.get('/bills/template.json')
def template_json() -> Response:
    return PlainTextResponse(
        dom.template_json(),
        media_type='application/json',
        headers={'Content-Disposition': 'attachment; filename="fiscus-bills-template.json"'},
    )


@router.get('/bills/import')
def import_page(request: Request, user: User = Depends(current_user)) -> Response:
    return render(request, 'bills/import.html', {})


@router.post('/bills/import')
async def upload(
    request: Request, file: UploadFile = File(...), user: User = Depends(current_user)
) -> Response:
    c = ctx(request)
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        return redirect('/bills/import', flash='That file is larger than 10 MB', request=request)
    fname = (file.filename or '').lower()
    kind = 'json' if fname.endswith('.json') or data.lstrip()[:1] in (b'{', b'[') else 'csv'
    name = f'bills-{user.id}-{uuid.uuid4().hex}.{kind}'
    (c.settings.upload_dir / name).write_bytes(data)
    return redirect(f'/bills/import/{name}', request=request)


def _staged(request: Request, user: User, name: str) -> tuple[bytes, str]:
    m = UPLOAD_NAME.match(name)
    if m is None or int(m.group(1)) != user.id:
        raise HTTPException(status_code=404)
    path = ctx(request).settings.upload_dir / name
    if not path.exists():
        raise HTTPException(status_code=404)
    return path.read_bytes(), m.group(3)


def _mapping(request: Request) -> dict[str, int | None] | None:
    q = request.query_params
    if not any(k.startswith('map_') for k in q):
        return None
    out: dict[str, int | None] = {}
    for f in dom.FIELDS:
        v = q.get(f'map_{f}', '')
        out[f] = _column(v)
    return out


def _column(value: object) -> int | None:
    """A chosen column number, or None for "not in the file"."""
    n = int_or(value, -1)
    return n if n >= 0 else None


def _parse(data: bytes, kind: str, mapping: dict[str, int | None] | None) -> dom.ParsedBills:
    return dom.parse_json(data) if kind == 'json' else dom.parse_csv(data, mapping)


def _diff(db: Session, user: User, parsed: dom.ParsedBills) -> dom.Diff:
    existing = [svc.as_spec(db, b) for b in user_bills(db, user)]
    return dom.diff(existing, parsed.items)


@router.get('/bills/import/{name}')
def review(
    name: str, request: Request, user: User = Depends(current_user), db: Session = DB
) -> Response:
    data, kind = _staged(request, user, name)
    mapping = _mapping(request)
    try:
        if kind == 'csv':
            header, body = dom.csv_header(data)
            effective = mapping or dom.default_mapping(header)
            if not dom.mapping_complete(effective):
                return render(
                    request,
                    'bills/import_mapping.html',
                    {
                        'name': name,
                        'header': header,
                        'sample': body[:5],
                        'fields': dom.FIELDS,
                        'required': dom.REQUIRED,
                        'mapping': effective,
                    },
                )
            mapping = effective
        parsed = _parse(data, kind, mapping)
    except ImportError_ as exc:
        return redirect('/bills/import', flash=str(exc), request=request)
    return render(
        request,
        'bills/import_preview.html',
        {
            'name': name,
            'diff': _diff(db, user, parsed),
            'errors': parsed.errors,
            'mapping': mapping or {},
        },
    )


@router.post('/bills/import/{name}/apply')
async def apply(
    name: str, request: Request, user: User = Depends(current_user), db: Session = DB
) -> Response:
    c = ctx(request)
    data, kind = _staged(request, user, name)
    form = await request.form()
    mapping: dict[str, int | None] | None = None
    if kind == 'csv':
        mapping = {}
        for f in dom.FIELDS:
            v = form.get(f'map_{f}')
            mapping[f] = _column(v)
    try:
        parsed = _parse(data, kind, mapping)
        result = svc.apply_diff(
            db,
            c.clock,
            user,
            _diff(db, user, parsed),
            remove_missing=form.get('remove_missing') == 'on',
        )
    except ImportError_ as exc:
        db.rollback()
        return redirect(f'/bills/import/{name}', flash=str(exc), request=request)
    (c.settings.upload_dir / name).unlink(missing_ok=True)
    return redirect(
        '/bills',
        flash=f'Imported: {result.added} added, {result.changed} changed, {result.removed} stopped',
        request=request,
    )
