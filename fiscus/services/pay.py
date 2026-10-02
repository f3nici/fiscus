"""What each pay schedule is expected to pay, for the forecast, calendar and tax."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from fiscus.domain.pay import WINDOW_DAYS, expected_pay
from fiscus.models import PaySchedule, Transaction, User
from fiscus.services.access import owned


def deposits(db: Session, user: User, ps: PaySchedule, today: date) -> list[tuple[date, int]]:
    """Recent deposits whose description contains the schedule's match text."""
    needle = ps.match_text.strip().lower()
    if not needle:
        return []
    rows = db.execute(
        owned(Transaction, user).where(
            Transaction.date > today - timedelta(days=WINDOW_DAYS),
            Transaction.date <= today,
            Transaction.amount_cents > 0,
            func.instr(func.lower(Transaction.description), needle) > 0,
        )
    ).scalars()
    return [(t.date, t.amount_cents) for t in rows]


def net_per_pay(db: Session, user: User, ps: PaySchedule, today: date) -> int:
    if not ps.varies:
        return ps.net_cents
    return expected_pay(
        ps.anchor_date, ps.frequency, deposits(db, user, ps, today), today, ps.net_cents
    )


def schedules(db: Session, user: User, today: date) -> list[tuple[PaySchedule, int]]:
    """Every pay schedule with the net it is expected to pay."""
    found: Sequence[PaySchedule] = (
        db.execute(owned(PaySchedule, user).order_by(PaySchedule.name)).scalars().all()
    )
    return [(ps, net_per_pay(db, user, ps, today)) for ps in found]
