"""Calendar events: bills, paydays and loan repayments.

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
from fiscus.models import Loan, SquareInvoice, User
from fiscus.services import bills, forecasting, ledger, pay
from fiscus.services.access import owned

VIEWS = ('month', 'week', 'day', 'agenda')


@dataclass
class Event:
    date: date
    kind: str  # bill | pay | loan
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
    # Agenda from today: bills still unpaid from before it, which the
    # balances count as going out tomorrow.
    overdue: list[Event] = field(default_factory=list)


def events(db: Session, user: User, start: date, end: date, today: date) -> list[Event]:
    out: list[Event] = []
    for occ in bills.occurrences(db, user, start, end, today):
        out.append(
            Event(
                occ.due,
                'bill',
                occ.bill.name,
                -occ.bill.amount_cents,
                occ.status,
                occ.bill.id,
            )
        )
    # Paydays, with their payslip's net once one is in, and one-off payslips.
    # Past one-offs from before paydays were recorded may repeat a payday, so
    # only one-offs still to come are shown.
    for item in pay.items(db, user, start, end, today):
        if item.schedule is None and item.when <= today:
            continue
        out.append(
            Event(
                item.when,
                'pay',
                item.label,
                item.net_cents,
                'paid' if item.when <= today else 'scheduled',
                None,
                item.schedule.id if item.schedule else None,
            )
        )
    # Unpaid Square invoices on their due date. Not in the forecast: business
    # income already comes in there through the 90-day average.
    for inv in db.execute(
        owned(SquareInvoice, user).where(
            SquareInvoice.due_date >= start, SquareInvoice.due_date <= end
        )
    ).scalars():
        title = f'Invoice {inv.number}' + (f' · {inv.customer}' if inv.customer else '')
        status = 'overdue' if inv.due_date < today else 'scheduled'
        out.append(Event(inv.due_date, 'pay', title, inv.amount_due_cents, status, None))
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
    order = {'pay': 0, 'bill': 1, 'loan': 2}
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
    overdue: list[Event] = []
    if view == 'agenda':
        days = [day for day in days if day.events or day.is_today]
        # Bills overdue from before the agenda's first day (later ones are on
        # their own days), when the agenda reaches today.
        if start <= today <= end:
            overdue = [
                Event(
                    o.due,
                    'bill',
                    f'{o.bill.name}, due {o.due.day} {o.due.strftime("%b")}',
                    -o.bill.amount_cents,
                    'overdue',
                    o.bill.id,
                )
                for o in bills.overdue(db, user, today)
                if o.due < start
            ]
    return Grid(view, focus, title, days, prev, nxt, weeks, overdue)
