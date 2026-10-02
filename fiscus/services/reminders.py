"""Which ntfy reminders are due for a user, and sending them once each."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fiscus.clock import Clock, utcnow_naive
from fiscus.models import NotificationLog, User
from fiscus.money import fmt
from fiscus.services import bills, forecasting, ntfy
from fiscus.services.users import settings_for

log = logging.getLogger('fiscus.reminders')


@dataclass(frozen=True)
class Reminder:
    kind: str
    key: str
    title: str
    message: str
    priority: str = 'default'
    tags: str = ''


def due_reminders(db: Session, clock: Clock, user: User) -> list[Reminder]:
    prefs = settings_for(db, user)
    today = clock.today()
    out: list[Reminder] = []
    if prefs.ntfy_bills_due:
        horizon = today + timedelta(days=prefs.ntfy_bill_days)
        for occ in bills.occurrences(db, user, today, horizon, today):
            if occ.payment is None:
                when = 'today' if occ.due == today else f'on {occ.due.strftime("%a %d %b")}'
                out.append(
                    Reminder(
                        'bill_due',
                        f'{occ.bill.id}:{occ.due.isoformat()}',
                        f'{occ.bill.name} due {when}',
                        f'{occ.bill.name}: {fmt(occ.share_cents)} due {when}.',
                        tags='calendar',
                    )
                )
    if prefs.ntfy_overdue:
        for occ in bills.overdue(db, user, today, lookback_days=60):
            out.append(
                Reminder(
                    'overdue',
                    f'{occ.bill.id}:{occ.due.isoformat()}',
                    f'{occ.bill.name} is overdue',
                    f'{occ.bill.name}: {fmt(occ.share_cents)} was due {occ.due.strftime("%d %b")} '
                    'and has not been matched to a payment.',
                    priority='high',
                    tags='warning',
                )
            )
    if prefs.ntfy_low_balance:
        result = forecasting.base(db, clock, user)
        low = result.low_balance
        if low is not None:
            out.append(
                Reminder(
                    'low_balance',
                    f'{low["account_id"]}:{low["date"].isoformat()}',
                    'Low balance ahead',
                    f'{low["account"]} is projected to reach {fmt(low["balance"])} on '
                    f'{low["date"].strftime("%d %b %Y")}.',
                    priority='high',
                    tags='chart_with_downwards_trend',
                )
            )
    return out


def send_for_user(db: Session, clock: Clock, user: User, *, force_hour: bool = False) -> int:
    prefs = settings_for(db, user)
    if not prefs.ntfy_enabled or not prefs.ntfy_url or not prefs.ntfy_topic:
        return 0
    if not force_hour and clock.local_now().hour != prefs.ntfy_hour:
        return 0
    sent = 0
    for r in due_reminders(db, clock, user):
        exists = db.execute(
            select(NotificationLog).where(
                NotificationLog.user_id == user.id,
                NotificationLog.kind == r.kind,
                NotificationLog.key == r.key,
            )
        ).scalar_one_or_none()
        if exists is not None:
            continue
        try:
            ntfy.send(prefs, r.title, r.message, priority=r.priority, tags=r.tags)
        except ntfy.NtfyError as exc:
            log.warning('ntfy reminder for user %s failed: %s', user.id, exc)
            return sent
        db.add(
            NotificationLog(user_id=user.id, kind=r.kind, key=r.key, sent_at=utcnow_naive(clock))
        )
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            continue
        sent += 1
    return sent
