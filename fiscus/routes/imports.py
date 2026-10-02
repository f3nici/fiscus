"""CSV import: upload, map, preview, commit, undo."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from fiscus.domain.csvimport import DATE_FORMATS, MAX_BYTES, ImportError_, Mapping
from fiscus.models import Account, Import, User
from fiscus.services import imports as svc
from fiscus.services import ledger
from fiscus.services.access import get_owned, owned
from fiscus.services.forecasting import spending_account
from fiscus.web import ctx, current_user, get_db, optional_user, redirect, render

router = APIRouter()


async def _read(upload: UploadFile) -> bytes:
    data = await upload.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ImportError_('That file is larger than 10 MB')
    return data


@router.get('/import')
def import_page(
    request: Request,
    account_id: int = 0,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    history = (
        db.execute(
            owned(Import, user)
            .where(Import.status != 'pending')
            .order_by(Import.id.desc())
            .limit(25)
        )
        .scalars()
        .all()
    )
    accts = ledger.accounts(db, user)
    return render(
        request,
        'imports/upload.html',
        {
            'accounts': accts,
            'history': history,
            'names': {a.id: a.name for a in ledger.accounts(db, user, include_archived=True)},
            'selected': account_id,
            'shared': request.query_params.get('shared') == '1',
        },
    )


@router.post('/import')
async def upload(
    request: Request,
    account_id: int = Form(...),
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    c = ctx(request)
    acct = get_owned(db, Account, account_id, user)
    try:
        data = await _read(file)
        imp = svc.stage(db, c.settings, c.clock, user, acct, file.filename or 'upload.csv', data)
    except ImportError_ as exc:
        return redirect(f'/import?account_id={acct.id}', flash=str(exc), request=request)
    return redirect(f'/import/{imp.id}', request=request)


@router.post('/share-target')
async def share_target(
    request: Request,
    file: UploadFile | None = File(None),
    user: User | None = Depends(optional_user),
    db: Session = Depends(get_db),
) -> Response:
    """Fallback for Android's share sheet when the service worker did not catch it.

    Exempt from the CSRF token (the OS posts it), so it only ever stages a file
    for the signed-in user to review; nothing is imported without a click.
    """
    if user is None:
        return redirect('/login?next=/import')
    accts = ledger.accounts(db, user)
    target = spending_account(db, user, accts)
    if file is None or target is None:
        return redirect('/import', flash='Choose a CSV to import')
    c = ctx(request)
    try:
        imp = svc.stage(
            db,
            c.settings,
            c.clock,
            user,
            db.get(Account, target),  # type: ignore[arg-type]
            file.filename or 'shared.csv',
            await _read(file),
        )
    except ImportError_ as exc:
        return redirect('/import', flash=str(exc))
    return redirect(f'/import/{imp.id}')


def _pending(db: Session, user: User, import_id: int) -> Import:
    return get_owned(db, Import, import_id, user)


@router.get('/import/{import_id}')
def review(
    import_id: int,
    request: Request,
    remap: int = 0,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    c = ctx(request)
    imp = _pending(db, user, import_id)
    if imp.status != 'pending':
        return redirect('/import', request=request)
    try:
        rows = svc.raw_rows(c.settings, imp)
        mapping, fmt = svc.mapping_for(db, c.settings, imp)
        if mapping is None or remap:
            width = max((len(r) for r in rows[:20]), default=0)
            return render(
                request,
                'imports/mapping.html',
                {
                    'imp': imp,
                    'sample': rows[:8],
                    'width': width,
                    'formats': list(DATE_FORMATS),
                    'mapping': mapping or Mapping(has_header=True, amount_col=None),
                    'accounts': ledger.accounts(db, user),
                },
            )
        pv = svc.preview(db, c.settings, imp, mapping)
    except ImportError_ as exc:
        return render(
            request,
            'imports/mapping.html',
            {
                'imp': imp,
                'sample': [],
                'width': 0,
                'formats': list(DATE_FORMATS),
                'mapping': Mapping(),
                'error': str(exc),
                'accounts': ledger.accounts(db, user),
            },
            status_code=400,
        )
    return render(
        request,
        'imports/preview.html',
        {
            'imp': imp,
            'pv': pv,
            'fmt': fmt,
            'accounts': ledger.accounts(db, user),
            'account': db.get(Account, imp.account_id),
        },
    )


@router.post('/import/{import_id}/account')
def change_account(
    import_id: int,
    request: Request,
    account_id: int = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    imp = _pending(db, user, import_id)
    acct = get_owned(db, Account, account_id, user)
    if imp.status == 'pending':
        imp.account_id = acct.id
    return redirect(f'/import/{imp.id}', request=request)


@router.post('/import/{import_id}/mapping')
async def save_mapping(
    import_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    imp = _pending(db, user, import_id)
    form = await request.form()

    def col(key: str) -> int | None:
        v = form.get(key)
        return (
            int(v)
            if isinstance(v, str) and v.strip().lstrip('-').isdigit() and int(v) >= 0
            else None
        )

    desc = [int(v) for v in form.getlist('description_cols') if isinstance(v, str) and v.isdigit()]
    try:
        mapping = Mapping.from_dict(
            {
                'has_header': form.get('has_header') == 'on',
                'date_col': col('date_col') or 0,
                'date_format': form.get('date_format') or 'DD/MM/YYYY',
                'amount_col': col('amount_col'),
                'debit_col': col('debit_col'),
                'credit_col': col('credit_col'),
                'description_cols': desc or [2],
                'invert': form.get('invert') == 'on',
            }
        )
        svc.preview(db, ctx(request).settings, imp, mapping)
    except (ImportError_, ValueError) as exc:
        return redirect(f'/import/{imp.id}?remap=1', flash=str(exc), request=request)
    svc.save_mapping(db, user, imp.account_id, mapping)
    return redirect(f'/import/{imp.id}', request=request)


@router.post('/import/{import_id}/commit')
def commit(
    import_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    c = ctx(request)
    imp = _pending(db, user, import_id)
    try:
        mapping, fmt = svc.mapping_for(db, c.settings, imp)
        if mapping is None:
            return redirect(f'/import/{imp.id}?remap=1', request=request)
        svc.commit(db, c.settings, c.clock, user, imp, mapping, fmt)
    except ImportError_ as exc:
        return redirect('/import', flash=str(exc), request=request)
    return redirect(
        f'/accounts/{imp.account_id}',
        flash=(
            f'Imported {imp.imported_count} transactions, skipped {imp.duplicate_count} duplicates'
        ),
        request=request,
    )


@router.post('/import/{import_id}/undo')
def undo(
    import_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    c = ctx(request)
    imp = _pending(db, user, import_id)
    try:
        n = svc.undo(db, c.settings, c.clock, imp)
    except ImportError_ as exc:
        return redirect('/import', flash=str(exc), request=request)
    return redirect(
        '/import', flash=f'Undid {imp.file_name}: removed {n} transactions', request=request
    )


@router.post('/import/{import_id}/discard')
def discard(
    import_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    imp = _pending(db, user, import_id)
    if imp.status == 'pending':
        svc.discard(db, ctx(request).settings, imp)
    return redirect('/import', flash='Upload discarded', request=request)
