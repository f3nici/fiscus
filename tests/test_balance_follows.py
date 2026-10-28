"""A bill marked paid, a payslip or a budget spend moves its account's balance
(D42, D49)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from fiscus.models import BudgetSpend, Category, Goal, Transaction, User
from fiscus.services import budgeting, forecasting, ledger
from tests.conftest import NOW, CsrfClient, Env
from tests.test_bills import create_bill
from tests.test_features import _groceries, chart_data
from tests.test_paydays import bulk_form, put_in, schedule_id, weekly

SEPT_30 = datetime(2026, 9, 30, 2, tzinfo=UTC)
OCT_2 = datetime(2026, 10, 2, 2, tzinfo=UTC)
HOUR = timedelta(hours=1)


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


def test_a_bill_with_no_account_comes_off_the_spending_account(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    env.clock.set(SEPT_30)
    alex_acct = typed_account(a)
    a_savings = typed_account(a, 'Savings', '5000', 'savings')
    env.clock.set(NOW)
    bill = create_bill(a, name='Internet', amount='90', next_due='2026-10-10')
    a.post(f'/bills/{bill}/occurrence/2026-10-10/paid')
    assert balance(env, alex, alex_acct) == 1000_00 - 90_00
    assert balance(env, alex, a_savings) == 5000_00
    # Deleting the bill takes its payments, and what they moved, with it.
    a.post(f'/bills/{bill}/delete')
    assert balance(env, alex, alex_acct) == 1000_00
    assert moves(env) == []


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


def slip_for(client: CsrfClient, day: str, net: str, sid: int) -> str:
    """Put a payslip in for the schedule's payday `day`; the flash it shows."""
    resp = client.post(
        '/payslips',
        data={
            'date': day,
            'gross': '700',
            'tax_withheld': '160',
            'net': net,
            'payday': f'{sid}:{day}',
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200, resp.text
    return str(resp.text)


def history(env: Env, user: User, acct: int, start: date, end: date) -> dict[date, int]:
    with env.db() as db:
        u = db.get(User, user.id)
        assert u is not None
        return ledger.balance_history(db, u, [acct], start, end)[0]


def test_a_typed_balance_has_what_was_put_in_before_it(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    acct = typed_account(a)  # typed in today, 1 Oct, at 10:00
    weekly(a, acct)
    sid = schedule_id(env)
    bill = create_bill(
        a, name='Electricity', amount='121.40', next_due='2026-09-15', account_id=str(acct)
    )
    # Last week's pay put in later: today's balance has it, and says so.
    env.clock.set(NOW + HOUR)
    page = slip_for(a, '2026-09-24', '500', sid)
    assert 'The balance typed in for Everyday on 01 Oct already has it' in page
    assert 'Everyday is now' not in page
    resp = a.post(
        '/payslips/bulk',
        data=bulk_form(
            [('one.pdf', '2026-09-17', 'Acme', '650', '150', '500', f'{sid}:2026-09-17')]
        ),
        follow_redirects=True,
    )
    assert '1 payslip saved. 1 was already in a balance typed in on or after its date' in resp.text
    assert balance(env, alex, acct) == 1000_00
    # Tomorrow, a bill paid comes off.
    env.clock.set(OCT_2)
    resp = a.post(f'/bills/{bill}/occurrence/2026-09-15/paid', follow_redirects=True)
    assert 'Electricity marked paid. Everyday is now $878.60' in resp.text
    assert history(env, alex, acct, date(2026, 10, 1), date(2026, 10, 2)) == {
        date(2026, 10, 1): 1000_00,
        date(2026, 10, 2): 878_60,
    }
    # Typing it in again sets it, and a payment it already has can be undone
    # without moving it.
    env.clock.set(OCT_2 + HOUR)
    a.post(f'/accounts/{acct}/balance', data={'balance': '900'})
    assert balance(env, alex, acct) == 900_00
    assert a.get(f'/accounts/{acct}').text.count('Electricity') == 0
    a.post(f'/bills/{bill}/occurrence/2026-09-15/unpaid')
    assert balance(env, alex, acct) == 900_00


def test_pay_put_in_after_the_days_balance_moves_it(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    acct = typed_account(a)  # this morning, before the pay landed
    weekly(a, acct)
    sid = schedule_id(env)
    bill = create_bill(
        a, name='Electricity', amount='121.40', next_due='2026-10-01', account_id=str(acct)
    )
    # This afternoon: today's pay, and a bill marked paid.
    env.clock.set(NOW + 5 * HOUR)
    page = slip_for(a, '2026-10-01', '500', sid)
    assert 'Payslip saved for Acme on 01 Oct. Everyday is now $1,500.00' in page
    assert 'already has it' not in page
    resp = a.post(f'/bills/{bill}/occurrence/2026-10-01/paid', follow_redirects=True)
    assert 'Everyday is now $1,378.60' in resp.text
    assert balance(env, alex, acct) == 1378_60
    shown = a.get(f'/accounts/{acct}').text
    assert 'Acme' in shown and 'Electricity' in shown
    assert history(env, alex, acct, date(2026, 9, 30), date(2026, 10, 2)) == {
        date(2026, 9, 30): 0,  # not known yet
        date(2026, 10, 1): 1378_60,
        date(2026, 10, 2): 1378_60,
    }
    chart = chart_data(a.get('/forecast?months=1').text)
    assert chart['combined'][0] == 1378_60
    # Typed in again tonight, it has them: undoing one no longer moves it.
    env.clock.set(NOW + 9 * HOUR)
    a.post(f'/accounts/{acct}/balance', data={'balance': '1380'})
    assert balance(env, alex, acct) == 1380_00
    a.post(f'/bills/{bill}/occurrence/2026-10-01/unpaid')
    assert balance(env, alex, acct) == 1380_00
    # Tomorrow, it moves with what is dated after it.
    env.clock.set(OCT_2)
    slip_for(a, '2026-10-08', '500', sid)
    env.clock.set(datetime(2026, 10, 8, 2, tzinfo=UTC))
    assert balance(env, alex, acct) == 1880_00


def test_balances_left_as_they_showed_are_not_typed_in(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    env.clock.set(SEPT_30)
    everyday = typed_account(a)
    card = typed_account(a, 'Card', '-200', 'credit_card')
    env.clock.set(NOW)
    weekly(a, everyday)
    sid = schedule_id(env)
    # Only the card changed; the page sends Everyday's figure back as it was.
    resp = a.post(
        '/accounts/balances',
        data={f'balance_{everyday}': '1000.00', f'balance_{card}': '-250'},
        follow_redirects=True,
    )
    assert 'Updated 1 balance' in resp.text
    assert balance(env, alex, card) == -250_00
    # So today's pay, put in tomorrow, still goes into Everyday.
    env.clock.set(OCT_2)
    page = slip_for(a, '2026-10-01', '500', sid)
    assert 'Everyday is now $1,500.00' in page
    assert balance(env, alex, everyday) == 1500_00
    resp = a.post(
        '/accounts/balances',
        data={f'balance_{everyday}': '1500.00', f'balance_{card}': '-250.00'},
        follow_redirects=True,
    )
    assert 'Nothing to update' in resp.text


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


def free_now(client: CsrfClient) -> str:
    """The budget page's Free in accounts now figure."""
    found = re.search(
        r'Free in accounts now</div>\s*<div class="value \w*">([^<]+)</div>',
        client.get('/budget').text,
    )
    assert found is not None
    return found.group(1)


def test_spending_taken_off_a_budget_comes_off_its_account(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    env.clock.set(SEPT_30)
    everyday = typed_account(a)
    card = typed_account(a, 'Visa', '-200', 'credit_card')
    env.clock.set(NOW)
    groceries = _groceries(env, alex)
    a.post('/budget', data={'category_id': str(groceries), 'amount': '400'})
    # 1000 less 200 owing on the card, less 400 still to spend.
    assert free_now(a) == '$400.00'
    page = a.get('/budget').text
    assert 'Paid from' in page and f'<option value="{everyday}" selected>Everyday' in page
    resp = a.post(
        '/budget/spend',
        data={
            'category_id': str(groceries),
            'amount': '120',
            'note': 'Coles',
            'account_id': str(everyday),
        },
        follow_redirects=True,
    )
    assert '$120.00 off Groceries. Everyday is now $880.00' in resp.text
    assert balance(env, alex, everyday) == 880_00
    # Spent within the budget: what is free is as it was.
    assert free_now(a) == '$400.00'
    page = a.get(f'/accounts/{everyday}').text
    assert 'Groceries · Coles' in page and ' · spent' in page and '-$120.00' in page
    # Off the card when paid with it, off nothing when paid in cash.
    a.post(
        '/budget/spend',
        data={'category_id': str(groceries), 'amount': '50', 'account_id': str(card)},
    )
    assert balance(env, alex, card) == -250_00
    a.post('/budget/spend', data={'category_id': str(groceries), 'amount': '10', 'account_id': '0'})
    assert balance(env, alex, everyday) == 880_00 and balance(env, alex, card) == -250_00
    # Left out, it comes off the spending account; a refund puts money back.
    a.post('/budget/spend', data={'category_id': str(groceries), 'amount': '-20'})
    assert balance(env, alex, everyday) == 900_00
    page = a.get('/budget').text
    assert '· Everyday' in page and '· Visa' in page
    # Removing an entry puts back what it took off.
    coles = next(t.budget_spend_id for t in moves(env) if t.amount_cents == -120_00)
    resp = a.post(f'/budget/spend/{coles}/delete', follow_redirects=True)
    assert 'Entry removed. Everyday is now $1,020.00' in resp.text
    assert len(moves(env)) == 2
    # The forecast starts from the moved balances. The spends are not an
    # average to forecast, even once the line is out of the budget.
    chart = chart_data(a.get('/forecast?months=1').text)
    assert chart['combined'][0] == 1020_00 - 250_00
    a.post(f'/budget/{groceries}/remove')
    with env.db() as db:
        u = db.get(User, alex.id)
        assert u is not None
        assert forecasting.estimates(db, u, env.clock.today()) == []


def test_spending_a_typed_balance_already_has_moves_nothing(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    env.clock.set(SEPT_30)
    acct = typed_account(a)
    env.clock.set(NOW)
    groceries = _groceries(env, alex)
    a.post('/budget', data={'category_id': str(groceries), 'amount': '400'})
    resp = a.post(
        '/budget/spend',
        data={'category_id': str(groceries), 'amount': '30', 'on': '2026-09-29'},
        follow_redirects=True,
    )
    assert 'The balance typed in for Everyday on 30 Sep already has it' in resp.text
    assert balance(env, alex, acct) == 1000_00
    # A spend put in a goal from its line stays in the account.
    a.post('/goals', data={'name': 'Christmas', 'account_id': str(acct)})
    with env.db() as db:
        goal = db.execute(select(Goal.id)).scalar_one()
    a.post(
        f'/goals/{goal}/move',
        data={'amount': '50', 'as_spent': 'true', 'category_id': str(groceries)},
    )
    assert balance(env, alex, acct) == 1000_00


def test_an_entry_on_the_wrong_line_is_moved_and_its_money_follows(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    env.clock.set(SEPT_30)
    everyday = typed_account(a)
    card = typed_account(a, 'Visa', '-200', 'credit_card')
    env.clock.set(NOW)
    groceries = _groceries(env, alex)
    a.post('/budget', data={'category_id': str(groceries), 'amount': '400'})
    a.post('/budget', data={'new_category': 'Dining out', 'amount': '100'})
    with env.db() as db:
        dining = db.execute(select(Category.id).where(Category.name == 'Dining out')).scalar_one()
    a.post(
        '/budget/spend',
        data={'category_id': str(groceries), 'amount': '60', 'note': 'Pizza'},
    )
    with env.db() as db:
        spend = db.execute(select(BudgetSpend.id)).scalar_one()
    assert f'action="/budget/spend/{spend}"' in a.get('/budget').text

    def change(**data: str) -> str:
        form = {'category_id': str(dining), 'amount': '60', 'note': 'Pizza', 'on': '2026-10-01'}
        resp = a.post(f'/budget/spend/{spend}', data={**form, **data}, follow_redirects=True)
        assert resp.status_code == 200
        return resp.text

    # Moved to the line it belongs in: still off the same account, once.
    page = change(account_id=str(everyday))
    assert '$60.00 moved from Groceries to Dining out' in page
    with env.db() as db:
        u = db.get(User, alex.id)
        assert u is not None
        spent = budgeting.spent_by_category(db, u, date(2026, 10, 1), date(2026, 10, 31))
        assert spent == {dining: 60_00}
    [txn] = moves(env)
    assert (txn.account_id, txn.amount_cents, txn.category_id, txn.description) == (
        everyday,
        -60_00,
        dining,
        'Dining out · Pizza',
    )
    assert balance(env, alex, everyday) == 940_00
    # Its amount and account put right.
    page = change(amount='45.50', account_id=str(card))
    assert 'Entry saved. Everyday is now $1,000.00, Visa is now -$245.50' in page
    # Paid in cash after all, then back on the card; left out, it stays there.
    change(amount='45.50', account_id='0')
    assert moves(env) == [] and balance(env, alex, card) == -200_00
    change(amount='45.50', account_id=str(card))
    change(amount='45.50', note='')
    [txn] = moves(env)
    assert (txn.account_id, txn.description) == (card, 'Dining out')
    # Dated a day the typed balance already has, it moves nothing.
    page = change(amount='45.50', on='2026-09-29')
    assert 'The balance typed in for Visa on 30 Sep already has it' in page
    assert balance(env, alex, card) == -200_00
    # Nothing is saved from a form that cannot be.
    assert 'Enter an amount' in change(amount='0')
    with env.db() as db:
        row = db.get(BudgetSpend, spend)
        assert row is not None
        assert (row.category_id, row.amount_cents, row.date) == (
            dining,
            45_50,
            date(2026, 9, 29),
        )
