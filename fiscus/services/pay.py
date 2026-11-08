"""What each pay schedule is expected to pay, for the forecast, calendar and tax.

`items` is the one list of pay: each schedule payday, replaced by its payslip
when one records it, the payments of irregular pay (D74), and one-off
payslips. The forecast, calendar, budget and tax all read it, so they agree
on what was and will be paid. Irregular pay still to come has no dates, so
each of them takes its typical month (`typical_month`) as it needs it.

Pay that varies, or irregular pay, can expect what it paid the same month
last year instead of its average (D77): `Expected.on` gives a payday, or a
month, its figure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from fiscus.clock import Clock
from fiscus.domain import pay as pay_domain
from fiscus.domain import recurrence
from fiscus.domain.pay import (
    ASK_DAYS,
    LINK_DAYS,
    IrregularPay,
    OpenPayday,
    expected_pay,
    window_days,
)
from fiscus.models import PaySchedule, Payslip, Transaction, User
from fiscus.services import ledger
from fiscus.services.access import owned


def deposits(db: Session, user: User, ps: PaySchedule, today: date) -> list[tuple[date, int]]:
    """Recent pay for a varying schedule: its payslips, else deposits matching its text."""
    since = today - timedelta(days=window_days(ps.frequency))
    slips = db.execute(
        owned(Payslip, user).where(
            Payslip.schedule_id == ps.id,
            Payslip.date > since,
            Payslip.date <= today,
        )
    ).scalars()
    found = [(s.date, s.net_cents) for s in slips]
    if found:
        return found
    needle = ps.match_text.strip().lower()
    if not needle:
        return []
    rows = db.execute(
        owned(Transaction, user).where(
            Transaction.date > since,
            Transaction.date <= today,
            Transaction.amount_cents > 0,
            Transaction.payslip_id.is_(None),
            Transaction.budget_spend_id.is_(None),
            Transaction.goal_move_id.is_(None),
            ~Transaction.dedup_hash.startswith(ledger.KEPT),
            func.instr(func.lower(Transaction.description), needle) > 0,
        )
    ).scalars()
    return [(t.date, t.amount_cents) for t in rows]


def follow(db: Session, clock: Clock, user: User, slip: Payslip) -> None:
    """Keep the payslip's net in the balance of the account it is paid into
    (its own, else its schedule's), on its date (D42)."""
    ps = db.get(PaySchedule, slip.schedule_id) if slip.schedule_id else None
    account_id = slip.account_id
    if account_id is None and ps is not None:
        account_id = ps.account_id
    ledger.follow(
        db,
        clock,
        user,
        slip,
        account_id=account_id,
        day=slip.date,
        cents=slip.net_cents,
        description=ps.name if ps else slip.source or 'Payslip',
    )


def is_irregular(ps: PaySchedule) -> bool:
    """Paid whenever it comes, with no paydays (D74)."""
    return ps.frequency == recurrence.IRREGULAR


def payments(db: Session, user: User, ps: PaySchedule, start: date, end: date) -> list[Payslip]:
    """An irregular pay's payments dated in [start, end], in date order."""
    return list(
        db.execute(
            owned(Payslip, user)
            .where(Payslip.schedule_id == ps.id, Payslip.date >= start, Payslip.date <= end)
            .order_by(Payslip.date, Payslip.id)
        ).scalars()
    )


def _last_year(db: Session, user: User, ps: PaySchedule, today: date) -> list[tuple[date, int]]:
    since = today - timedelta(days=pay_domain.LONG_WINDOW_DAYS - 1)
    return [(s.date, s.net_cents) for s in payments(db, user, ps, since, today)]


def typical_month(db: Session, user: User, ps: PaySchedule, today: date) -> int:
    """What an irregular pay brings in a typical month: the last year's
    average, or the month typed in until it has paid anything (D74)."""
    paid = _last_year(db, user, ps, today)
    return pay_domain.typical_month(ps.anchor_date, paid, today, ps.net_cents)


def averaged(db: Session, user: User, ps: PaySchedule, today: date) -> bool:
    """Whether an irregular pay's typical month is its average yet, not the
    month typed in."""
    return any(c > 0 for _d, c in _last_year(db, user, ps, today))


def runs(ps: PaySchedule, start: date, end: date) -> bool:
    """Whether a pay schedule runs at some point in [start, end]."""
    return ps.anchor_date <= end and (ps.end_date is None or ps.end_date >= start)


def irregular(db: Session, user: User) -> list[IrregularPay]:
    """Irregular pay a payslip can go on, with the employers on its payments."""
    found = [
        ps
        for ps in db.execute(owned(PaySchedule, user).order_by(PaySchedule.name)).scalars()
        if is_irregular(ps)
    ]
    employers: dict[int, set[str]] = {}
    if found:
        for slip in db.execute(
            owned(Payslip, user).where(
                Payslip.schedule_id.in_([ps.id for ps in found]), Payslip.source != ''
            )
        ).scalars():
            if slip.schedule_id is not None:
                employers.setdefault(slip.schedule_id, set()).add(slip.source)
    return [IrregularPay(ps.id, ps.name, tuple(sorted(employers.get(ps.id, ())))) for ps in found]


def net_per_pay(db: Session, user: User, ps: PaySchedule, today: date) -> int:
    """What a payday is expected to pay; for irregular pay, a typical month.
    The average, whether or not it expects the same month last year."""
    if is_irregular(ps):
        return typical_month(db, user, ps, today)
    if not ps.varies:
        return ps.net_cents
    return expected_pay(
        ps.anchor_date, ps.frequency, deposits(db, user, ps, today), today, ps.net_cents
    )


def seasonal(ps: PaySchedule) -> bool:
    """Whether it expects the same month last year (D77): pay that varies,
    or irregular pay, with that chosen."""
    return ps.seasonal and (ps.varies or is_irregular(ps))


def same_month(db: Session, user: User, ps: PaySchedule, today: date) -> dict[int, int]:
    """What it paid the same month last year, by month number: a payday's
    share, or irregular pay's month. Empty unless it expects that (D77)."""
    if not seasonal(ps):
        return {}
    months = pay_domain.last_twelve_months(today)
    first, last = months[0][0], months[-1][1]
    if is_irregular(ps):
        paid = [(s.date, s.net_cents) for s in payments(db, user, ps, first, last)]
        return pay_domain.same_month_totals(ps.anchor_date, paid, today)
    slips = db.execute(
        owned(Payslip, user).where(
            Payslip.schedule_id == ps.id, Payslip.payday >= first, Payslip.payday <= last
        )
    ).scalars()
    return pay_domain.same_month_per_payday(
        ps.anchor_date,
        ps.frequency,
        [(s.payday, s.net_cents) for s in slips if s.payday is not None],
        today,
        ps.end_date,
    )


@dataclass(frozen=True)
class Expected:
    """What a pay schedule is expected to pay: each payday, or a month of
    irregular pay."""

    schedule: PaySchedule
    net_cents: int  # the average (or as typed): per payday, or a typical month
    # By month number, the same month last year, when it expects that (D77).
    months: Mapping[int, int] = field(default_factory=dict)

    def on(self, day: date) -> int:
        """A payday on `day`, or irregular pay's month that `day` is in."""
        return self.months.get(day.month, self.net_cents)

    def as_last_year(self, day: date) -> bool:
        """Whether `day`'s figure is the same month last year's."""
        return day.month in self.months

    def spread(self, first: date, last: date) -> list[tuple[date, int]]:
        """Irregular pay over each day from `first` to `last`: a typical month,
        or each month as it went last year, a whole month coming to it."""
        if self.months:
            return pay_domain.spread_by_month(self.months, self.net_cents, first, last)
        return pay_domain.spread(self.net_cents, first, last)


def expected(db: Session, user: User, ps: PaySchedule, today: date) -> Expected:
    return Expected(ps, net_per_pay(db, user, ps, today), same_month(db, user, ps, today))


def schedules(db: Session, user: User, today: date) -> list[Expected]:
    """Every pay schedule with what it is expected to pay."""
    found: Sequence[PaySchedule] = (
        db.execute(owned(PaySchedule, user).order_by(PaySchedule.name)).scalars().all()
    )
    return [expected(db, user, ps, today) for ps in found]


@dataclass(frozen=True)
class PayItem:
    when: date  # when it is paid: the payslip's date, else the payday
    net_cents: int
    schedule: PaySchedule | None
    # The payday's expected net (D77), or the payslip's for a one-off.
    expected_cents: int
    slip: Payslip | None = None
    payday: date | None = None

    @property
    def label(self) -> str:
        if self.schedule is not None:
            return self.schedule.name
        return (self.slip.source if self.slip else '') or 'Payslip'

    @property
    def account_id(self) -> int | None:
        if self.slip is not None and self.slip.account_id is not None:
            return self.slip.account_id
        return self.schedule.account_id if self.schedule else None


def items(
    db: Session,
    user: User,
    start: date,
    end: date,
    today: date,
    expected: list[Expected] | None = None,
) -> list[PayItem]:
    """Pay in [start, end] by the date it is paid, in date order: paydays
    (their payslip's figures once in), irregular pay's payments and one-offs."""
    expected = schedules(db, user, today) if expected is None else expected
    paid_whenever = {e.schedule.id: e.schedule for e in expected if is_irregular(e.schedule)}
    pad = timedelta(days=LINK_DAYS)
    slips = db.execute(
        owned(Payslip, user).where(
            or_(
                and_(Payslip.date >= start, Payslip.date <= end),
                and_(Payslip.payday >= start - pad, Payslip.payday <= end + pad),
            )
        )
    ).scalars()
    linked: dict[tuple[int, date], Payslip] = {}
    loose: list[Payslip] = []
    for s in slips:
        if s.schedule_id is not None and s.payday is not None:
            linked[(s.schedule_id, s.payday)] = s
        else:
            loose.append(s)
    out: list[PayItem] = []
    for e in expected:
        ps = e.schedule
        for d in recurrence.occurrences(
            ps.anchor_date, ps.frequency, start - pad, end + pad, until=ps.end_date
        ):
            slip = linked.pop((ps.id, d), None)
            when = slip.date if slip else d
            if start <= when <= end:
                net = e.on(d)
                out.append(PayItem(when, slip.net_cents if slip else net, ps, net, slip, d))
    # Irregular pay's payments, one-offs, and payslips whose payday the
    # schedule no longer has.
    for s in [*loose, *linked.values()]:
        if start <= s.date <= end:
            whenever = paid_whenever.get(s.schedule_id) if s.schedule_id else None
            out.append(PayItem(s.date, s.net_cents, whenever, s.net_cents, s))
    out.sort(key=lambda i: (i.when, i.label))
    return out


def open_paydays(
    db: Session, user: User, start: date, end: date, except_slip: int | None = None
) -> list[OpenPayday]:
    """Schedule paydays in [start, end] that no payslip records yet."""
    found = db.execute(owned(PaySchedule, user).order_by(PaySchedule.name)).scalars().all()
    taken = {
        (s.schedule_id, s.payday)
        for s in db.execute(
            owned(Payslip, user).where(
                Payslip.schedule_id.is_not(None), Payslip.payday >= start, Payslip.payday <= end
            )
        ).scalars()
        if s.id != except_slip
    }
    employers: dict[int, set[str]] = {}
    for slip in db.execute(
        owned(Payslip, user).where(Payslip.schedule_id.is_not(None), Payslip.source != '')
    ).scalars():
        if slip.schedule_id is not None and slip.id != except_slip:
            employers.setdefault(slip.schedule_id, set()).add(slip.source)
    out = [
        OpenPayday(ps.id, d, ps.name, tuple(sorted(employers.get(ps.id, ()))))
        for ps in found
        for d in recurrence.occurrences(ps.anchor_date, ps.frequency, start, end, until=ps.end_date)
        if (ps.id, d) not in taken
    ]
    out.sort(key=lambda o: (o.payday, o.name))
    return out


def to_put_in(db: Session, user: User, today: date) -> list[OpenPayday]:
    """Recent paydays, today included, still without a payslip: newest first."""
    return open_paydays(db, user, today - timedelta(days=ASK_DAYS), today)[::-1]


def is_payday(ps: PaySchedule, day: date) -> bool:
    return any(recurrence.occurrences(ps.anchor_date, ps.frequency, day, day, until=ps.end_date))
