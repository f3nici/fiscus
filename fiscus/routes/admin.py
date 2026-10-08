"""Admin: users, invites, audit log and backups."""

from __future__ import annotations

import shutil

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from fiscus.models import AuditLog, SetupLink, User
from fiscus.services import audit, backup
from fiscus.services import auth as auth_svc
from fiscus.services.access import in_range
from fiscus.services.users import UserError, active_admin_count, create_user, passkey_count
from fiscus.web import admin_user, client_ip, ctx, get_db, redirect, render

router = APIRouter(prefix='/admin')


def _users_page(request: Request, db: Session, extra: dict[str, object] | None = None) -> Response:
    c = ctx(request)
    users = db.execute(select(User).order_by(User.username)).scalars().all()
    now = c.clock.now().replace(tzinfo=None)
    links = (
        db.execute(
            select(SetupLink).where(
                SetupLink.used_at.is_(None),
                SetupLink.revoked_at.is_(None),
                SetupLink.expires_at > now,
                SetupLink.user_id.is_not(None),
            )
        )
        .scalars()
        .all()
    )
    pending = {link.user_id: link for link in links}
    rows = [
        {'user': u, 'passkeys': passkey_count(db, u), 'pending': pending.get(u.id)} for u in users
    ]
    return render(request, 'admin/users.html', {'rows': rows, **(extra or {})})


@router.get('/users')
def users(
    request: Request, admin: User = Depends(admin_user), db: Session = Depends(get_db)
) -> Response:
    return _users_page(request, db)


@router.post('/users')
def create(
    request: Request,
    username: str = Form(...),
    display_name: str = Form(''),
    is_admin: bool = Form(False),
    admin: User = Depends(admin_user),
    db: Session = Depends(get_db),
) -> Response:
    c = ctx(request)
    try:
        user = create_user(db, c.clock, username, display_name, is_admin=is_admin)
    except UserError as exc:
        return _users_page(request, db, {'error': str(exc)})
    audit.record(
        db,
        c.clock,
        'user.created',
        actor_id=admin.id,
        subject_id=user.id,
        ip=client_ip(request),
        admin=is_admin,
    )
    link = auth_svc.issue_link(db, c.settings, c.clock, 'onboard', user, admin)
    return _users_page(request, db, {'issued': link, 'issued_for': user})


def _target(db: Session, user_id: int) -> User:
    user = db.get(User, user_id) if in_range(user_id) else None
    if user is None:
        raise HTTPException(status_code=404)
    return user


@router.post('/users/{user_id}/invite')
def reinvite(
    user_id: int, request: Request, admin: User = Depends(admin_user), db: Session = Depends(get_db)
) -> Response:
    c = ctx(request)
    user = _target(db, user_id)
    if not user.is_active:
        return _users_page(request, db, {'error': 'Enable the account before sending a link.'})
    purpose = 'recovery' if passkey_count(db, user) else 'onboard'
    link = auth_svc.issue_link(db, c.settings, c.clock, purpose, user, admin)
    return _users_page(request, db, {'issued': link, 'issued_for': user})


@router.post('/users/{user_id}/revoke-link')
def revoke_link(
    user_id: int, request: Request, admin: User = Depends(admin_user), db: Session = Depends(get_db)
) -> Response:
    c = ctx(request)
    now = c.clock.now().replace(tzinfo=None)
    for link in db.execute(
        select(SetupLink).where(
            SetupLink.user_id == user_id,
            SetupLink.used_at.is_(None),
            SetupLink.revoked_at.is_(None),
        )
    ).scalars():
        link.revoked_at = now
    audit.record(
        db,
        c.clock,
        'setup_link.revoked',
        actor_id=admin.id,
        subject_id=user_id,
        ip=client_ip(request),
    )
    return redirect('/admin/users', flash='Setup link revoked', request=request)


@router.post('/users/{user_id}/active')
def set_active(
    user_id: int,
    request: Request,
    active: bool = Form(...),
    admin: User = Depends(admin_user),
    db: Session = Depends(get_db),
) -> Response:
    c = ctx(request)
    user = _target(db, user_id)
    if not active and user.is_admin and user.is_active and active_admin_count(db) <= 1:
        return _users_page(request, db, {'error': 'You cannot disable the last admin.'})
    user.is_active = active
    if not active:
        auth_svc.end_sessions(db, user.id)
        now = c.clock.now().replace(tzinfo=None)
        for link in db.execute(
            select(SetupLink).where(SetupLink.user_id == user.id, SetupLink.used_at.is_(None))
        ).scalars():
            link.revoked_at = now
    audit.record(
        db,
        c.clock,
        'user.enabled' if active else 'user.disabled',
        actor_id=admin.id,
        subject_id=user.id,
        ip=client_ip(request),
    )
    return redirect(
        '/admin/users',
        flash=f'{user.username} {"enabled" if active else "disabled"}',
        request=request,
    )


@router.post('/users/{user_id}/password')
def set_password_allowed(
    user_id: int,
    request: Request,
    enabled: bool = Form(...),
    admin: User = Depends(admin_user),
    db: Session = Depends(get_db),
) -> Response:
    c = ctx(request)
    user = _target(db, user_id)
    if not enabled and passkey_count(db, user) == 0 and user.password_hash:
        return _users_page(
            request,
            db,
            {
                'error': f'{user.username} has no passkey, so turning off their password would '
                'lock them out. Send them a setup link first.'
            },
        )
    user.password_enabled = enabled
    if not enabled:
        user.password_hash = None
    audit.record(
        db,
        c.clock,
        'password.allowed' if enabled else 'password.disallowed',
        actor_id=admin.id,
        subject_id=user.id,
        ip=client_ip(request),
    )
    return redirect('/admin/users', flash='Password sign-in updated', request=request)


@router.post('/users/{user_id}/admin')
def set_admin(
    user_id: int,
    request: Request,
    is_admin: bool = Form(...),
    admin: User = Depends(admin_user),
    db: Session = Depends(get_db),
) -> Response:
    c = ctx(request)
    user = _target(db, user_id)
    if not is_admin and user.is_admin and active_admin_count(db) <= 1:
        return _users_page(request, db, {'error': 'There must be at least one admin.'})
    user.is_admin = is_admin
    audit.record(
        db,
        c.clock,
        'user.admin_granted' if is_admin else 'user.admin_removed',
        actor_id=admin.id,
        subject_id=user.id,
        ip=client_ip(request),
    )
    return redirect('/admin/users', flash='Role updated', request=request)


@router.get('/audit')
def audit_log(
    request: Request,
    page: int = 1,
    admin: User = Depends(admin_user),
    db: Session = Depends(get_db),
) -> Response:
    page = max(page, 1)
    rows = (
        db.execute(
            select(AuditLog).order_by(AuditLog.id.desc()).offset((page - 1) * 100).limit(101)
        )
        .scalars()
        .all()
    )
    names = {u.id: u.username for u in db.execute(select(User)).scalars()}
    return render(
        request,
        'admin/audit.html',
        {'rows': rows[:100], 'names': names, 'page': page, 'more': len(rows) > 100},
    )


@router.get('/backups')
def backups(request: Request, admin: User = Depends(admin_user)) -> Response:
    c = ctx(request)
    return render(
        request,
        'admin/backups.html',
        {'files': backup.list_backups(c.settings), 'retention': c.settings.backup_retention_days},
    )


@router.post('/backups/run')
def run_backup(
    request: Request, admin: User = Depends(admin_user), db: Session = Depends(get_db)
) -> Response:
    c = ctx(request)
    path = backup.nightly(c.settings, c.engine, c.clock)
    audit.record(
        db, c.clock, 'backup.created', actor_id=admin.id, ip=client_ip(request), file=path.name
    )
    return redirect('/admin/backups', flash=f'Backup written: {path.name}', request=request)


@router.post('/backups/download')
def download_backup(
    request: Request, admin: User = Depends(admin_user), db: Session = Depends(get_db)
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
