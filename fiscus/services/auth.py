"""Sessions, setup links, bootstrap and the WebAuthn ceremonies."""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url, parse_authentication_credential_json
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from fiscus.clock import Clock, utcnow_naive
from fiscus.config import Settings
from fiscus.models import (
    AppState,
    AuthChallenge,
    AuthSession,
    SetupLink,
    User,
    WebAuthnCredential,
)
from fiscus.security.tokens import new_token, token_hash
from fiscus.services import audit
from fiscus.services.users import create_user

log = logging.getLogger('fiscus.auth')
CHALLENGE_TTL = timedelta(minutes=5)


class AuthError(Exception):
    """Fails a ceremony with a message safe to show."""


# ------------------------------------------------------------------ sessions


def start_session(
    db: Session, settings: Settings, clock: Clock, user: User, method: str, ip: str, ua: str
) -> str:
    token = new_token()
    now = utcnow_naive(clock)
    db.add(
        AuthSession(
            token_hash=token_hash(token),
            user_id=user.id,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(days=settings.session_idle_days),
            absolute_expires_at=now + timedelta(days=settings.session_absolute_days),
            method=method,
            ip=ip[:64],
            user_agent=ua[:255],
        )
    )
    audit.record(db, clock, 'login', actor_id=user.id, ip=ip, method=method)
    return token


def end_sessions(db: Session, user: User, *, keep: AuthSession | None = None) -> int:
    """Sign `user` out everywhere but the session `keep` (D56). Returns how many ended."""
    q = select(AuthSession).where(AuthSession.user_id == user.id)
    if keep is not None:
        q = q.where(AuthSession.id != keep.id)
    rows = list(db.execute(q).scalars())
    for row in rows:
        db.delete(row)
    db.flush()
    return len(rows)


# --------------------------------------------------------------- setup links


@dataclass(frozen=True)
class IssuedLink:
    token: str
    url: str
    expires_at: datetime


def issue_link(
    db: Session,
    settings: Settings,
    clock: Clock,
    purpose: str,
    user: User | None,
    created_by: User | None,
    hours: int | None = None,
) -> IssuedLink:
    now = utcnow_naive(clock)
    if user is not None:
        # One live link per person: a new one revokes the old.
        db.execute(
            update(SetupLink)
            .where(
                SetupLink.user_id == user.id,
                SetupLink.used_at.is_(None),
                SetupLink.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
    token = new_token()
    ttl = hours if hours is not None else settings.setup_link_hours
    expires = now + timedelta(hours=ttl)
    db.add(
        SetupLink(
            token_hash=token_hash(token),
            purpose=purpose,
            user_id=user.id if user else None,
            created_by=created_by.id if created_by else None,
            created_at=now,
            expires_at=expires,
        )
    )
    audit.record(
        db,
        clock,
        f'setup_link.{purpose}',
        actor_id=created_by.id if created_by else None,
        subject_id=user.id if user else None,
        expires=expires.isoformat(),
    )
    return IssuedLink(token, f'{settings.base_url}/setup/{token}', expires)


def find_link(db: Session, clock: Clock, token: str) -> SetupLink | None:
    link = db.execute(
        select(SetupLink).where(SetupLink.token_hash == token_hash(token))
    ).scalar_one_or_none()
    if link is None or link.used_at or link.revoked_at:
        return None
    if link.expires_at <= utcnow_naive(clock):
        return None
    if link.purpose != 'bootstrap':
        user = db.get(User, link.user_id) if link.user_id else None
        if user is None or not user.is_active:
            return None
    elif db.scalar(select(func.count()).select_from(User)):
        return None
    return link


def ensure_bootstrap(db: Session, settings: Settings, clock: Clock) -> IssuedLink | None:
    """With no users at all, mint a fresh bootstrap link and log it."""
    if db.scalar(select(func.count()).select_from(User)):
        db.execute(delete(SetupLink).where(SetupLink.purpose == 'bootstrap'))
        return None
    db.execute(delete(SetupLink).where(SetupLink.purpose == 'bootstrap'))
    link = issue_link(db, settings, clock, 'bootstrap', None, None, settings.bootstrap_hours)
    db.merge(AppState(key='bootstrap_issued', value=link.expires_at.isoformat()))
    return link


# ------------------------------------------------------------------ webauthn


def _require_passkeys(settings: Settings) -> None:
    if not settings.passkeys_allowed:
        raise HTTPException(
            status_code=503,
            detail='Passkeys need HTTPS. Set FISCUS_ORIGIN to your https:// address.',
        )


def _store_challenge(
    db: Session,
    clock: Clock,
    purpose: str,
    challenge: bytes,
    *,
    user_id: int | None = None,
    link_id: int | None = None,
    data: dict[str, Any] | None = None,
) -> str:
    now = utcnow_naive(clock)
    db.execute(delete(AuthChallenge).where(AuthChallenge.expires_at <= now))
    handle = new_token(24)
    db.add(
        AuthChallenge(
            handle=handle,
            purpose=purpose,
            challenge=challenge,
            user_id=user_id,
            setup_link_id=link_id,
            data=data or {},
            expires_at=now + CHALLENGE_TTL,
        )
    )
    return handle


def _take_challenge(db: Session, clock: Clock, handle: str, purpose: str) -> AuthChallenge:
    row = db.execute(
        select(AuthChallenge).where(AuthChallenge.handle == handle)
    ).scalar_one_or_none()
    if row is None or row.purpose != purpose:
        raise AuthError('That sign-in attempt expired. Try again.')
    db.delete(row)  # single use, whatever happens next
    if row.expires_at <= utcnow_naive(clock):
        raise AuthError('That sign-in attempt expired. Try again.')
    return row


def _transports(raw: list[str]) -> list[AuthenticatorTransport]:
    out = []
    for t in raw:
        try:
            out.append(AuthenticatorTransport(t))
        except ValueError:
            continue
    return out


def registration_options(
    db: Session,
    settings: Settings,
    clock: Clock,
    *,
    user: User | None,
    username: str,
    display_name: str,
    user_handle: bytes,
    link: SetupLink | None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _require_passkeys(settings)
    exclude = []
    if user is not None:
        exclude = [
            PublicKeyCredentialDescriptor(id=c.credential_id, transports=_transports(c.transports))
            for c in user.credentials
        ]
    opts = generate_registration_options(
        rp_id=settings.rp_id,
        rp_name=settings.rp_name,
        user_name=username,
        user_id=user_handle,
        user_display_name=display_name,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=exclude,
        timeout=120000,
    )
    payload = dict(data or {})
    payload['user_handle'] = bytes_to_base64url(user_handle)
    handle = _store_challenge(
        db,
        clock,
        'register',
        opts.challenge,
        user_id=user.id if user else None,
        link_id=link.id if link else None,
        data=payload,
    )
    return {'handle': handle, 'options': json.loads(options_to_json(opts))}


def finish_registration(
    db: Session,
    settings: Settings,
    clock: Clock,
    handle: str,
    credential: dict[str, Any],
    name: str,
) -> tuple[AuthChallenge, Any]:
    _require_passkeys(settings)
    challenge = _take_challenge(db, clock, handle, 'register')
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge.challenge,
            expected_rp_id=settings.rp_id,
            expected_origin=settings.origin,
        )
    except (InvalidRegistrationResponse, ValueError, KeyError, TypeError) as exc:
        log.info('passkey registration refused: %s', exc)
        raise AuthError('That passkey could not be verified.') from exc
    if db.execute(
        select(WebAuthnCredential).where(WebAuthnCredential.credential_id == verified.credential_id)
    ).scalar_one_or_none():
        raise AuthError('That passkey is already registered.')
    return challenge, verified


def save_credential(
    db: Session,
    clock: Clock,
    user: User,
    verified: Any,
    name: str,
    transports: list[str],
) -> WebAuthnCredential:
    cred = WebAuthnCredential(
        user_id=user.id,
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        transports=[t for t in transports if isinstance(t, str)][:6],
        name=(name.strip() or 'Passkey')[:80],
        backed_up=bool(verified.credential_backed_up),
        created_at=utcnow_naive(clock),
    )
    db.add(cred)
    return cred


def authentication_options(
    db: Session, settings: Settings, clock: Clock, username: str | None
) -> dict[str, Any]:
    _require_passkeys(settings)
    allow: list[PublicKeyCredentialDescriptor] = []
    if username:
        user = db.execute(
            select(User).where(User.username == username.strip().lower())
        ).scalar_one_or_none()
        if user is not None and user.is_active:
            allow = [
                PublicKeyCredentialDescriptor(
                    id=c.credential_id, transports=_transports(c.transports)
                )
                for c in user.credentials
            ]
        if not allow:
            # Unknown or credential-less: answer with a plausible random id so
            # the response does not reveal whether the username exists.
            allow = [PublicKeyCredentialDescriptor(id=secrets.token_bytes(32))]
    opts = generate_authentication_options(
        rp_id=settings.rp_id,
        allow_credentials=allow,
        user_verification=UserVerificationRequirement.PREFERRED,
        timeout=120000,
    )
    handle = _store_challenge(db, clock, 'login', opts.challenge)
    return {'handle': handle, 'options': json.loads(options_to_json(opts))}


def finish_authentication(
    db: Session, settings: Settings, clock: Clock, handle: str, credential: dict[str, Any]
) -> tuple[User, WebAuthnCredential]:
    _require_passkeys(settings)
    challenge = _take_challenge(db, clock, handle, 'login')
    try:
        parsed = parse_authentication_credential_json(json.dumps(credential))
    except Exception as exc:
        raise AuthError('That passkey response was malformed.') from exc
    cred = db.execute(
        select(WebAuthnCredential).where(WebAuthnCredential.credential_id == parsed.raw_id)
    ).scalar_one_or_none()
    if cred is None:
        raise AuthError('That passkey is not registered here.')
    user = db.get(User, cred.user_id)
    if user is None or not user.is_active:
        raise AuthError('That account is disabled.')
    handle_bytes = parsed.response.user_handle
    if handle_bytes and handle_bytes != user.user_handle:
        raise AuthError('That passkey does not belong to this account.')
    try:
        verified = verify_authentication_response(
            credential=parsed,
            expected_challenge=challenge.challenge,
            expected_rp_id=settings.rp_id,
            expected_origin=settings.origin,
            credential_public_key=cred.public_key,
            credential_current_sign_count=cred.sign_count,
        )
    except (InvalidAuthenticationResponse, ValueError) as exc:
        log.info('passkey sign-in refused for user %s: %s', user.id, exc)
        raise AuthError('That passkey could not be verified.') from exc
    cred.sign_count = verified.new_sign_count
    cred.last_used_at = utcnow_naive(clock)
    cred.backed_up = bool(verified.credential_backed_up)
    return user, cred


def complete_setup(
    db: Session,
    settings: Settings,
    clock: Clock,
    link_token: str,
    handle: str,
    credential: dict[str, Any],
    name: str,
    ip: str,
) -> tuple[User, SetupLink]:
    """Register the first passkey from a setup link (bootstrap, onboard or recovery)."""
    link = find_link(db, clock, link_token)
    if link is None:
        raise AuthError('This setup link has expired or was already used.')
    challenge, verified = finish_registration(db, settings, clock, handle, credential, name)
    if challenge.setup_link_id != link.id:
        raise AuthError('That sign-in attempt expired. Try again.')
    transports = credential.get('response', {}).get('transports') or []
    if link.purpose == 'bootstrap':
        username = str(challenge.data.get('username', ''))
        display = str(challenge.data.get('display_name', ''))
        from webauthn.helpers import base64url_to_bytes

        user = create_user(
            db,
            clock,
            username,
            display,
            is_admin=True,
            user_handle=base64url_to_bytes(challenge.data['user_handle']),
        )
        db.flush()
        audit.record(db, clock, 'user.bootstrap', actor_id=user.id, ip=ip)
    else:
        found = db.get(User, link.user_id)
        if found is None:
            raise AuthError('This setup link has expired or was already used.')
        user = found
        # A recovery link is for a lost device, which may still be signed in.
        ended = end_sessions(db, user)
        if ended:
            audit.record(
                db, clock, 'sessions.ended', actor_id=user.id, ip=ip, via=link.purpose, count=ended
            )
    cred = save_credential(db, clock, user, verified, name, transports)
    link.used_at = utcnow_naive(clock)
    db.flush()
    audit.record(
        db,
        clock,
        'passkey.registered',
        actor_id=user.id,
        ip=ip,
        via=link.purpose,
        name=cred.name,
    )
    return user, link
