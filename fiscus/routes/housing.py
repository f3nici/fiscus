"""Housing costs: manual line items and file import with mapping, preview and diff."""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from fiscus.domain import housing as dom
from fiscus.domain.csvimport import MAX_BYTES, ImportError_
from fiscus.forms import FormError, choice, money, opt_date, text
from fiscus.models import FREQUENCIES, Account, HousingItem, Loan, User
from fiscus.services import housing as svc
from fiscus.services import ledger, loans
from fiscus.services.access import get_owned, maybe_owned, owned
from fiscus.web import ctx, current_user, get_db, redirect, render

router = APIRouter()
UPLOAD_NAME = re.compile(r'^housing-(\d+)-([0-9a-f]{32})\.(csv|json)$')


@router.get('/housing')
def housing_page(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    c = ctx(request)
    items = svc.items(db, user)
    others = (
        db.execute(select(User).where(User.is_active.is_(True), User.id != user.id)).scalars().all()
    )
    people = {u.id: u for u in others}
    cats = {cat.id: cat.name for cat in ledger.categories(db, user)}
    monthly = {i.id: dom_monthly(i) for i in items}
    loan_rows = [
        loans.summarise(db, user, loan, c.clock.today())
        for loan in db.execute(owned(Loan, user)).scalars()
    ]
    edit = maybe_owned(db, HousingItem, int(request.query_params.get('edit', '0') or 0), user)
    return render(
        request,
        'housing/index.html',
        {
            'items': items,
            'people': people,
            'others': others,
            'cats': cats,
            'monthly': monthly,
            'total': sum(monthly.values()),
            'freqs': FREQUENCIES,
            'loans': loan_rows,
            'accounts': ledger.accounts(db, user),
            'edit': edit,
            'edit_cat': cats.get(edit.category_id, '') if edit else '',
        },
    )


def dom_monthly(item: HousingItem) -> int:
    from fiscus.domain.recurrence import monthly_equivalent

    return monthly_equivalent(svc.my_share(item), item.frequency)


@router.post('/housing/import')
async def upload(
    request: Request, file: UploadFile = File(...), user: User = Depends(current_user)
) -> Response:
    c = ctx(request)
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        return redirect('/housing', flash='That file is larger than 10 MB', request=request)
    fname = (file.filename or '').lower()
    kind = 'json' if fname.endswith('.json') or data.lstrip()[:1] in (b'{', b'[') else 'csv'
    name = f'housing-{user.id}-{uuid.uuid4().hex}.{kind}'
    (c.settings.upload_dir / name).write_bytes(data)
    return redirect(f'/housing/import/{name}', request=request)


@router.post('/housing')
@router.post('/housing/{item_id}')
def save_item(
    request: Request,
    item_id: int | None = None,
    name: str = Form(...),
    amount: str = Form(...),
    frequency: str = Form(...),
    category: str = Form('Housing'),
    due_date: str = Form(''),
    shared_with: str = Form(''),
    split_percent: str = Form('50'),
    account_id: int = Form(0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    c = ctx(request)
    existing = get_owned(db, HousingItem, item_id, user) if item_id else None
    try:
        freq = choice(frequency, FREQUENCIES, 'Frequency')
        due = opt_date(due_date, 'Due date')
        if freq == 'one_off' and due is None:
            raise FormError('A one-off item needs a due date')
        split_bp = 0
        if shared_with.strip():
            try:
                pct = float(split_percent or 50)
            except ValueError as exc:
                raise FormError('Their share: enter a percentage') from exc
            if not 0 < pct < 100:
                raise FormError('Their share must be between 0 and 100 percent')
            split_bp = round(pct * 100)
        spec = dom.ItemSpec(
            name=text(name, 'Name'),
            amount_cents=money(amount, 'Amount', allow_negative=False),
            frequency=freq,
            category=category.strip() or 'Housing',
            due_date=due,
            shared_with=shared_with.strip().lower(),
            split_bp=split_bp,
        )
        clash = db.execute(
            owned(HousingItem, user).where(HousingItem.name == spec.name)
        ).scalar_one_or_none()
        if clash is not None and clash is not existing:
            raise FormError('You already have an item with that name')
        acct = maybe_owned(db, Account, account_id, user)
        svc.upsert(db, c.clock, user, spec, acct.id if acct else None, existing)
    except (FormError, svc.HousingError) as exc:
        return redirect('/housing', flash=str(exc), request=request)
    return redirect('/housing', flash=f'{spec.name} saved', request=request)


@router.post('/housing/{item_id}/delete')
def delete_item(
    item_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    item = get_owned(db, HousingItem, item_id, user)
    svc.remove(db, user, item)
    return redirect('/housing', flash=f'{item.name} removed', request=request)


@router.get('/housing/template.csv')
def template_csv() -> Response:
    return PlainTextResponse(
        dom.template_csv(),
        media_type='text/csv',
        headers={'Content-Disposition': 'attachment; filename="fiscus-housing-template.csv"'},
    )


@router.get('/housing/template.json')
def template_json() -> Response:
    return PlainTextResponse(
        dom.template_json(),
        media_type='application/json',
        headers={'Content-Disposition': 'attachment; filename="fiscus-housing-template.json"'},
    )


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
        out[f] = int(v) if v.isdigit() else None
    return out


def _parse(data: bytes, kind: str, mapping: dict[str, int | None] | None) -> dom.ParsedItems:
    return dom.parse_json(data) if kind == 'json' else dom.parse_csv(data, mapping)


@router.get('/housing/import/{name}')
def review(
    name: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    data, kind = _staged(request, user, name)
    mapping = _mapping(request)
    try:
        if kind == 'csv':
            header, body = dom.csv_header(data)
            effective = mapping or dom.default_mapping(header)
            if (mapping is None and not dom.mapping_complete(effective)) or (
                mapping is not None and not dom.mapping_complete(mapping)
            ):
                return render(
                    request,
                    'housing/mapping.html',
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
        return redirect('/housing', flash=str(exc), request=request)
    existing = [svc.as_spec(db, i) for i in svc.items(db, user)]
    diff = dom.diff(existing, parsed.items)
    return render(
        request,
        'housing/preview.html',
        {
            'name': name,
            'diff': diff,
            'errors': parsed.errors,
            'mapping': mapping or {},
        },
    )


@router.post('/housing/import/{name}/apply')
async def apply(
    name: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    c = ctx(request)
    data, kind = _staged(request, user, name)
    form = await request.form()
    mapping: dict[str, int | None] | None = None
    if kind == 'csv':
        mapping = {}
        for f in dom.FIELDS:
            v = form.get(f'map_{f}')
            mapping[f] = int(v) if isinstance(v, str) and v.isdigit() else None
    try:
        parsed = _parse(data, kind, mapping)
        existing = [svc.as_spec(db, i) for i in svc.items(db, user)]
        diff = dom.diff(existing, parsed.items)
        result = svc.apply_diff(
            db, c.clock, user, diff, remove_missing=form.get('remove_missing') == 'on'
        )
    except (ImportError_, svc.HousingError) as exc:
        db.rollback()
        return redirect(f'/housing/import/{name}', flash=str(exc), request=request)
    (c.settings.upload_dir / name).unlink(missing_ok=True)
    return redirect(
        '/housing',
        flash=f'Imported: {result.added} added, {result.changed} changed, {result.removed} removed',
        request=request,
    )
