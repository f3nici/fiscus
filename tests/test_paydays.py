"""Payslips recording paydays: asked for, replacing the expected pay, edited, in bulk."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from fiscus.domain.pay import OpenPayday, Unsure, match_paydays
from fiscus.models import PaySchedule, Payslip, User
from fiscus.services import budgeting, calendar_events, forecasting, pay, taxes
from tests.conftest import CsrfClient, Env
from tests.pdfgen import build
from tests.test_imports_bills import make_account

TODAY = date(2026, 10, 1)  # a Thursday, the test clock's default


def simple_slip(paid: str, gross: str, tax: str, net: str, employer: str = 'Acme Pty Ltd') -> bytes:
    return build(
        [
            [
                (40, 800, f'Employer: {employer}'),
                (40, 780, f'Payment Date: {paid}'),
                (40, 760, f'Gross Pay: ${gross}'),
                (40, 740, f'PAYG: ${tax}'),
                (40, 720, f'Net Pay: ${net}'),
            ]
        ]
    )


def weekly(client: CsrfClient, acct: int, name: str = 'Acme', **extra: str) -> None:
    resp = client.post(
        '/pay/schedules',
        data={
            'name': name,
            'frequency': 'weekly',
            'anchor_date': '2026-09-03',
            'net': '500',
            'gross': '650',
            'tax_withheld': '150',
            'account_id': str(acct),
            **extra,
        },
    )
    assert resp.status_code == 303


def slips(env: Env) -> list[Payslip]:
    with env.db() as db:
        found = db.execute(select(Payslip).order_by(Payslip.date)).scalars().all()
        db.expunge_all()
        return list(found)


def put_in(client: CsrfClient, payday: str, net: str, **extra: str) -> None:
    resp = client.post(
        '/payslips',
        data={
            'date': payday,
            'gross': '700',
            'tax_withheld': '160',
            'net': net,
            'payday': f'{extra.pop("schedule")}:{payday}',
            **extra,
        },
    )
    assert resp.status_code == 303, resp.text


def schedule_id(env: Env) -> int:
    with env.db() as db:
        return db.execute(select(PaySchedule.id)).scalars().first() or 0


def bulk_form(rows: list[tuple[str, str, str, str, str, str, str]]) -> dict[str, list[str]]:
    keys = ['file_name', 'date', 'source', 'gross', 'tax_withheld', 'net', 'payday']
    form: dict[str, list[str]] = {k: [r[i] for r in rows] for i, k in enumerate(keys)}
    form['include'] = [str(i) for i in range(len(rows))]
    form['account_id'] = ['0']
    return form


# ---------------------------------------------------------------- matching


def test_match_takes_the_nearest_payday_once_and_prefers_the_named_job() -> None:
    a1 = OpenPayday(1, date(2026, 9, 24), 'Cafe')
    a2 = OpenPayday(1, date(2026, 10, 1), 'Cafe')
    b1 = OpenPayday(2, date(2026, 9, 30), 'Health Insight')
    hits = match_paydays(
        [
            (date(2026, 9, 30), 'Health Insight HQ Pty Ltd'),
            (date(2026, 10, 1), 'Cafe Co'),
            (date(2026, 10, 2), ''),
            (date(2026, 8, 1), ''),
        ],
        [a1, a2, b1],
    )
    assert hits == [b1, a2, None, None]


def test_two_jobs_paid_the_same_day_are_not_guessed() -> None:
    cafe = OpenPayday(1, date(2026, 9, 24), 'Cafe')
    shed = OpenPayday(2, date(2026, 9, 24), 'Warehouse', ('Bigco Logistics',))
    when = date(2026, 9, 24)
    assert match_paydays([(when, 'Someone Else')], [cafe, shed]) == [Unsure((cafe, shed))]
    # Named by an earlier payslip's employer, the Warehouse is taken first.
    hits = match_paydays([(when, 'Someone Else'), (when, 'BIGCO LOGISTICS PTY LTD')], [cafe, shed])
    assert hits == [cafe, shed]
    # Once one job's payday is taken, the other is the only one left.
    assert match_paydays([(when, '')], [shed]) == [shed]


# ---------------------------------------------------------------- asking


def test_recent_paydays_without_a_payslip_are_asked_for(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    weekly(a, make_account(a))
    sid = schedule_id(env)
    page = a.get('/pay').text
    assert 'Payslips to put in' in page
    # Weekly from 3 Sep: 3, 10, 17, 24 Sep and today.
    assert page.count('/no-pay') == 5
    assert '4 payslips' not in a.get('/').text and '5 payslips to put in' in a.get('/').text

    # The prefilled form for a payday carries the schedule's figures.
    form = a.get(f'/pay?payday={sid}:2026-09-24').text
    assert 'value="2026-09-24"' in form and 'value="500.00"' in form
    assert f'value="{sid}:2026-09-24" selected' in form

    put_in(a, '2026-09-24', '520', schedule=str(sid))
    assert a.post(f'/pay/schedules/{sid}/no-pay', data={'payday': '2026-09-17'}).status_code == 303
    page = a.get('/pay').text
    assert page.count('/no-pay') == 3
    assert 'no pay' in page
    # A payday cannot be recorded twice, nor a day that is not a payday.
    resp = a.post(f'/pay/schedules/{sid}/no-pay', data={'payday': '2026-09-17'})
    assert 'already%20has%20a%20payslip' in resp.headers['set-cookie']
    a.post(f'/pay/schedules/{sid}/no-pay', data={'payday': '2026-09-18'})
    assert len(slips(env)) == 2


# ------------------------------------------------------- replacing expected


def test_a_payslip_for_a_payday_replaces_its_expected_pay(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    acct = make_account(a)
    weekly(a, acct)
    sid = schedule_id(env)
    # Paid on Thursdays; next week's payslip is in already, for more than usual.
    put_in(a, '2026-10-08', '640', schedule=str(sid))
    # And a bonus on the same day, which is extra.
    a.post(
        '/payslips',
        data={
            'date': '2026-10-08',
            'gross': '1000',
            'tax_withheld': '300',
            'net': '700',
            'source': 'Bonus',
            'payday': '',
        },
    )
    with env.db() as db:
        user = db.merge(alex)
        flows = [f for f in forecasting.build_input(db, user, TODAY, 1).flows if f.kind == 'pay']
        on_8th = sorted(f.amount_cents for f in flows if f.date == date(2026, 10, 8))
        assert on_8th == [640_00, 700_00]
        assert {f.amount_cents for f in flows if f.date == date(2026, 10, 15)} == {500_00}
        expected = budgeting.expected_pay(db, user, date(2026, 10, 2), date(2026, 10, 15), TODAY)
        assert expected == 640_00 + 700_00 + 500_00
        events = calendar_events.events(db, user, date(2026, 10, 8), date(2026, 10, 8), TODAY)
        assert sorted(e.amount_cents for e in events if e.kind == 'pay') == [640_00, 700_00]
        tv = taxes.view(db, user, TODAY)
        # Future: the recorded payslip's 700 gross and the bonus's 1000, then 650 a week.
        weeks_left = sum(
            1 for i in pay.items(db, user, date(2026, 10, 2), date(2027, 6, 30), TODAY)
        )
        assert tv.projected.salary_gross_cents == 700_00 + 1000_00 + (weeks_left - 2) * 650_00


def test_auto_matches_by_date_and_one_off_stays_extra(env: Env) -> None:
    a = env.login(env.user('alex'))
    weekly(a, make_account(a))
    sid = schedule_id(env)
    # Paid a day early: matched to Thursday's payday.
    a.post(
        '/payslips',
        data={'date': '2026-09-23', 'gross': '650', 'tax_withheld': '150', 'net': '500'},
    )
    a.post(
        '/payslips',
        data={'date': '2026-08-20', 'gross': '50', 'tax_withheld': '0', 'net': '50'},
    )
    early, late = slips(env)
    assert early.schedule_id is None  # before the schedule's first payday
    assert (late.schedule_id, late.payday) == (sid, date(2026, 9, 24))


def test_edit_a_payslip_and_a_taken_payday_is_refused(env: Env) -> None:
    a = env.login(env.user('alex'))
    weekly(a, make_account(a))
    sid = schedule_id(env)
    put_in(a, '2026-09-24', '500', schedule=str(sid))
    put_in(a, '2026-10-01', '500', schedule=str(sid))
    first, _second = slips(env)
    page = a.get(f'/pay?slip={first.id}').text
    assert 'Edit payslip' in page and f'action="/payslips/{first.id}"' in page
    resp = a.post(
        f'/payslips/{first.id}',
        data={
            'date': '2026-09-24',
            'gross': '720',
            'tax_withheld': '170',
            'net': '550',
            'payday': f'{sid}:2026-09-24',
        },
    )
    assert resp.status_code == 303
    resp = a.post(
        f'/payslips/{first.id}',
        data={
            'date': '2026-09-24',
            'gross': '720',
            'tax_withheld': '170',
            'net': '550',
            'payday': f'{sid}:2026-10-01',
        },
    )
    assert resp.status_code == 200 and 'already has a payslip' in resp.text
    first, _second = slips(env)
    assert first.net_cents == 550_00 and first.payday == date(2026, 9, 24)


def test_a_varying_schedule_averages_its_payslips(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    weekly(a, make_account(a), varies='on')
    sid = schedule_id(env)
    put_in(a, '2026-09-03', '400', schedule=str(sid))
    put_in(a, '2026-09-10', '600', schedule=str(sid))
    a.post(f'/pay/schedules/{sid}/no-pay', data={'payday': '2026-09-17'})
    put_in(a, '2026-09-24', '500', schedule=str(sid))
    with env.db() as db:
        user = db.get(User, alex.id)
        assert user is not None
        [(_ps, net)] = pay.schedules(db, user, TODAY)
    assert net == 375_00
    assert '~$375.00' in a.get('/pay').text


def test_a_second_job_paid_the_same_day_can_be_put_in(env: Env) -> None:
    a = env.login(env.user('alex'))
    acct = make_account(a)
    weekly(a, acct, name='Cafe')
    weekly(a, acct, name='Warehouse')
    with env.db() as db:
        sid = {p.name: p.id for p in db.execute(select(PaySchedule)).scalars()}
    day = '2026-09-24'
    slip = {'date': day, 'gross': '650', 'tax_withheld': '150', 'net': '500'}
    # The employer names neither job: asked which, not given to the first.
    resp = a.post('/payslips', data={**slip, 'source': 'Bigco Logistics', 'payday': 'auto'})
    assert resp.status_code == 200
    assert 'Cafe and Warehouse are both paid around then' in resp.text
    resp = a.post(
        '/payslips/read',
        files=[
            (
                'file',
                (
                    'b.pdf',
                    simple_slip('24/09/2026', '650.00', '150.00', '500.00', 'Bigco Logistics'),
                    'application/pdf',
                ),
            )
        ],
    )
    assert 'Choose which pay this is' in resp.text
    assert 'value="auto" selected' in resp.text
    put_in(a, day, '500', schedule=str(sid['Warehouse']), source='Bigco Logistics')
    # The other job's payslip goes in against its own payday.
    resp = a.post('/payslips', data={**slip, 'net': '420', 'source': 'Little Cafe'})
    assert resp.status_code == 303, resp.text
    assert sorted((s.schedule_id, s.payday) for s in slips(env)) == [
        (sid['Cafe'], date(2026, 9, 24)),
        (sid['Warehouse'], date(2026, 9, 24)),
    ]
    # Next week Bigco is known to be the Warehouse.
    resp = a.post(
        '/payslips/read',
        files=[
            (
                'file',
                (
                    'b.pdf',
                    simple_slip('01/10/2026', '650.00', '150.00', '510.00', 'Bigco Logistics'),
                    'application/pdf',
                ),
            )
        ],
    )
    assert f'value="{sid["Warehouse"]}:2026-10-01" selected' in resp.text
    # A payday already taken says by which payslip, and how to move it.
    resp = a.post('/payslips', data={**slip, 'payday': f'{sid["Cafe"]}:{day}'})
    assert 'already has a payslip (Little Cafe, net $420.00)' in resp.text
    assert 'edit it and choose its pay first' in resp.text


def test_an_older_payday_this_financial_year_can_be_chosen(env: Env) -> None:
    a = env.login(env.user('alex'))
    weekly(a, make_account(a), anchor_date='2026-07-02')
    sid = schedule_id(env)
    page = a.get('/pay').text
    # 2 July is more than eight weeks back: not asked for, but offered.
    assert 'Thu 2 Jul 2026' not in page.split('id="slip-form"')[0]
    assert f'value="{sid}:2026-07-02"' in page
    put_in(a, '2026-07-02', '500', schedule=str(sid))
    assert [s.payday for s in slips(env)] == [date(2026, 7, 2)]


# ---------------------------------------------------------------- bulk


def test_several_pdfs_are_checked_then_added_together(env: Env) -> None:
    a = env.login(env.user('alex'))
    weekly(a, make_account(a))
    sid = schedule_id(env)
    resp = a.post(
        '/payslips/read',
        files=[
            (
                'file',
                (
                    'one.pdf',
                    simple_slip('10/09/2026', '650.00', '150.00', '500.00'),
                    'application/pdf',
                ),
            ),
            (
                'file',
                (
                    'two.pdf',
                    simple_slip('17/09/2026', '700.00', '160.00', '540.00'),
                    'application/pdf',
                ),
            ),
            (
                'file',
                (
                    'bonus.pdf',
                    simple_slip('20/09/2026', '1000.00', '300.00', '700.00'),
                    'application/pdf',
                ),
            ),
            ('file', ('notes.txt', b'hello', 'text/plain')),
        ],
    )
    assert resp.status_code == 200, resp.text
    page = resp.text
    assert 'Check payslips' in page
    assert 'notes.txt' in page and 'not a PDF' in page
    assert f'value="{sid}:2026-09-10" selected' in page
    assert f'value="{sid}:2026-09-17" selected' in page
    assert 'One-off payment (bonus, back pay)' in page
    assert len(slips(env)) == 0

    rows = [
        ('one.pdf', '2026-09-10', '650.00', '150.00', '500.00', f'{sid}:2026-09-10'),
        ('two.pdf', '2026-09-17', '700.00', '160.00', '540.00', f'{sid}:2026-09-17'),
        ('bonus.pdf', '2026-09-20', '1000.00', '300.00', '700.00', ''),
    ]
    form = bulk_form(
        [
            (name, when, 'Acme Pty Ltd', gross, tax, net, payday)
            for name, when, gross, tax, net, payday in rows
        ]
    )
    resp = a.post('/payslips/bulk', data=form)
    assert resp.status_code == 303, resp.text
    assert '3%20payslips%20saved' in resp.headers['set-cookie']
    saved = slips(env)
    assert [(s.payday, s.net_cents) for s in saved] == [
        (date(2026, 9, 10), 500_00),
        (date(2026, 9, 17), 540_00),
        (None, 700_00),
    ]

    # The same payday twice in one go: the second comes back to fix.
    twice = (f'{sid}:2026-09-24',) * 2
    form = bulk_form(
        [(f'{i}.pdf', '2026-09-24', '', '1', '0', '1', p) for i, p in enumerate(twice)]
    )
    resp = a.post('/payslips/bulk', data=form)
    assert resp.status_code == 200
    assert '1 payslip saved' in resp.text and 'already has a payslip' in resp.text
    assert len(slips(env)) == 4


def test_one_pdf_for_a_chosen_payday_keeps_that_payday(env: Env) -> None:
    a = env.login(env.user('alex'))
    weekly(a, make_account(a))
    sid = schedule_id(env)
    # Paid well after the payday it is for, so only the choice links it.
    resp = a.post(
        '/payslips/read',
        data={'payday': f'{sid}:2026-09-10'},
        files={
            'file': (
                'late.pdf',
                simple_slip('19/09/2026', '650.00', '150.00', '500.00'),
                'application/pdf',
            )
        },
    )
    assert resp.status_code == 200
    assert f'value="{sid}:2026-09-10" selected' in resp.text
    assert 'value="2026-09-19"' in resp.text


def test_another_persons_payslip_and_payday_are_out_of_reach(env: Env) -> None:
    a = env.login(env.user('alex'))
    weekly(a, make_account(a))
    sid = schedule_id(env)
    put_in(a, '2026-09-24', '500', schedule=str(sid))
    [slip] = slips(env)
    b = env.login(env.user('blake'))
    assert b.get(f'/pay?slip={slip.id}').status_code == 404
    assert b.get(f'/pay?payday={sid}:2026-09-24').status_code == 404
    resp = b.post(
        '/payslips',
        data={'date': '2026-10-01', 'gross': '1', 'tax_withheld': '0', 'net': '1', 'payday': 'x'},
    )
    assert resp.status_code == 200 and 'not one of your pay schedules' in resp.text
    resp = b.post(
        '/payslips',
        data={
            'date': '2026-10-01',
            'gross': '1',
            'tax_withheld': '0',
            'net': '1',
            'payday': '999:2026-10-01',
        },
    )
    assert resp.status_code == 404
    assert len(slips(env)) == 1


def test_a_quarterly_pay_is_spread_over_the_budget_months(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    acct = make_account(a)
    resp = a.post(
        '/pay/schedules',
        data={
            'name': 'Bonus',
            'frequency': 'quarterly',
            'anchor_date': '2026-07-15',
            'net': '3000',
            'account_id': str(acct),
        },
    )
    assert resp.status_code == 303
    assert '<option value="quarterly"' in a.get('/pay').text
    with env.db() as db:
        user = db.get(User, alex.id)
        assert user is not None
        months = {}
        for m in (10, 11, 12):
            start, end = budgeting.period_starting_in(db, user, date(2026, m, 1))
            months[m] = budgeting.expected_pay(db, user, start, end, TODAY)
        paydays = [i.when for i in pay.items(db, user, date(2026, 10, 1), date(2027, 6, 30), TODAY)]
    # A third of the pay each budget month, not all of it in October.
    assert months == {10: 1000_00, 11: 1000_00, 12: 1000_00}
    # The calendar and forecast still see the real paydays.
    assert paydays == [date(2026, 10, 15), date(2027, 1, 15), date(2027, 4, 15)]
    assert 'includes $1,000.00 a month of quarterly pay' in a.get('/budget').text
    # A payslip for the payday does not add the whole pay to October.
    put_in(a, '2026-10-15', '3300', schedule=str(schedule_id(env)))
    with env.db() as db:
        user = db.get(User, alex.id)
        assert user is not None
        start, end = budgeting.period_starting_in(db, user, date(2026, 10, 1))
        assert budgeting.expected_pay(db, user, start, end, TODAY) == 1000_00
        # Before the first payday's month, nothing.
        start, end = budgeting.period_starting_in(db, user, date(2026, 6, 1))
        assert budgeting.expected_pay(db, user, start, end, TODAY) == 0


def test_a_varying_quarterly_pay_averages_over_a_year() -> None:
    from fiscus.domain.pay import expected_pay

    deposits = [
        (date(2025, 10, 15), 2000_00),
        (date(2026, 1, 15), 4000_00),
        (date(2026, 4, 15), 3000_00),
        (date(2026, 7, 15), 3000_00),
    ]
    assert expected_pay(date(2025, 10, 15), 'quarterly', deposits, TODAY, 1_00) == 3000_00
    # Weekly still looks back thirteen weeks only.
    assert expected_pay(date(2026, 7, 15), 'weekly', deposits, TODAY, 1_00) == 3000_00 // 12
