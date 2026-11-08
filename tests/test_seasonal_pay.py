"""Pay expecting what it paid the same month last year, not its average (D77)."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from fiscus.domain import recurrence
from fiscus.domain.pay import (
    last_twelve_months,
    same_month_per_payday,
    same_month_totals,
    spread_by_month,
)
from fiscus.models import PaySchedule, Payslip, User
from fiscus.money import round_div
from fiscus.services import budgeting, calendar_events, forecasting, pay, taxes
from tests.conftest import CsrfClient, Env
from tests.test_bills import make_account
from tests.test_irregular_pay import irregular, me

TODAY = date(2026, 10, 1)  # a Thursday, the test clock's default
THURSDAY = date(2025, 1, 2)


# ------------------------------------------------------------------ domain


def test_the_last_twelve_months_are_the_whole_months_before_this_one() -> None:
    months = last_twelve_months(TODAY)
    assert months[0] == (date(2025, 10, 1), date(2025, 10, 31))
    assert months[-1] == (date(2026, 9, 1), date(2026, 9, 30))
    assert [lo.month for lo, _hi in months] == [10, 11, 12, *range(1, 10)]
    assert last_twelve_months(date(2026, 10, 31)) == months


def test_a_payday_expects_its_share_of_the_same_month_last_year() -> None:
    def paid(on: date) -> int:
        return {12: 900_00, 1: 200_00}.get(on.month, 500_00)

    paydays = list(recurrence.occurrences(THURSDAY, 'weekly', date(2025, 10, 1), TODAY))
    slips = [(d, paid(d)) for d in paydays]
    months = same_month_per_payday(THURSDAY, 'weekly', slips, TODAY)
    assert months == {m: {12: 900_00, 1: 200_00}.get(m, 500_00) for m in range(1, 13)}
    # A payday with nothing put in counts as nothing paid: December's last
    # Thursday, of four.
    gap = [s for s in slips if s[0] != date(2025, 12, 25)]
    assert same_month_per_payday(THURSDAY, 'weekly', gap, TODAY)[12] == 900_00 * 3 // 4
    # The months start at the first payday that paid something: before it,
    # the average. Part of a month counts its paydays from then.
    late = [(d, c) for d, c in slips if d >= date(2026, 2, 12)]
    later = same_month_per_payday(THURSDAY, 'weekly', [(date(2026, 2, 5), 0), *late], TODAY)
    assert sorted(later) == list(range(2, 10)) and later[2] == 500_00
    # Fortnightly pay over five weekly paydays' worth is shared over its own.
    fortnights = [(d, c) for d, c in slips if (d - THURSDAY).days % 14 == 0]
    assert same_month_per_payday(THURSDAY, 'weekly', fortnights, TODAY)[1] < 200_00
    # Nothing paid in the twelve months: all the average.
    assert same_month_per_payday(THURSDAY, 'weekly', [], TODAY) == {}
    assert same_month_per_payday(THURSDAY, 'weekly', [(d, 0) for d in paydays], TODAY) == {}
    assert same_month_per_payday(THURSDAY, 'weekly', [(date(2025, 9, 25), 1)], TODAY) == {}
    # Paydays before the anchor are none: a schedule from August has none
    # in July, so July is left out.
    aug = same_month_per_payday(date(2026, 8, 6), 'weekly', slips, TODAY)
    assert sorted(aug) == [8, 9]


def test_irregular_pay_expects_what_the_same_month_brought_in() -> None:
    paid = [(date(2025, 10, 15), 3000_00), (date(2025, 12, 3), 5000_00)]
    paid += [(date(2025, 12, 20), 3000_00), (date(2026, 9, 30), 2500_00)]
    months = same_month_totals(date(2025, 7, 1), paid, TODAY)
    # A month with nothing in is a month that brings nothing.
    assert months == {10: 3000_00, 11: 0, 12: 8000_00} | dict.fromkeys(range(1, 9), 0) | {
        9: 2500_00
    }
    # A month it started part way through expects a typical month.
    assert 10 not in same_month_totals(date(2025, 10, 2), paid, TODAY)
    assert 11 in same_month_totals(date(2025, 10, 2), paid, TODAY)
    # A payment before the start counts from that payment.
    early = same_month_totals(date(2026, 3, 1), paid, TODAY)
    assert 10 not in early and 11 in early
    # Nothing in the twelve months: a typical month throughout.
    assert same_month_totals(date(2025, 7, 1), [], TODAY) == {}
    assert same_month_totals(date(2025, 7, 1), [(date(2025, 9, 30), 99_00)], TODAY) == {}
    # Today's month is not over, so it counts last year's, not this one.
    assert same_month_totals(date(2025, 7, 1), [(TODAY, 1_00)], TODAY) == {}


def test_each_month_comes_to_its_figure_over_its_days() -> None:
    months = {12: 8000_00, 1: 0}
    days = spread_by_month(months, 3100_00, date(2026, 11, 1), date(2027, 2, 28))

    def total(m: int) -> int:
        return sum(c for d, c in days if d.month == m)

    # November and February have none: a typical month each, over their days.
    assert (total(11), total(12), total(1), total(2)) == (3100_00, 8000_00, 0, 3100_00)
    assert len({c for d, c in days if d.month == 11}) <= 2  # even, give or take a cent
    assert spread_by_month(months, 1_00, TODAY, TODAY - timedelta(days=1)) == []


# ----------------------------------------------------------------- the app


def casual(client: CsrfClient, acct: int, sid: int | None = None, **extra: str) -> int:
    resp = client.post(
        f'/pay/schedules/{sid}' if sid else '/pay/schedules',
        data={
            'name': 'Cafe',
            'frequency': 'weekly',
            'anchor_date': THURSDAY.isoformat(),
            'net': '450',
            'account_id': str(acct),
            'seasonal': 'on',
            **extra,
        },
    )
    assert resp.status_code == 303, resp.text
    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    with ctx.sessions() as db:
        return int(
            db.execute(select(PaySchedule.id).where(PaySchedule.name == 'Cafe')).scalar_one()
        )


def a_year_of_payslips(env: Env, user: User, sid: int) -> None:
    """Weekly payslips from October last year: busy in December, quiet in
    January and a little more in October."""
    with env.db() as db:
        for d in recurrence.occurrences(THURSDAY, 'weekly', date(2025, 10, 1), TODAY):
            if d >= TODAY:
                continue
            net = {10: 600_00, 12: 900_00, 1: 200_00}.get(d.month, 500_00)
            db.add(
                Payslip(
                    user_id=user.id,
                    date=d,
                    gross_cents=net,
                    tax_withheld_cents=0,
                    net_cents=net,
                    source='Cafe',
                    schedule_id=sid,
                    payday=d,
                )
            )
        db.commit()


def test_pay_that_varies_expects_the_same_month_last_year(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    acct = make_account(a)
    # Expecting the same month last year is pay that varies, ticked or not.
    sid = casual(a, acct)
    with env.db() as db:
        ps = db.get(PaySchedule, sid)
        assert ps is not None and ps.varies and ps.seasonal
    # No payslips yet: the pay typed in throughout.
    with env.db() as db:
        [e] = pay.schedules(db, me(db, alex), TODAY)
        assert (e.net_cents, dict(e.months)) == (450_00, {})
    a_year_of_payslips(env, alex, sid)

    with env.db() as db:
        user = me(db, alex)
        [e] = pay.schedules(db, user, TODAY)
        # The average is the last thirteen weeks, all $500.
        assert e.net_cents == 500_00
        assert (e.on(date(2026, 12, 3)), e.on(date(2027, 1, 7))) == (900_00, 200_00)

        # Five Thursdays in December 2026, each a December payday of last year.
        dec = budgeting.period_starting_in(db, user, date(2026, 12, 1))
        assert budgeting.expected_pay(db, user, *dec, TODAY) == 5 * 900_00
        jan = budgeting.period_starting_in(db, user, date(2027, 1, 1))
        assert budgeting.expected_pay(db, user, *jan, TODAY) == 4 * 200_00

        flows = forecasting.build_input(db, user, TODAY, 12).flows
        paid = {f.date: f.amount_cents for f in flows if f.kind == 'pay'}
        assert paid[date(2026, 10, 8)] == 600_00
        assert paid[date(2026, 11, 5)] == 500_00
        assert paid[date(2026, 12, 31)] == 900_00
        assert paid[date(2027, 1, 28)] == 200_00

        events = calendar_events.events(db, user, date(2026, 12, 1), date(2026, 12, 31), TODAY)
        assert {e.amount_cents for e in events if e.kind == 'pay'} == {900_00}

        # The tax estimate projects the same paydays.
        projected = taxes.view(db, user, TODAY).projected.income_cents
        ps = db.get(PaySchedule, sid)
        assert ps is not None
        ps.seasonal = False
        db.flush()
        flat = taxes.view(db, user, TODAY).projected.income_cents
        # Five Thursdays left in October, five in December and four in January.
        assert projected - flat == 5 * 100_00 + 5 * 400_00 - 4 * 300_00
        db.rollback()

    page = a.get('/pay').text
    # Today is a payday, not yet in: it expects what October paid last year.
    assert 'varies, as October last year' in page and '~$600.00' in page
    assert 'expected $600.00' in page
    form = a.get(f'/pay?payday={sid}:2026-10-01').text
    assert 'value="600.00"' in form
    assert 'name="seasonal" checked' in a.get(f'/pay?edit={sid}').text

    # Turned off, the average again.
    casual(a, acct, sid, seasonal='', varies='on')
    with env.db() as db:
        user = me(db, alex)
        [e] = pay.schedules(db, user, TODAY)
        assert dict(e.months) == {} and e.on(date(2026, 12, 3)) == 500_00
        dec = budgeting.period_starting_in(db, user, date(2026, 12, 1))
        assert budgeting.expected_pay(db, user, *dec, TODAY) == 5 * 500_00
    assert 'varies, recent average' in a.get('/pay').text


def test_pay_that_does_not_vary_keeps_what_was_typed(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    acct = make_account(a)
    sid = casual(a, acct)
    a_year_of_payslips(env, alex, sid)
    with env.db() as db:
        ps = db.get(PaySchedule, sid)
        assert ps is not None
        ps.varies = False
        db.commit()
    with env.db() as db:
        [e] = pay.schedules(db, me(db, alex), TODAY)
        assert (e.net_cents, dict(e.months)) == (450_00, {})


def test_irregular_pay_expects_the_same_month_last_year(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    acct = make_account(a)
    sid = irregular(a, acct, anchor_date='2025-07-01', seasonal='on')
    with env.db() as db:
        ps = db.get(PaySchedule, sid)
        assert ps is not None and ps.seasonal and not ps.varies
        user = me(db, alex)
        # Busy in December, nothing in January, $3,000 a month otherwise.
        for m in range(12):
            on = recurrence.add_months(date(2025, 10, 15), m)
            net = {12: 8000_00, 1: 0}.get(on.month, 3000_00)
            if net:
                db.add(
                    Payslip(
                        user_id=user.id,
                        date=on,
                        gross_cents=net,
                        tax_withheld_cents=0,
                        net_cents=net,
                        source='Fenici Design',
                        schedule_id=sid,
                    )
                )
        db.commit()

    with env.db() as db:
        user = me(db, alex)
        [e] = pay.schedules(db, user, TODAY)
        months = {12: 8000_00, 1: 0} | dict.fromkeys((2, 3, 4, 5, 6, 7, 8, 9, 10, 11), 300000)
        assert dict(e.months) == months
        # The average, for a month with none, is the year's.
        assert e.net_cents == round_div(10 * 3000_00 + 8000_00, 12)

        def month(m: int, y: int = 2026) -> budgeting.Irregular:
            start, end = budgeting.period_starting_in(db, user, date(y, m, 1))
            [x] = budgeting.irregular_pay(db, user, start, end, TODAY)
            return x

        assert (month(10).expected_cents, month(12).expected_cents) == (3000_00, 8000_00)
        assert month(1, 2027).expected_cents == 0 and month(1, 2027).as_last_year
        # A month gone by still counts only what came in.
        assert month(9).expected_cents == 3000_00 and month(9).closed

        flows = forecasting.build_input(db, user, TODAY, 12).flows
        daily = [f for f in flows if f.kind == 'irregular']
        assert daily[0].date == TODAY + timedelta(days=1)

        def total(y: int, m: int) -> int:
            return sum(f.amount_cents for f in daily if (f.date.year, f.date.month) == (y, m))

        assert abs(total(2026, 12) - 8000_00) <= 1 and total(2027, 1) == 0
        assert abs(total(2027, 2) - 3000_00) <= 1

        tv = taxes.view(db, user, TODAY)
        rest = sum(c for _d, c in e.spread(TODAY + timedelta(days=1), date(2027, 6, 30)))
        assert tv.projected.income_cents == 3 * 3000_00 + rest

    page = a.get('/pay').text
    assert 'Irregular · this month, as October last year' in page and '~$3,000.00' in page
    assert 'Fenici Design: as last year, $8,000.00' in a.get('/budget?month=2026-12').text
    assert 'Fenici Design: as last year, $0.00' in a.get('/budget?month=2027-01').text
