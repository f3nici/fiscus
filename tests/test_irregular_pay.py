"""Irregular pay (D74): pay with no paydays, such as pay from one's own business."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from fiscus.domain import recurrence
from fiscus.domain.forecast import Flow, ForecastInput, IrregularSpread, apply_scenario, run
from fiscus.domain.pay import IrregularPay, match_irregular, spread, typical_month
from fiscus.models import PaySchedule, Payslip, Scenario, User
from fiscus.money import fmt, round_div
from fiscus.services import budgeting, calendar_events, forecasting, ledger, pay, taxes
from fiscus.services.users import settings_for
from tests.conftest import CsrfClient, Env
from tests.test_bills import make_account
from tests.test_paydays import simple_slip, slips

TODAY = date(2026, 10, 1)  # a Thursday, the test clock's default


def irregular(client: CsrfClient, acct: int, name: str = 'Fenici Design', **extra: str) -> int:
    resp = client.post(
        '/pay/schedules',
        data={
            'name': name,
            'frequency': 'irregular',
            'anchor_date': '2026-07-01',
            'net': '4000',
            'account_id': str(acct),
            **extra,
        },
    )
    assert resp.status_code == 303, resp.text
    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    with ctx.sessions() as db:
        return int(db.execute(select(PaySchedule.id).where(PaySchedule.name == name)).scalar_one())


def payment(client: CsrfClient, sid: int, on: str, net: str, **extra: str) -> None:
    resp = client.post(
        '/payslips',
        data={'date': on, 'gross': '', 'tax_withheld': '', 'net': net, 'payday': f'pay:{sid}'}
        | extra,
    )
    assert resp.status_code == 303, resp.text


def me(db: Session, user: User) -> User:
    found = db.get(User, user.id)
    assert found is not None
    return found


# ------------------------------------------------------------------ domain


def test_a_typical_month_is_the_last_years_average_from_when_it_started() -> None:
    paid = [(date(2026, 7, 20), 3000_00), (date(2026, 8, 5), 6500_00), (date(2026, 9, 28), 1200_00)]
    # From 1 July to today is 93 days: the year so far, a month's share of it.
    assert typical_month(date(2026, 7, 1), paid, TODAY, 1_00) == round_div(10700_00 * 365, 93 * 12)
    # A first payment before the start date counts from that payment.
    # 20 July to today is 74 days.
    assert typical_month(date(2026, 8, 1), paid, TODAY, 1_00) == round_div(10700_00 * 365, 74 * 12)
    # Only the last year counts, and a whole year is a twelfth of it.
    old = [(date(2025, 9, 30), 99_000_00), (date(2025, 10, 2), 12_000_00)]
    assert typical_month(date(2020, 1, 1), old, TODAY, 1_00) == 1000_00
    # One payment just after the start is a month's pay, not thirty.
    assert typical_month(date(2026, 9, 29), [(TODAY, 2500_00)], TODAY, 1_00) == 2500_00
    # Nothing paid in the year: the typical month typed in.
    assert typical_month(date(2026, 7, 1), [], TODAY, 4000_00) == 4000_00
    assert typical_month(date(2026, 7, 1), old[:1], TODAY, 4000_00) == 4000_00


def test_a_month_spread_over_its_days_adds_up() -> None:
    days = spread(3650_00, date(2026, 10, 2), date(2027, 10, 1))
    assert len(days) == 365 and {c for _d, c in days} == {120_00}
    odd = spread(1000_01, date(2026, 10, 2), date(2027, 10, 1))
    assert sum(c for _d, c in odd) == 1000_01 * 12
    assert spread(1000_00, TODAY, TODAY - timedelta(days=1)) == []


def test_a_pay_change_to_irregular_pay_is_by_the_month() -> None:
    # Studio expects $3,000 a typical month; Shop has expected nothing yet.
    last = date(2027, 10, 1)
    studio = IrregularSpread(7, 1, 'Studio', TODAY + timedelta(days=1), last)
    shop = IrregularSpread(9, 1, 'Shop', date(2027, 1, 1), last)
    salary = Flow(date(2026, 10, 8), 1, 900_00, 'pay', 'Acme', 8)
    flows = [salary] + [
        Flow(d, 1, c, 'irregular', 'Studio', 7) for d, c in spread(3000_00, studio.first, last)
    ]
    inp = ForecastInput(
        today=TODAY,
        days=(last - TODAY).days,
        balances={1: 0},
        account_names={1: 'Everyday'},
        account_types={1: 'transaction'},
        flows=flows,
        irregular=[studio, shop],
    )

    def change(sid: int, on: str, **p: int | str) -> tuple[str, dict[str, object]]:
        return ('income_change', {'from_date': on, 'pay_schedule_id': sid, **p})

    def studio_days(
        changed: ForecastInput, first: date, last: date = last
    ) -> list[tuple[date, int]]:
        return [
            (f.date, f.amount_cents)
            for f in changed.flows
            if f.kind == 'irregular' and f.ref == 7 and first <= f.date <= last
        ]

    def total(days: list[tuple[date, int]]) -> int:
        return sum(c for _d, c in days)

    nov, jan = date(2026, 11, 1), date(2027, 1, 1)
    dec31 = jan - timedelta(days=1)
    # $500 less a month from January: the days before are as they were.
    cut = apply_scenario(inp, [change(7, '2027-01-01', delta_cents=-500_00)])
    assert total(studio_days(cut, jan)) == total(studio_days(inp, jan)) - total(
        spread(500_00, jan, last)
    )
    assert studio_days(cut, TODAY, dec31) == studio_days(inp, TODAY, dec31)
    assert run(cut).combined[-1] == run(inp).combined[-1] - total(spread(500_00, jan, last))
    # Pay with paydays, and the other irregular pay, are left as they are.
    assert salary in cut.flows and not [f for f in cut.flows if f.ref == 9]
    assert len(inp.flows) == len(flows)  # the input is never changed

    # A new net is its month from then, in place of the typical one.
    lower = apply_scenario(inp, [change(7, '2026-11-01', new_net_cents=2000_00)])
    assert studio_days(lower, nov) == spread(2000_00, nov, last)
    oct31 = nov - timedelta(days=1)
    assert studio_days(lower, TODAY, oct31) == studio_days(inp, TODAY, oct31)
    # A new net, and $300 less from January, whichever is listed first.
    both = [
        change(7, '2026-11-01', new_net_cents=2000_00),
        change(7, '2027-01-01', delta_cents=-300_00),
    ]
    one, other = (apply_scenario(inp, both), apply_scenario(inp, both[::-1]))
    assert one.flows == other.flows
    assert studio_days(one, nov, dec31) == studio_days(lower, nov, dec31)
    assert total(studio_days(one, jan)) == total(studio_days(lower, jan)) - total(
        spread(300_00, jan, last)
    )
    # A later new net takes over from an earlier change.
    later = [
        change(7, '2026-11-01', delta_cents=500_00),
        change(7, '2027-01-01', new_net_cents=1000_00),
    ]
    assert studio_days(apply_scenario(inp, later), jan) == spread(1000_00, jan, last)
    # A percentage scales each month from then (D79), fractions carried, so
    # the days add up as the whole would; with a new net, it is of that.
    rise = apply_scenario(inp, [change(7, '2027-01-01', percent='10')])
    assert studio_days(rise, TODAY, dec31) == studio_days(inp, TODAY, dec31)
    assert abs(total(studio_days(rise, jan)) - total(studio_days(inp, jan)) * 11 / 10) <= 1
    assert all(
        abs(c * 10 - b * 11) <= 10
        for (_d, c), (_e, b) in zip(studio_days(rise, jan), studio_days(inp, jan), strict=True)
    )
    up = apply_scenario(inp, [change(7, '2026-11-01', new_net_cents=2000_00, percent='10')])
    assert studio_days(up, nov) == spread(2200_00, nov, last)
    # After a change by, the percentage is of both.
    after = [change(7, '2026-11-01', delta_cents=500_00), change(7, '2027-01-01', percent='-50')]
    halved = apply_scenario(inp, after)
    more = [(d, c) for d, c in spread(500_00, nov, last) if d >= jan]
    expected = (total(studio_days(inp, jan)) + total(more)) / 2
    assert abs(total(studio_days(halved, jan)) - expected) <= 1
    # Irregular pay expecting nothing yet gets a month from when it starts.
    started = apply_scenario(inp, [change(9, '2026-10-01', new_net_cents=1200_00)])
    shop_days = [(f.date, f.amount_cents) for f in started.flows if f.ref == 9]
    assert shop_days == spread(1200_00, shop.first, last) and shop_days[0][0] == jan


def test_irregular_pay_has_no_dates_and_is_matched_by_name() -> None:
    assert list(recurrence.occurrences(TODAY, 'irregular', TODAY, date(2027, 1, 1))) == []
    assert recurrence.next_on_or_after(TODAY, 'irregular', TODAY) is None
    studio = IrregularPay(1, 'Fenici Design', ('Fenici Pty Ltd',))
    shop = IrregularPay(2, 'Market stall')
    assert match_irregular('FENICI DESIGN', [studio, shop]) == studio
    assert match_irregular('Fenici Pty Ltd', [studio, shop]) == studio
    assert match_irregular('Acme', [studio, shop]) is None
    assert match_irregular('', [studio, shop]) is None
    # Named by both: not guessed.
    assert match_irregular('Fenici Design market stall', [studio, shop]) is None


# ---------------------------------------------------------------- the pay page


def test_a_payment_goes_in_whenever_it_comes_and_moves_the_balance(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    acct = make_account(a)
    sid = irregular(a, acct)
    page = a.get('/pay').text
    assert 'Irregular · a typical month, as you put it' in page
    assert '~$4,000.00' in page and f'href="/pay?payday=pay:{sid}#slip-form"' in page
    # No paydays, so nothing to put in or remind about.
    assert 'Payslips to put in' not in page
    form = a.get(f'/pay?payday=pay:{sid}').text
    assert 'Payslip for Fenici Design' in form
    assert f'value="pay:{sid}" selected' in form and 'value="2026-10-01"' in form

    # Gross and tax left blank: nothing withheld, gross is net.
    resp = a.post(
        '/payslips',
        data={
            'date': '2026-09-28',
            'gross': '',
            'tax_withheld': '',
            'net': '1200',
            'source': 'Fenici Pty Ltd',
            'payday': f'pay:{sid}',
        },
    )
    assert 'Payment%20of%20Fenici%20Design%20saved' in resp.headers['set-cookie']
    [slip] = slips(env)
    assert (slip.schedule_id, slip.payday) == (sid, None)
    assert (slip.gross_cents, slip.tax_withheld_cents, slip.net_cents) == (1200_00, 0, 1200_00)
    with env.db() as db:
        assert ledger.balances(db, me(db, alex), TODAY)[acct] == 2200_00
    page = a.get('/pay').text
    assert 'Fenici Design, irregular' in page
    assert 'a typical month, the last year&#39;s average' in page
    # The next payment's form has the last one's employer.
    assert 'value="Fenici Pty Ltd"' in a.get(f'/pay?payday=pay:{sid}').text
    # Editing keeps it on the pay.
    assert f'value="pay:{sid}" selected' in a.get(f'/pay?slip={slip.id}').text

    # A payslip for a job with paydays still needs its gross.
    resp = a.post(
        '/payslips',
        data={'date': '2026-09-28', 'gross': '', 'tax_withheld': '0', 'net': '5', 'payday': ''},
    )
    assert resp.status_code == 200 and 'Gross:' in resp.text
    assert len(slips(env)) == 1


def test_auto_puts_a_payment_on_the_irregular_pay_its_employer_names(env: Env) -> None:
    a = env.login(env.user('alex'))
    sid = irregular(a, make_account(a))
    base = {'gross': '800', 'tax_withheld': '0', 'net': '800', 'payday': 'auto'}
    a.post('/payslips', data=base | {'date': '2026-09-20', 'source': 'Fenici Design Pty Ltd'})
    # A bonus from someone else stays a one-off.
    a.post('/payslips', data=base | {'date': '2026-09-21', 'source': 'Acme'})
    named, bonus = slips(env)
    assert (named.schedule_id, named.payday) == (sid, None)
    assert bonus.schedule_id is None


def test_one_offs_already_in_can_be_put_on_it(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    acct = make_account(a)
    for on, who in (('2026-08-10', 'Fenici Design'), ('2026-09-01', 'Acme'), ('2025-09-01', '')):
        a.post(
            '/payslips',
            data={'date': on, 'gross': '900', 'tax_withheld': '0', 'net': '900', 'source': who},
        )
    assert 'Put one-offs on' not in a.get('/pay').text
    sid = irregular(a, acct)
    page = a.get('/pay').text
    # The last year's two; the one its name matches is ticked.
    assert 'Put one-offs on Fenici Design (2)' in page
    other, named = sorted((s for s in slips(env) if s.date.year == 2026), key=lambda s: s.source)
    assert f'name="slip_id" value="{other.id}" >' in page
    assert f'name="slip_id" value="{named.id}" checked>' in page
    resp = a.post('/pay/payments', data={'schedule_id': str(sid), 'slip_id': [str(named.id)]})
    assert '1%20one-off%20put%20on%20Fenici%20Design' in resp.headers['set-cookie']
    found = {s.id: s for s in slips(env)}
    assert (found[named.id].schedule_id, found[named.id].payday) == (sid, None)
    assert found[other.id].schedule_id is None
    with env.db() as db:
        user = me(db, alex)
        # It counts in its average now: 900 over the 93 days since 1 July.
        [e] = pay.schedules(db, user, TODAY)
        assert e.net_cents == round_div(900_00 * 365, 93 * 12)
        # Its account was the spending account, and still is.
        assert ledger.balances(db, user, TODAY)[acct] == 1000_00 + 3 * 900_00
    # Only one-offs can be put on it, and only on irregular pay.
    a.post('/pay/payments', data={'schedule_id': str(sid), 'slip_id': [str(named.id)]})
    weekly = a.post(
        '/pay/schedules',
        data={'name': 'Acme', 'frequency': 'weekly', 'anchor_date': '2026-09-03', 'net': '500'},
    )
    assert weekly.status_code == 303
    with env.db() as db:
        wid = db.execute(select(PaySchedule.id).where(PaySchedule.name == 'Acme')).scalar_one()
    resp = a.post('/pay/payments', data={'schedule_id': str(wid), 'slip_id': [str(other.id)]})
    assert 'is%20not%20irregular%20pay' in resp.headers['set-cookie']
    assert {s.id: s.schedule_id for s in slips(env)}[other.id] is None


def test_a_schedule_turned_irregular_takes_its_payslips(env: Env) -> None:
    a = env.login(env.user('alex'))
    acct = make_account(a)
    a.post(
        '/pay/schedules',
        data={'name': 'Studio', 'frequency': 'weekly', 'anchor_date': '2026-09-03', 'net': '500'},
    )
    with env.db() as db:
        sid = db.execute(select(PaySchedule.id)).scalar_one()
    a.post(
        '/payslips',
        data={
            'date': '2026-09-10',
            'gross': '500',
            'tax_withheld': '0',
            'net': '500',
            'payday': f'{sid}:2026-09-10',
        },
    )
    form = {'name': 'Studio', 'anchor_date': '2026-09-03', 'net': '2000', 'varies': 'on'}
    resp = a.post(f'/pay/schedules/{sid}', data=form | {'frequency': 'irregular'})
    assert 'payslip%20is%20a%20payment%20of%20it%20now' in resp.headers['set-cookie']
    [slip] = slips(env)
    assert (slip.schedule_id, slip.payday) == (sid, None)
    with env.db() as db:
        ps = db.get(PaySchedule, sid)
        assert ps is not None and not ps.varies
    resp = a.post(
        f'/pay/schedules/{sid}', data=form | {'frequency': 'weekly', 'account_id': str(acct)}
    )
    assert 'payment%20is%20a%20one-off%20now' in resp.headers['set-cookie']
    [slip] = slips(env)
    assert (slip.schedule_id, slip.payday) == (None, None)


def test_pdfs_named_for_irregular_pay_go_on_it(env: Env) -> None:
    a = env.login(env.user('alex'))
    sid = irregular(a, make_account(a))
    files = [
        (
            'file',
            ('a.pdf', simple_slip('20/09/2026', '1500.00', '0.00', '1500.00', 'Fenici Design')),
        ),
        ('file', ('b.pdf', simple_slip('22/09/2026', '700.00', '0.00', '700.00', 'Acme'))),
    ]
    page = a.post('/payslips/read', files=files).text
    assert page.count(f'value="pay:{sid}" selected') == 1
    one = a.post(
        '/payslips/read',
        files=[('file', ('c.pdf', simple_slip('02/10/2026', '1.00', '0.00', '1.00', 'X')))],
        data={'payday': f'pay:{sid}'},
    ).text
    assert f'value="pay:{sid}" selected' in one and 'Payslip for Fenici Design' in one


# ------------------------------------------------------ budget, forecast, tax


def test_budget_forecast_tax_and_calendar_expect_a_typical_month(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    acct = make_account(a)
    sid = irregular(a, acct)
    for on, net in (('2026-07-20', '3000'), ('2026-08-05', '6500'), ('2026-09-28', '1200')):
        payment(a, sid, on, net)
    typical = round_div(10700_00 * 365, 93 * 12)
    with env.db() as db:
        user = me(db, alex)
        [e] = pay.schedules(db, user, TODAY)
        assert e.net_cents == typical

        def month(m: int, y: int = 2026) -> int:
            start, end = budgeting.period_starting_in(db, user, date(y, m, 1))
            return budgeting.expected_pay(db, user, start, end, TODAY)

        # Gone by: what came in. Under way or to come: a typical month.
        assert (month(8), month(9), month(10), month(11)) == (6500_00, 1200_00, typical, typical)
        # Before it started, nothing.
        assert month(6) == 0

        flows = forecasting.build_input(db, user, TODAY, 12).flows
        daily = [f for f in flows if f.kind == 'irregular']
        assert {f.account_id for f in daily} == {acct} and daily[0].date == TODAY + timedelta(1)
        year = [f.amount_cents for f in daily if f.date <= date(2027, 10, 1)]
        assert sum(year) == typical * 12
        # A pay change to all of them is to every payday; irregular pay has
        # none, so it is left as it is.
        changed = apply_scenario(
            forecasting.build_input(db, user, TODAY, 12),
            [('income_change', {'from_date': '2026-10-01', 'delta_cents': 100_00})],
        )
        assert [f for f in changed.flows if f.kind == 'irregular'] == daily

        tv = taxes.view(db, user, TODAY)
        rest = sum(c for _d, c in spread(typical, date(2026, 10, 2), date(2027, 6, 30)))
        assert tv.projected.income_cents == 10700_00 + rest
        assert tv.untaxed == () and tv.missing == ()

        events = calendar_events.events(db, user, date(2026, 9, 1), date(2026, 9, 30), TODAY)
        assert [(e.title, e.amount_cents, e.status) for e in events if e.kind == 'pay'] == [
            ('Fenici Design', 1200_00, 'paid')
        ]

    # A big month counts what came in, beyond a typical one.
    payment(a, sid, '2026-10-01', '9000')
    with env.db() as db:
        user = me(db, alex)
        start, end = budgeting.period(db, user, TODAY)
        [x] = budgeting.irregular_pay(db, user, start, end, TODAY)
        assert x.received_cents == 9000_00 and x.expected_cents == 9000_00 > x.typical_cents
        assert budgeting.expected_pay(db, user, start, end, TODAY) == 9000_00
    page = a.get('/budget').text
    assert 'Fenici Design: $9,000.00 in, a typical month is' in page
    assert 'Fenici Design: $1,200.00 came in' in a.get('/budget?month=2026-09').text
    assert 'Fenici Design: a typical month, $' in a.get('/budget?month=2026-11').text
    # The scenario form offers it beside pay with paydays, by the month (D78).
    weekly = {'name': 'Acme', 'frequency': 'weekly', 'anchor_date': '2026-09-03', 'net': '500'}
    assert a.post('/pay/schedules', data=weekly).status_code == 303
    scenario = a.post('/scenarios', data={'name': 'Lean year'}).headers['location']
    form = a.get(scenario).text
    assert '>Acme</option>' in form and f'value="{sid}">Fenici Design</option>' in form
    assert 'Irregular pay changes by the month' in form
    now = fmt(round_div(19700_00 * 365, 93 * 12))
    assert f'Fenici Design expects {now} a typical month, from the last year.' in form


def test_a_scenario_changes_irregular_pay_by_the_month(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    acct = make_account(a)
    sid = irregular(a, acct)
    for on, net in (('2026-07-20', '3000'), ('2026-08-05', '6500'), ('2026-09-28', '1200')):
        payment(a, sid, on, net)
    scenario = a.post('/scenarios', data={'name': 'Quiet year'}).headers['location']
    form = {'kind': 'income_change', 'pay_schedule_id': str(sid), 'from_date': '2026-11-01'}
    assert a.post(f'{scenario}/adjustments', data=form | {'delta': '-1000'}).status_code == 303
    page = a.get(scenario).text
    assert 'Fenici Design, from 2026-11-01 change -$1,000.00 a month' in page
    sc_id = int(scenario.rsplit('/', 1)[1])
    with env.db() as db:
        user = me(db, alex)
        [sc] = db.execute(select(Scenario)).scalars().all()
        months = settings_for(db, user).forecast_months
        base = forecasting.base(db, env.clock, user, months)
        lean = forecasting.scenario(db, env.clock, user, [sc], months)
        less = sum(c for _d, c in spread(1000_00, date(2026, 11, 1), base.dates[-1]))
        assert lean.combined[-1] == base.combined[-1] - less
        assert lean.combined[:31] == base.combined[:31]  # to 31 October
    assert a.get(f'/forecast?scenario_id={sc_id}').status_code == 200


def test_ended_irregular_pay_expects_nothing_more(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    acct = make_account(a)
    sid = irregular(a, acct, end_date='2026-09-30')
    payment(a, sid, '2026-09-15', '2000')
    with env.db() as db:
        user = me(db, alex)
        start, end = budgeting.period(db, user, TODAY)
        assert budgeting.expected_pay(db, user, start, end, TODAY) == 0
        assert not [
            f for f in forecasting.build_input(db, user, TODAY, 3).flows if f.kind == 'irregular'
        ]
        tv = taxes.view(db, user, TODAY)
        assert tv.projected.income_cents == 2000_00
    with env.db() as db:
        assert db.execute(select(Payslip.schedule_id)).scalar_one() == sid


def test_another_persons_one_offs_cannot_be_put_on_my_pay(env: Env) -> None:
    a = env.login(env.user('alex'))
    a.post('/payslips', data={'date': '2026-09-01', 'gross': '9', 'tax_withheld': '0', 'net': '9'})
    [theirs] = slips(env)
    b = env.login(env.user('blake'))
    mine = irregular(b, make_account(b))
    resp = b.post('/pay/payments', data={'schedule_id': str(mine), 'slip_id': [str(theirs.id)]})
    assert resp.status_code == 404
    assert b.get(f'/pay?payday=pay:{mine}').status_code == 200
    assert a.get(f'/pay?payday=pay:{mine}').status_code == 404
    [still] = slips(env)
    assert still.schedule_id is None
