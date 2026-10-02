"""Append-only audit log of logins and credential and account changes."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from fiscus.clock import Clock, utcnow_naive
from fiscus.models import AuditLog


def record(
    db: Session,
    clock: Clock,
    action: str,
    *,
    actor_id: int | None = None,
    subject_id: int | None = None,
    ip: str = '',
    **detail: Any,
) -> None:
    db.add(
        AuditLog(
            at=utcnow_naive(clock),
            actor_id=actor_id,
            subject_id=subject_id if subject_id is not None else actor_id,
            action=action,
            ip=ip[:64],
            detail=detail,
        )
    )
