"""Casual pay: the forecast expects the recent average per payday."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from fiscus.domain.pay import expected_pay
from fiscus.models import PaySchedule, Transaction
from fiscus.services import forecasting
from tests.conftest import CsrfClient, Env
from tests.test_imports_bills import import_csv, make_account

TODAY = date(2026, 10, 1)  # a Thursday
THURSDAY = date(2026, 9, 3)


def test_average_counts_paydays_with_nothing_paid() -> None:
    # Paid three Thursdays of four; the fourth (17 Sep) had no shifts.
    deposits = [(date(2026, 9, 3), 40000), (date(2026, 9, 10), 60000), (date(2026, 9, 24), 50000)]
    assert expected_pay(THURSDAY, 'weekly', deposits, TODAY, 1) == 37500


def test_average_starts_at_the_first_pay_and_allows_a_late_deposit() -> None:
    # A job that began in September; one pay landed a day late.
    deposits = [(date(2026, 9, 11), 30000), (date(2026, 9, 17), 50000)]
    assert expected_pay(THURSDAY, 'weekly', deposits, TODAY, 1) == round(80000 / 3)


def test_no_deposits_uses_the_estimate() -> None:
    assert expected_pay(THURSDAY, 'weekly', [], TODAY, 45000) == 45000
    old = [(date(2026, 5, 1), 99999)]
    assert expected_pay(THURSDAY, 'weekly', old, TODAY, 45000) == 45000


def _casual(client: CsrfClient, acct: int, **extra: str) -> None:
    resp = client.post(
        '/pay/schedules',
        data={
            'name': 'Cafe shifts',
            'frequency': 'weekly',
            'anchor_date': '2026-09-03',
            'net': '450',
            'account_id': str(acct),
            'varies': 'on',
            'match_text': 'cafe co',
            **extra,
        },
    )
    assert resp.status_code == 303


def test_forecast_uses_the_average_of_imported_pays(env: Env) -> None:
    alice = env.user('alice')
    a = env.login(alice)
    acct = make_account(a)
    _casual(a, acct)
    import_csv(
        a,
        acct,
        '03/09/2026,"400.00","PAY/SALARY CAFE CO"\n'
        '10/09/2026,"600.00","PAY/SALARY CAFE CO"\n'
        '24/09/2026,"500.00","PAY/SALARY CAFE CO"\n'
        '24/09/2026,"-30.00","CAFE CO PURCHASE"\n',
    )
    # Another person's deposits with the same name are not theirs to average.
    bob = env.login(env.user('bob'))
    import_csv(bob, make_account(bob), '17/09/2026,"9000.00","PAY/SALARY CAFE CO"\n')

    with env.db() as db:
        user = db.merge(alice)
        inp = forecasting.build_input(db, user, TODAY, 1)
    pays = [f for f in inp.flows if f.kind == 'pay']
    assert pays and {f.amount_cents for f in pays} == {37500}
    assert pays[0].date == date(2026, 10, 8)
    page = a.get('/pay').text
    assert '~$375.00' in page and 'recent average' in page


def test_varying_pay_needs_match_text(env: Env) -> None:
    a = env.login(env.user('alice'))
    acct = make_account(a)
    _casual(a, acct, match_text='  ')
    with env.db() as db:
        assert db.execute(select(PaySchedule)).first() is None
        assert db.execute(select(Transaction)).first() is None
