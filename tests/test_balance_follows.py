"""A bill marked paid or a payslip moves its account's balance (D42)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import select

from fiscus.models import Category, Transaction, User
from fiscus.services import forecasting, ledger
from tests.conftest import NOW, CsrfClient, Env
from tests.test_bills import create_bill
from tests.test_features import chart_data
from tests.test_paydays import bulk_form, put_in, schedule_id, weekly

SEPT_30 = datetime(2026, 9, 30, 2, tzinfo=UTC)
OCT_2 = datetime(2026, 10, 2, 2, tzinfo=UTC)


def typed_account(
    client: CsrfClient, name: str = 'Everyday', balance: str = '1000', type_: str = 'transaction'
) -> int:
    """An account made as the form makes one: with today's balance typed in."""
    resp = client.post('/accounts', data={'name': name, 'type': type_, 'balance': balance})
    assert resp.status_code == 303, resp.text
    return int(resp.headers['location'].rsplit('/', 1)[1])


def balance(env: Env, user: User, acct: int) -> int:
    with env.db() as db:
        u = db.get(User, user.id)
        assert u is not None
        return ledger.balances(db, u, env.clock.today())[acct]


def moves(env: Env) -> list[Transaction]:
    with env.db() as db:
        found = db.execute(select(Transaction).order_by(Transaction.id)).scalars().all()
        db.expunge_all()
        return list(found)


def test_marking_a_bill_paid_takes_it_off_its_account(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    env.clock.set(SEPT_30)
    acct = typed_account(a)
    env.clock.set(NOW)
    bill = create_bill(
        a, name='Electricity', amount='121.40', next_due='2026-10-15', account_id=str(acct)
    )
    resp = a.post(f'/bills/{bill}/occurrence/2026-10-15/paid', follow_redirects=True)
    assert 'Electricity marked paid. Everyday is now $878.60' in resp.text
    assert balance(env, alex, acct) == 878_60
    page = a.get(f'/accounts/{acct}').text
    assert 'Updated 30 Sep 2026, plus what has moved it since' in page
    assert 'Since 30 Sep 2026' in page and 'Electricity' in page and '-$121.40' in page
    # The forecast starts lower and no longer pays it on the 15th.
    chart = chart_data(a.get('/forecast?months=1').text)
    assert chart['combined'][0] == 878_60
    assert chart['combined'][chart['dates'].index('2026-10-16')] == 878_60

    resp = a.post(f'/bills/{bill}/occurrence/2026-10-15/unpaid', follow_redirects=True)
    assert 'Electricity marked unpaid. Everyday is now $1,000.00' in resp.text
    assert moves(env) == []
    assert 'Since 30 Sep' not in a.get(f'/accounts/{acct}').text


def test_each_person_pays_their_share_from_their_own_account(env: Env) -> None:
    alex, sam = env.user('alex'), env.user('sam')
    a, s = env.login(alex), env.login(sam)
    env.clock.set(SEPT_30)
    alex_acct = typed_account(a)
    a_savings = typed_account(a, 'Savings', '5000', 'savings')
    sam_acct = typed_account(s, 'Sam everyday', '500')
    env.clock.set(NOW)
    bill = create_bill(
        a,
        name='Internet',
        amount='90',
        next_due='2026-10-10',
        split_mode='percent',
        participants=[str(sam.id)],
        **{f'split_{alex.id}': '60', f'split_{sam.id}': '40'},
    )
    # Neither share names an account: each comes off the person's spending account.
    a.post(f'/bills/{bill}/occurrence/2026-10-10/paid')
    s.post(f'/bills/{bill}/occurrence/2026-10-10/paid')
    assert balance(env, alex, alex_acct) == 1000_00 - 54_00
    assert balance(env, alex, a_savings) == 5000_00
    assert balance(env, sam, sam_acct) == 500_00 - 36_00
    assert {t.user_id for t in moves(env)} == {alex.id, sam.id}
    # Deleting the bill takes its payments, and what they moved, with it.
    a.post(f'/bills/{bill}/delete')
    assert balance(env, alex, alex_acct) == 1000_00
    assert balance(env, sam, sam_acct) == 500_00


def test_a_payslip_adds_its_net_to_its_account(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    env.clock.set(SEPT_30)
    everyday = typed_account(a)
    savings = typed_account(a, 'Savings', '5000', 'savings')
    env.clock.set(NOW)
    weekly(a, savings)  # Acme, Thursdays from 3 Sep, paid into Savings
    sid = schedule_id(env)
    # Today's pay goes into the schedule's account.
    resp = a.post(
        '/payslips',
        data={
            'date': '2026-10-01',
            'gross': '650',
            'tax_withheld': '150',
            'net': '500',
            'payday': f'{sid}:2026-10-01',
        },
        follow_redirects=True,
    )
    assert 'Savings is now $5,500.00' in resp.text
    assert balance(env, alex, savings) == 5500_00
    # Edited: a different net, paid into Everyday.
    slip = moves(env)[0].payslip_id
    a.post(
        f'/payslips/{slip}',
        data={
            'date': '2026-10-01',
            'gross': '670',
            'tax_withheld': '150',
            'net': '520',
            'account_id': str(everyday),
            'payday': f'{sid}:2026-10-01',
        },
    )
    assert balance(env, alex, savings) == 5000_00
    assert balance(env, alex, everyday) == 1520_00
    page = a.get(f'/accounts/{everyday}').text
    assert 'Acme' in page and '$520.00' in page
    resp = a.post(f'/payslips/{slip}/delete', follow_redirects=True)
    assert 'Payslip deleted. Everyday is now $1,000.00' in resp.text
    assert moves(env) == []
    # Several at once: each goes in, a no-pay week moves nothing.
    resp = a.post(
        '/payslips/bulk',
        data=bulk_form(
            [
                ('one.pdf', '2026-10-01', 'Acme', '650', '150', '500', f'{sid}:2026-10-01'),
                ('two.pdf', '2026-10-01', 'Acme', '0', '0', '0', ''),
            ]
        ),
        follow_redirects=True,
    )
    assert '2 payslips saved. Savings is now $5,500.00' in resp.text
    assert len(moves(env)) == 1


def test_a_future_payslip_counts_on_its_date(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    env.clock.set(SEPT_30)
    acct = typed_account(a)
    env.clock.set(NOW)
    weekly(a, acct)
    # Next week's payslip, in early.
    put_in(a, '2026-10-08', '520', schedule=str(schedule_id(env)))
    assert balance(env, alex, acct) == 1000_00
    chart = chart_data(a.get('/forecast?months=1').text)
    dates = chart['dates']
    assert chart['combined'][dates.index('2026-10-08')] == 1520_00
    env.clock.set(datetime(2026, 10, 8, 2, tzinfo=UTC))
    assert balance(env, alex, acct) == 1520_00
    # Counted once: the forecast starts from it and adds only the next payday.
    chart = chart_data(a.get('/forecast?months=1').text)
    assert chart['combined'][0] == 1520_00
    assert chart['combined'][chart['dates'].index('2026-10-15')] == 2020_00


def test_a_typed_balance_already_counts_its_own_day_and_before(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    acct = typed_account(a)  # typed in today, 1 Oct
    weekly(a, acct)
    sid = schedule_id(env)
    bill = create_bill(
        a, name='Electricity', amount='121.40', next_due='2026-09-15', account_id=str(acct)
    )
    # Last week's pay put in now, and a bill paid today: today's balance has them.
    put_in(a, '2026-09-24', '500', schedule=str(sid))
    resp = a.post(f'/bills/{bill}/occurrence/2026-09-15/paid', follow_redirects=True)
    assert 'Electricity marked paid' in resp.text and 'Everyday is now' not in resp.text
    assert balance(env, alex, acct) == 1000_00
    assert len(moves(env)) == 2
    # Tomorrow, the next one paid comes off.
    env.clock.set(OCT_2)
    a.post(f'/bills/{bill}/occurrence/2026-10-15/paid')
    assert balance(env, alex, acct) == 878_60
    with env.db() as db:
        u = db.get(User, alex.id)
        assert u is not None
        days, _known = ledger.balance_history(db, u, [acct], date(2026, 10, 1), date(2026, 10, 2))
    assert days == {date(2026, 10, 1): 1000_00, date(2026, 10, 2): 878_60}
    # Typing it in again sets it, and a payment it already counts can be
    # undone without moving it.
    a.post(f'/accounts/{acct}/balance', data={'balance': '900'})
    assert balance(env, alex, acct) == 900_00
    a.post(f'/bills/{bill}/occurrence/2026-10-15/unpaid')
    assert balance(env, alex, acct) == 900_00


def test_payments_stay_out_of_the_spending_average(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    env.clock.set(SEPT_30)
    acct = typed_account(a)
    env.clock.set(NOW)
    with env.db() as db:
        utilities = db.execute(
            select(Category.id).where(Category.user_id == alex.id, Category.name == 'Utilities')
        ).scalar_one()
    bill = create_bill(
        a,
        name='Electricity',
        amount='121.40',
        next_due='2026-10-15',
        account_id=str(acct),
        category_id=str(utilities),
    )
    a.post(f'/bills/{bill}/occurrence/2026-10-15/paid')
    assert [t.category_id for t in moves(env)] == [utilities]
    with env.db() as db:
        u = db.get(User, alex.id)
        assert u is not None
        assert forecasting.estimates(db, u, env.clock.today()) == []
