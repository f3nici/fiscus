"""Personal settings: passkeys, forecast preferences, ntfy and, for the admin, backups."""

from __future__ import annotations

import shutil

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from fiscus.models import Account, User, WebAuthnCredential
from fiscus.money import MoneyError, parse_cents
from fiscus.routes.auth import _json, limit
from fiscus.services import audit, backup, ntfy
from fiscus.services import auth as svc
from fiscus.services.access import in_range, owned
from fiscus.services.users import passkey_count, settings_for
from fiscus.web import DB, admin_user, client_ip, ctx, current_user, redirect, render

router = APIRouter()


def _page(
    request: Request,
    db: Session,
    user: User,
    extra: dict[str, object] | None = None,
    status_code: int = 200,
) -> Response:
    creds = (
        db.execute(
            select(WebAuthnCredential)
            .where(WebAuthnCredential.user_id == user.id)
            .order_by(WebAuthnCredential.created_at)
        )
        .scalars()
        .all()
    )
    accounts = db.execute(owned(Account, user).order_by(Account.name)).scalars().all()
    c = ctx(request)
    return render(
        request,
        'settings.html',
        {
            'creds': creds,
            'backups': backup.list_backups(c.settings) if user.is_admin else [],
            'retention': c.settings.backup_retention_days,
            'prefs': settings_for(db, user),
            'accounts': accounts,
            'recovered': request.query_params.get('recovered') == '1',
            'passkeys_allowed': c.settings.passkeys_allowed,
            **(extra or {}),
        },
        status_code=status_code,
    )


@router.get('/settings')
def settings_page(
    request: Request, user: User = Depends(current_user), db: Session = DB
) -> Response:
    return _page(request, db, user)


@router.post('/settings/passkeys/options')
async def add_passkey_options(
    request: Request, user: User = Depends(current_user), db: Session = DB
) -> Response:
    limit(request, 'register_ip', client_ip(request))
    c = ctx(request)
    return JSONResponse(
        svc.registration_options(
            db,
            c.settings,
            c.clock,
            user=user,
            username=user.username,
            display_name=user.display_name,
            user_handle=user.user_handle,
            link=None,
        )
    )


@router.post('/settings/passkeys/verify')
async def add_passkey_verify(
    request: Request, user: User = Depends(current_user), db: Session = DB
) -> Response:
    body = await _json(request)
    c = ctx(request)
    try:
        challenge, verified = svc.finish_registration(
            db,
            c.settings,
            c.clock,
            str(body.get('handle', '')),
            body.get('credential') or {},
            str(body.get('name') or ''),
        )
    except svc.AuthError as exc:
        return JSONResponse({'error': str(exc)}, status_code=400)
    if challenge.user_id != user.id or challenge.setup_link_id is not None:
        return JSONResponse({'error': 'That sign-in attempt expired. Try again.'}, status_code=400)
    transports = (body.get('credential') or {}).get('response', {}).get('transports') or []
    cred = svc.save_credential(db, c.clock, user, verified, str(body.get('name') or ''), transports)
    audit.record(
        db,
        c.clock,
        'passkey.registered',
        actor_id=user.id,
        ip=client_ip(request),
        via='settings',
        name=cred.name,
    )
    return JSONResponse({'ok': True, 'redirect': '/settings'})


def _own_cred(db: Session, user: User, cred_id: int) -> WebAuthnCredential | None:
    cred = db.get(WebAuthnCredential, cred_id) if in_range(cred_id) else None
    return cred if cred is not None and cred.user_id == user.id else None


@router.post('/settings/passkeys/{cred_id}/rename')
def rename_passkey(
    cred_id: int,
    request: Request,
    name: str = Form(...),
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    c = ctx(request)
    cred = _own_cred(db, user, cred_id)
    if cred is None:
        return _page(request, db, user, {'error': 'Passkey not found'}, 404)
    old, cred.name = cred.name, (name.strip() or 'Passkey')[:80]
    audit.record(
        db,
        c.clock,
        'passkey.renamed',
        actor_id=user.id,
        ip=client_ip(request),
        old=old,
        new=cred.name,
    )
    return redirect('/settings', flash='Passkey renamed', request=request)


@router.post('/settings/passkeys/{cred_id}/revoke')
def revoke_passkey(
    cred_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    c = ctx(request)
    cred = _own_cred(db, user, cred_id)
    if cred is None:
        return _page(request, db, user, {'error': 'Passkey not found'}, 404)
    if passkey_count(db, user) <= 1:
        return _page(
            request,
            db,
            user,
            {
                'error': 'That is your last passkey and you have no other way to sign in. '
                'Add another passkey first.'
            },
            400,
        )
    db.delete(cred)
    audit.record(
        db, c.clock, 'passkey.revoked', actor_id=user.id, ip=client_ip(request), name=cred.name
    )
    return redirect('/settings', flash='Passkey revoked', request=request)


@router.post('/settings/forecast')
def forecast_prefs(
    request: Request,
    low_balance: str = Form('0'),
    forecast_months: int = Form(12),
    spending_account_id: int = Form(0),
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    prefs = settings_for(db, user)
    try:
        prefs.low_balance_cents = parse_cents(low_balance)
    except MoneyError as exc:
        return _page(request, db, user, {'error': str(exc)}, 400)
    prefs.forecast_months = min(24, max(1, forecast_months))
    if spending_account_id and in_range(spending_account_id):
        acct = db.get(Account, spending_account_id)
        prefs.spending_account_id = acct.id if acct and acct.user_id == user.id else None
    else:
        prefs.spending_account_id = None
    return redirect('/settings', flash='Forecast settings saved', request=request)


@router.post('/settings/ntfy')
def ntfy_prefs(
    request: Request,
    enabled: bool = Form(False),
    url: str = Form(''),
    topic: str = Form(''),
    token: str = Form(''),
    bill_days: int = Form(3),
    hour: int = Form(8),
    bills_due: bool = Form(False),
    overdue: bool = Form(False),
    low_balance: bool = Form(False),
    payslips: bool = Form(False),
    user: User = Depends(current_user),
    db: Session = DB,
) -> Response:
    prefs = settings_for(db, user)
    url, topic = ntfy.normalise(url, topic)
    if enabled or url:
        try:
            ntfy.validate_target(url, topic)
        except ntfy.NtfyError as exc:
            return _page(request, db, user, {'ntfy_error': str(exc)}, 400)
    prefs.ntfy_enabled = enabled
    prefs.ntfy_url = url
    prefs.ntfy_topic = topic
    if token.strip():
        prefs.ntfy_token = token.strip()
    prefs.ntfy_bill_days = min(30, max(0, bill_days))
    prefs.ntfy_hour = min(23, max(0, hour))
    prefs.ntfy_bills_due = bills_due
    prefs.ntfy_overdue = overdue
    prefs.ntfy_low_balance = low_balance
    prefs.ntfy_payslips = payslips
    return redirect('/settings', flash='Reminder settings saved', request=request)


@router.post('/settings/ntfy/clear-token')
def ntfy_clear_token(
    request: Request, user: User = Depends(current_user), db: Session = DB
) -> Response:
    settings_for(db, user).ntfy_token = ''
    return redirect('/settings', flash='ntfy token removed', request=request)


@router.post('/settings/ntfy/test')
def ntfy_test(request: Request, user: User = Depends(current_user), db: Session = DB) -> Response:
    prefs = settings_for(db, user)
    try:
        ntfy.send(prefs, 'Fiscus test', 'Reminders from Fiscus will arrive here.')
    except ntfy.NtfyError as exc:
        return _page(request, db, user, {'ntfy_error': str(exc)}, 400)
    return redirect('/settings', flash='Test notification sent', request=request)


# Backups: the whole database, so the admin's only (others get a 404).


@router.post('/settings/backups/run')
def run_backup(request: Request, admin: User = Depends(admin_user), db: Session = DB) -> Response:
    c = ctx(request)
    path = backup.nightly(c.settings, c.engine, c.clock)
    audit.record(
        db, c.clock, 'backup.created', actor_id=admin.id, ip=client_ip(request), file=path.name
    )
    return redirect('/settings#backups', flash=f'Backup written: {path.name}', request=request)


@router.post('/settings/backups/download')
def download_backup(
    request: Request, admin: User = Depends(admin_user), db: Session = DB
) -> Response:
    c = ctx(request)
    path = backup.snapshot_to_temp(c.settings, c.engine, c.clock)
    audit.record(db, c.clock, 'backup.downloaded', actor_id=admin.id, ip=client_ip(request))
    db.commit()
    return FileResponse(
        path,
        media_type='application/vnd.sqlite3',
        filename=path.name,
        background=BackgroundTask(shutil.rmtree, path.parent, ignore_errors=True),
    )
