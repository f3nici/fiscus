"""Calendar events: bills at this person's share, paydays and loan repayments.

The grid itself (month, week, day and agenda) is laid out here too, so the
template only draws what it is given.
"""

from __future__ import annotations

import calendar as pycal
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy.orm import Session

from fiscus.clock import Clock
from fiscus.domain import recurrence
from fiscus.domain.loan import repayments as loan_repayments
from fiscus.models import Loan, Payslip, User
from fiscus.services import bills, forecasting, ledger, pay
from fiscus.services.access import owned

VIEWS = ('month', 'week', 'day', 'agenda')


@dataclass
class Event:
    date: date
    kind: str  # bill | shared | pay | loan
    title: str
    amount_cents: int
    status: str  # paid | overdue | due_soon | upcoming | scheduled
    bill_id: int | None = None
    ref_id: int | None = None

    @property
    def css(self) -> str:
        return f'ev-{self.kind} ev-{self.status}'


@dataclass
class Day:
    date: date
    in_scope: bool
    is_today: bool
    events: list[Event] = field(default_factory=list)
    balance: int | None = None


@dataclass
class Grid:
    view: str
    focus: date
    title: str
    days: list[Day]
    prev: date
    next: date
    weeks: list[list[Day]]


def events(db: Session, user: User, start: date, end: date, today: date) -> list[Event]:
    out: list[Event] = []
    for occ in bills.occurrences(db, user, start, end, today):
        out.append(
            Event(
                occ.due,
                'shared' if occ.shared else 'bill',
                occ.bill.name,
                -occ.share_cents,
                occ.status,
                occ.bill.id,
            )
        )
    for ps, net in pay.schedules(db, user, today):
        for d in recurrence.occurrences(
            ps.anchor_date, ps.frequency, start, end, until=ps.end_date
        ):
            out.append(
                Event(
                    d,
                    'pay',
                    ps.name,
                    net,
                    'paid' if d <= today else 'scheduled',
                    None,
                    ps.id,
                )
            )
    # Payslips after today are one-off pays to come; earlier ones are in the
    # imported transactions already.
    for slip in db.execute(
        owned(Payslip, user).where(Payslip.date > today, Payslip.date >= start, Payslip.date <= end)
    ).scalars():
        out.append(
            Event(slip.date, 'pay', slip.source or 'Payslip', slip.net_cents, 'scheduled', None)
        )
    loans = db.execute(owned(Loan, user)).scalars().all()
    balances = ledger.balances(db, user, today) if loans else {}
    for loan in loans:
        spec = forecasting.loan_spec(db, loan)
        # Today's offset held steady, as on the loan page.
        offset = sum(max(0, balances.get(a, 0)) for a in spec.offset_account_ids)
        for day, payment in loan_repayments(spec, start, end, offset):
            out.append(
                Event(
                    day,
                    'loan',
                    f'{loan.name} repayment',
                    -payment,
                    'paid' if day <= today else 'scheduled',
                    None,
                    loan.id,
                )
            )
    order = {'pay': 0, 'shared': 1, 'bill': 1, 'loan': 2}
    out.sort(key=lambda e: (e.date, order.get(e.kind, 3), e.title))
    return out


def _range(view: str, focus: date) -> tuple[date, date, date, date, date, str]:
    """(first cell, last cell, prev focus, next focus, scope start, title)."""
    if view == 'month':
        first = focus.replace(day=1)
        last = first.replace(day=pycal.monthrange(first.year, first.month)[1])
        start = first - timedelta(days=first.weekday())
        end = last + timedelta(days=6 - last.weekday())
        return (
            start,
            end,
            recurrence.add_months(first, -1),
            recurrence.add_months(first, 1),
            first,
            focus.strftime('%B %Y'),
        )
    if view == 'week':
        start = focus - timedelta(days=focus.weekday())
        end = start + timedelta(days=6)
        if start.month == end.month:
            title = f'{start.day} to {end.day} {end.strftime("%B %Y")}'
        else:
            title = f'{start.day} {start.strftime("%b")} to {end.day} {end.strftime("%b %Y")}'
        return start, end, start - timedelta(days=7), start + timedelta(days=7), start, title
    if view == 'day':
        title = f'{focus.strftime("%A")} {focus.day} {focus.strftime("%B %Y")}'
        return focus, focus, focus - timedelta(days=1), focus + timedelta(days=1), focus, title
    # agenda: the focus day and the 30 after it
    end = focus + timedelta(days=30)
    title = f'From {focus.day} {focus.strftime("%B %Y")}'
    return focus, end, focus - timedelta(days=31), focus + timedelta(days=31), focus, title


def grid(db: Session, clock: Clock, user: User, view: str, focus: date) -> Grid:
    if view not in VIEWS:
        view = 'month'
    today = clock.today()
    start, end, prev, nxt, scope_start, title = _range(view, focus)
    evs = events(db, user, start, end, today)
    by_day: dict[date, list[Event]] = {}
    for e in evs:
        by_day.setdefault(e.date, []).append(e)
    forecast = None
    if end >= today:
        forecast = forecasting.base(db, clock, user)
    days = []
    d = start
    while d <= end:
        in_scope = d.month == scope_start.month if view == 'month' else True
        bal = forecast.balance_on(d) if forecast is not None and d >= today else None
        days.append(Day(d, in_scope, d == today, by_day.get(d, []), bal))
        d += timedelta(days=1)
    weeks = [days[i : i + 7] for i in range(0, len(days), 7)] if view in ('month', 'week') else []
    if view == 'agenda':
        days = [day for day in days if day.events or day.is_today]
    return Grid(view, focus, title, days, prev, nxt, weeks)
