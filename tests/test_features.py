"""Calendar, loans, forecast caching, budget, bill import, tax and the dashboard."""

from __future__ import annotations

import html
import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select

from fiscus.domain import loan as L
from fiscus.models import (
    Account,
    Bill,
    Budget,
    BudgetSpend,
    Category,
    ForecastCache,
    Loan,
    PaySchedule,
    Payslip,
    User,
)
from fiscus.services import budgeting, forecasting, ledger, taxes
from tests.conftest import CsrfClient, Env
from tests.test_budget_import import upload as _upload
from tests.test_imports_bills import ANZ_SEPT, create_bill, import_csv, make_account


def setup_household(env: Env) -> tuple[CsrfClient, User, int]:
    alex = env.user('alex')
    a = env.login(alex)
    acct = make_account(a, opening='3000')
    a.post(
        '/pay/schedules',
        data={
            'name': 'Acme wages',
            'frequency': 'fortnightly',
            'anchor_date': '2026-10-02',
            'net': '2500',
            'gross': '3400',
            'tax_withheld': '750',
            'account_id': str(acct),
        },
    )
    create_bill(a, name='Rent', amount='2200', next_due='2026-10-05', account_id=str(acct))
    return a, alex, acct


# ------------------------------------------------------------------ calendar


def test_calendar_month_view_matches_carelane_structure(env: Env) -> None:
    a, *_ = setup_household(env)
    page = a.get('/calendar?view=month&date=2026-10-01').text
    assert 'vuecal--month-view' in page and 'vuecal__title-bar' in page
    assert 'October 2026' in page
    assert page.count('vuecal__cell ') == 35  # 28 Sep to 1 Nov, Monday first
    assert 'vuecal__cell--today' in page
    assert 'ev-bill' in page and 'ev-pay' in page and 'Rent' in page
    # Forecast balance shown at the foot of future days.
    assert 'cell-balance' in page


def test_calendar_navigates_into_future_months(env: Env) -> None:
    a, *_ = setup_household(env)
    page = a.get('/calendar?view=month&date=2027-03-01').text
    assert 'March 2027' in page and 'Rent' in page and 'cell-balance' in page
    assert re.search(r'/calendar\?view=month&(amp;)?date=2027-04-01', page)
    # The partial for in-place navigation is just the calendar.
    partial = a.get(
        '/calendar?view=month&date=2027-03-01', headers={'HX-Request': 'true', 'HX-Target': 'cal'}
    )
    assert (
        partial.text.lstrip().startswith(('{#', '<div id="cal"'))
        or '<div id="cal"' in partial.text[:400]
    )
    assert '<html' not in partial.text


def test_calendar_week_day_and_agenda(env: Env) -> None:
    a, *_ = setup_household(env)
    week = a.get('/calendar?view=week&date=2026-10-05').text
    assert 'vuecal--week-view' in week and '5 to 11 October 2026' in week
    day = a.get('/calendar?view=day&date=2026-10-05').text
    assert 'Monday 5 October 2026' in day and 'Rent' in day
    agenda = a.get('/calendar?view=agenda&date=2026-10-01').text
    assert 'Rent' in agenda and 'Acme wages' in agenda
    assert 'data-view-given="1"' in agenda
    assert 'data-view-given="0"' in a.get('/calendar').text


def test_calendar_states_and_quick_actions(env: Env) -> None:
    a, *_ = setup_household(env)
    bill = create_bill(a, name='Phone', amount='55', next_due='2026-09-25')
    page = a.get('/calendar?view=month&date=2026-09-01').text
    assert re.search(r'ev-bill ev-overdue', page)
    sheet = a.get(f'/bills/{bill}/occurrence/2026-09-25')
    assert 'Mark paid' in sheet.text
    a.post(f'/bills/{bill}/occurrence/2026-09-25/paid', data={'back': '/calendar'})
    assert 'ev-bill ev-paid' in a.get('/calendar?view=month&date=2026-09-01').text


def test_shared_bill_on_participants_calendar_at_their_share(env: Env) -> None:
    a, _alex, _acct = setup_household(env)
    sam = env.user('sam')
    create_bill(a, name='Internet', amount='90', next_due='2026-10-12', participants=[str(sam.id)])
    page = env.login(sam).get('/calendar?view=agenda&date=2026-10-01').text
    assert 'ev-shared' in page and '-$45.00' in page and 'Rent' not in page


# --------------------------------------------------------------------- loans


def test_loan_page_shows_repayment_offset_savings_and_payoff(env: Env) -> None:
    a, _alex, acct = setup_household(env)
    offset = make_account(a, 'Offset', 'offset', '50000')
    resp = a.post(
        '/loans',
        data={
            'name': 'Mortgage',
            'principal': '600000',
            'start_date': '2024-07-10',
            'term_years': '30',
            'initial_rate': '6.0',
            'balance': '560000',
            'balance_date': '2026-10-01',
            'repayment_account_id': str(acct),
        },
    )
    loan = int(resp.headers['location'].rsplit('/', 1)[1])
    a.post(f'/loans/{loan}/offsets', data={'account_id': str(offset)})
    page = a.get(f'/loans/{loan}').text
    assert '$3,597.30' in page  # 600,000 at 6 % over 30 years
    assert 'Offset saves' in page and 'months sooner' in page
    # 1 Oct only: 50,000 x 6 % / 365 = $8.22
    assert '$8.22' in page
    # Rate change with history.
    a.post(f'/loans/{loan}/rates', data={'effective_date': '2026-11-01', 'new_rate': '6.35'})
    page = a.get(f'/loans/{loan}').text
    assert '6.35% from 1 Nov 2026' in page and '6% from 10 Jul 2024' in page


def test_backdated_rate_change_reprices_the_repayment(env: Env) -> None:
    a, _alex, acct = setup_household(env)
    resp = a.post(
        '/loans',
        data={
            'name': 'Mortgage',
            'principal': '600000',
            'start_date': '2024-07-10',
            'term_years': '30',
            'initial_rate': '6.0',
            'balance': '560000',
            'balance_date': '2026-10-01',
            'repayment_account_id': str(acct),
        },
    )
    loan = int(resp.headers['location'].rsplit('/', 1)[1])
    assert '$3,597.30' in a.get(f'/loans/{loan}').text
    assert '-$3,597.30' in a.get('/calendar?view=agenda&date=2026-10-10').text
    # A cut that took effect last month, before the balance date.
    a.post(f'/loans/{loan}/rates', data={'effective_date': '2026-09-01', 'new_rate': '5.5'})
    # 560,000 at 5.5 % over the 334 repayments from 10 Oct 2026 to 10 Jul 2054.
    expected = L.repayment(560_000_00, Decimal('5.5'), 334, 12)
    shown = f'${expected / 100:,.2f}'
    page = a.get(f'/loans/{loan}').text
    assert shown in page and '$3,597.30' not in page
    assert f'-{shown}' in a.get('/calendar?view=agenda&date=2026-10-10').text
    with env.db() as db:
        r = forecasting.base(db, env.clock, db.get(User, _alex.id), 1)  # type: ignore[arg-type]
    idx = r.dates.index(date(2026, 10, 10))
    assert r.accounts[acct][idx - 1] - r.accounts[acct][idx] == expected


# ------------------------------------------------------------------ forecast


def chart_data(page: str) -> Any:
    m = re.search(r'data-chart="([^"]*)"', page)
    assert m is not None
    return json.loads(html.unescape(m.group(1)))


def test_forecast_page_and_cache_invalidation(env: Env) -> None:
    a, alex, acct = setup_household(env)
    page = a.get('/forecast?months=3').text
    data = chart_data(page)
    assert data['dates'][0] == '2026-10-01'
    assert data['combined'][0] == 3000_00
    # +2500 on 2 Oct, -2200 on 5 Oct, +2500 on 16 Oct and 30 Oct.
    assert data['combined'][data['dates'].index('2026-10-31')] == 3000_00 + 3 * 2500_00 - 2200_00
    assert {m['label'] for m in data['markers']} >= {'Acme wages', 'Rent'}
    with env.db() as db:
        key1 = db.get(ForecastCache, alex.id).cache_key  # type: ignore[union-attr]
    # Same data, same key: served from cache.
    a.get('/forecast?months=3')
    with env.db() as db:
        assert db.get(ForecastCache, alex.id).cache_key == key1  # type: ignore[union-attr]
    # Changing data invalidates.
    create_bill(a, name='Car rego', amount='800', next_due='2026-10-20', account_id=str(acct))
    page = a.get('/forecast?months=3').text
    data = chart_data(page)
    assert (
        data['combined'][data['dates'].index('2026-10-31')]
        == 3000_00 + 3 * 2500_00 - 2200_00 - 800_00
    )
    with env.db() as db:
        assert db.get(ForecastCache, alex.id).cache_key != key1  # type: ignore[union-attr]


def test_the_dashboard_and_forecast_horizons_share_the_cache(env: Env) -> None:
    a, alex, _acct = setup_household(env)
    a.get('/forecast?months=12')
    a.get('/')  # three months
    with env.db() as db:
        cached = db.get(ForecastCache, alex.id)
        assert cached is not None
        assert set(cached.payload['runs']) == {'12', '3'}
        stamp = cached.created_at
    # Back to the forecast: served from the cache, not run again.
    env.clock.set(env.clock.now() + timedelta(minutes=5))
    a.get('/forecast?months=12')
    with env.db() as db:
        assert db.get(ForecastCache, alex.id).created_at == stamp  # type: ignore[union-attr]


def test_shared_bill_change_invalidates_participants_forecast(env: Env) -> None:
    a, _alex, _ = setup_household(env)
    sam = env.user('sam')
    s = env.login(sam)
    make_account(s, 'Sam everyday', opening='100')
    s.get('/forecast')
    with env.db() as db:
        before = db.get(User, sam.id).data_version  # type: ignore[union-attr]
    create_bill(a, name='Internet', amount='90', next_due='2026-10-12', participants=[str(sam.id)])
    with env.db() as db:
        assert db.get(User, sam.id).data_version > before  # type: ignore[union-attr]
    page = s.get('/forecast?months=1').text
    data = chart_data(page)
    assert data['combined'][data['dates'].index('2026-10-12')] == 100_00 - 45_00


def test_low_balance_warning_on_forecast_and_dashboard(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    acct = make_account(a, opening='500')
    create_bill(
        a,
        name='Insurance',
        amount='900',
        next_due='2026-10-20',
        account_id=str(acct),
        frequency='yearly',
    )
    assert 'projected to fall to' in a.get('/forecast').text
    dash = a.get('/').text
    assert 'Low balance ahead' in dash and '20 October 2026' in dash


def test_discretionary_spending_uses_average_or_budget(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    acct = make_account(a, opening='1000')
    import_csv(a, acct, '15/09/2026,"-300.00","COLES"\n20/08/2026,"-300.00","COLES"\n')
    with env.db() as db:
        est = forecasting.estimates(db, db.get(User, alex.id), date(2026, 10, 1))  # type: ignore[arg-type]
        assert len(est) == 1 and est[0].source == 'average'
        # 600 over 90 days, as a month of 30.4375 days: $202.92
        assert est[0].monthly_cents == -202_92
        groceries = (
            db.execute(
                select(Category).where(Category.user_id == alex.id, Category.name == 'Groceries')
            )
            .scalar_one()
            .id
        )
    a.post('/rules', data={'pattern': 'coles', 'category_id': str(groceries), 'apply_now': 'true'})
    a.post(
        '/budget', data={'category_id': str(groceries), 'amount': '400', 'use_in_forecast': 'true'}
    )
    with env.db() as db:
        est = forecasting.estimates(db, db.get(User, alex.id), date(2026, 10, 1))  # type: ignore[arg-type]
        assert [(e.name, e.monthly_cents, e.source) for e in est] == [
            ('Groceries', -400_00, 'budget')
        ]


# -------------------------------------------------------------------- budget


def _groceries(env: Env, user: User) -> int:
    with env.db() as db:
        return int(
            db.execute(
                select(Category.id).where(Category.user_id == user.id, Category.name == 'Groceries')
            ).scalar_one()
        )


def test_spending_is_taken_off_the_budget_by_hand(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    acct = make_account(a)
    groceries = _groceries(env, alex)
    a.post('/rules', data={'pattern': 'coles', 'category_id': str(groceries)})
    # Imported transactions no longer count towards the budget.
    import_csv(a, acct, '01/10/2026,"-450.00","COLES"\n')
    a.post('/budget', data={'category_id': str(groceries), 'amount': '400'})
    page = a.get('/budget?month=2026-10').text
    assert '$400.00' in page and '$450.00' not in page
    resp = a.post(
        '/budget/spend',
        data={'category_id': str(groceries), 'amount': '120', 'note': 'Coles'},
        follow_redirects=True,
    )
    assert '$120.00 off Groceries' in resp.text
    a.post('/budget/spend', data={'category_id': str(groceries), 'amount': '330'})
    page = a.get('/budget?month=2026-10').text
    assert '$450.00' in page and '-$50.00' in page and '1 category over' in page
    # A refund puts money back.
    a.post('/budget/spend', data={'category_id': str(groceries), 'amount': '-60'})
    page = a.get('/budget?month=2026-10').text
    assert '$390.00' in page and '$10.00' in page and 'nothing over' in page
    # Removing an entry puts it back too; the next month starts full.
    with env.db() as db:
        sid = db.execute(select(BudgetSpend.id).where(BudgetSpend.note == 'Coles')).scalar_one()
    a.post(f'/budget/spend/{sid}/delete')
    assert '$130.00' in a.get('/budget?month=2026-10').text
    nov = a.get('/budget?month=2026-11').text
    assert '1 Nov to 30 Nov 2026' in nov and 'Nothing taken off yet' in nov
    assert (
        a.post('/budget/spend', data={'category_id': str(groceries), 'amount': '0'}).status_code
        == 303
    )
    with env.db() as db:
        assert db.execute(select(BudgetSpend)).scalars().all().__len__() == 2


def test_a_budget_line_can_be_removed(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    groceries = _groceries(env, alex)
    a.post('/budget', data={'category_id': str(groceries), 'amount': '600', 'cycle_months': '12'})
    a.post('/budget/spend', data={'category_id': str(groceries), 'amount': '25', 'note': 'Coles'})
    page = a.get('/budget').text
    assert f'action="/budget/{groceries}/remove"' in page
    resp = a.post(f'/budget/{groceries}/remove', follow_redirects=True)
    assert 'Groceries removed from the budget' in resp.text
    assert f'action="/budget/{groceries}/remove"' not in resp.text
    with env.db() as db:
        assert db.execute(select(Budget).where(Budget.user_id == alex.id)).first() is None
        # What was taken off stays on record.
        assert len(db.execute(select(BudgetSpend)).scalars().all()) == 1
    # Nothing left to remove.
    assert a.post(f'/budget/{groceries}/remove').status_code == 404


def test_home_loan_repayments_stay_in_the_budget(env: Env) -> None:
    a, alex, acct = setup_household(env)
    a.post(
        '/loans',
        data={
            'name': 'Mortgage',
            'principal': '600000',
            'start_date': '2024-07-10',
            'term_years': '30',
            'initial_rate': '6.0',
            'balance': '560000',
            'balance_date': '2026-10-01',
            'repayment_account_id': str(acct),
        },
    )
    groceries = _groceries(env, alex)
    a.post('/budget', data={'category_id': str(groceries), 'amount': '400'})
    a.post('/budget', data={'new_category': 'Mortgage repayment', 'amount': '3600'})
    page = a.get('/budget?month=2026-10').text
    # First in the table, ahead of every other line, with no way to remove it.
    body = page[page.index('<tbody>') :]
    assert body.index('Mortgage repayments') < body.index('Groceries')
    assert '0 of 1 repayment made' in page
    assert 'Always in the budget while you have the loan' in page
    # Budgeted: groceries, the stray mortgage line and the loan.
    assert '$7,597.30' in page
    assert 'Remove this line so they are not counted twice' in page
    # Once the repayment day passes it is spent.
    env.clock.set(env.clock.now() + timedelta(days=10))
    page = a.get('/budget?month=2026-10').text
    assert '1 of 1 repayment made' in page
    # Clearing the budget on import leaves the loan in it.
    url = _upload(a, b'Item,Amount,Frequency\nInternet,79,Monthly\nMortgage,3597.30,Monthly\n')
    preview = a.get(url).text
    assert 'Your home loan is already in the budget' in preview
    a.post(
        url + '/apply',
        data={
            'count': '1',
            'include_0': 'on',
            'name_0': 'Internet',
            'category_0': 'Internet',
            'amount_0': '79',
            'mode': 'clear',
        },
    )
    page = a.get('/budget?month=2026-10').text
    assert 'Mortgage repayments' in page and '$3,676.30' in page


def test_budget_shows_expected_earnings_against_budgeted(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    make_account(a, opening='1000')
    a.post(
        '/pay/schedules',
        data={
            'name': 'Acme wages',
            'frequency': 'fortnightly',
            'anchor_date': '2026-10-02',
            'net': '2500',
        },
    )
    groceries = _groceries(env, alex)
    a.post('/budget', data={'category_id': str(groceries), 'amount': '400'})
    a.post('/budget', data={'category_id': '0', 'new_category': 'Fuel', 'amount': '200'})
    a.post('/budget/spend', data={'category_id': str(groceries), 'amount': '100'})
    with env.db() as db:
        user = db.get(User, alex.id)
        assert user is not None
        start, end = budgeting.period(db, user, date(2026, 10, 1))
        rows = budgeting.rows(db, user, start, end, date(2026, 10, 1))
        s = budgeting.summary(rows, budgeting.expected_pay(db, user, start, end, date(2026, 10, 1)))
    # Paid on 2, 16 and 30 October.
    assert s.expected_cents == 7500_00
    assert s.budget_cents == 600_00
    assert s.unbudgeted_cents == 6900_00
    assert s.spent_cents == 100_00 and s.to_spend_cents == 500_00
    page = a.get('/budget').text
    assert 'Expected to earn' in page and '$7,500.00' in page and '$6,900.00' in page
    # 1000 in the account, 500 still budgeted.
    assert 'Free in accounts now' in page and '$500.00 in accounts' not in page
    assert '$1,000.00 in accounts, less what is still to spend' in page
    dash = a.get('/').text
    assert 'Unbudgeted (extra)' in dash and '$6,900.00' in dash and 'Budget left' in dash


def test_budget_month_starts_on_the_chosen_day(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    groceries = _groceries(env, alex)
    a.post('/budget', data={'category_id': str(groceries), 'amount': '400'})
    assert a.post('/budget/start-day', data={'start_day': '0'}).status_code == 303
    a.post('/budget/start-day', data={'start_day': '15'})
    # Today is 1 October, so the budget month began on 15 September.
    page = a.get('/budget').text
    assert '15 Sep to 14 Oct 2026' in page
    a.post(
        '/budget/spend', data={'category_id': str(groceries), 'amount': '50', 'on': '2026-09-20'}
    )
    a.post(
        '/budget/spend', data={'category_id': str(groceries), 'amount': '70', 'on': '2026-10-20'}
    )
    page = a.get('/budget').text
    assert '$350.00' in page and '$330.00' not in page
    later = a.get('/budget?month=2026-10').text
    assert '15 Oct to 14 Nov 2026' in later and '$330.00' in later


def test_a_paid_bill_comes_off_its_category(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    utilities = cat_id(env, alex, 'Utilities')
    bill = create_bill(
        a, name='Power', amount='200', next_due='2026-10-20', category_id=str(utilities)
    )
    a.post('/budget', data={'category_id': str(utilities), 'amount': '300'})
    page = a.get('/budget').text
    assert '$200.00 a month of bills' in page and 'bills paid' not in page
    a.post(f'/bills/{bill}/occurrence/2026-10-20/paid')
    page = a.get('/budget').text
    assert '$200.00 bills paid' in page and 'nothing over' in page
    # The budget stays 300; 100 of it is left.
    assert '$300.00' in page and '$100.00' in page


def test_balances_are_typed_in(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    everyday = make_account(a, opening='1000')
    savings = make_account(a, 'Savings', 'savings', opening='5000')
    import_csv(a, everyday, '15/09/2026,"-300.00","COLES"\n')
    resp = a.post(
        '/accounts/balances',
        data={f'balance_{everyday}': '812.40', f'balance_{savings}': ''},
        follow_redirects=True,
    )
    assert 'Updated 1 balance' in resp.text
    with env.db() as db:
        user = db.get(User, alex.id)
        assert user is not None
        bal = ledger.balances(db, user, date(2026, 10, 1))
        assert bal == {everyday: 812_40, savings: 5000_00}
        acct = db.get(Account, everyday)
        assert acct is not None and acct.balance_updated_on == date(2026, 10, 1)
    assert a.post('/accounts/balances', data={f'balance_{savings}': 'abc'}).status_code == 303
    a.post(f'/accounts/{savings}/balance', data={'balance': '5100'})
    page = a.get(f'/accounts/{savings}').text
    assert '$5,100.00' in page and 'Updated 1 Oct 2026' in page
    # Editing the account without touching the balance leaves it be.
    a.post(
        f'/accounts/{everyday}',
        data={
            'name': 'Day to day',
            'type': 'transaction',
            'opening_balance': '1112.40',
            'balance': '812.40',
        },
    )
    with env.db() as db:
        user = db.get(User, alex.id)
        assert user is not None
        assert ledger.balances(db, user, date(2026, 10, 1))[everyday] == 812_40


def test_any_budget_replaces_the_average_in_the_forecast(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    make_account(a)
    a.post('/budget', data={'category_id': str(_groceries(env, alex)), 'amount': '400'})
    with env.db() as db:
        est = forecasting.estimates(db, db.get(User, alex.id), date(2026, 10, 1))  # type: ignore[arg-type]
    assert [(e.name, e.monthly_cents, e.source) for e in est] == [('Groceries', -400_00, 'budget')]


# -------------------------------------------------- bills in the budget


def cat_id(env: Env, user: User, name: str) -> int:
    with env.db() as db:
        return int(
            db.execute(
                select(Category.id).where(Category.user_id == user.id, Category.name == name)
            ).scalar_one()
        )


def test_bills_are_paid_out_of_their_category_budget(env: Env) -> None:
    alex = env.user('alex')
    sam = env.user('sam')
    a = env.login(alex)
    housing = cat_id(env, alex, 'Housing')
    a.post('/budget', data={'category_id': str(housing), 'amount': '150'})
    create_bill(
        a,
        name='Council rates',
        amount='520',
        frequency='quarterly',
        next_due='2026-11-15',
        category_id=str(housing),
        split_mode='percent',
        participants=[str(sam.id)],
        **{f'split_{alex.id}': '50', f'split_{sam.id}': '50'},
    )
    # My half of 520 a quarter is 86.67 a month. It does not add to the 150.
    page = a.get('/budget?month=2026-10').text
    assert '$150.00' in page and '$236.67' not in page
    assert 'Bills here come to $86.67 a month' in page
    with env.db() as db:
        assert db.execute(select(Budget.monthly_cents)).scalar_one() == 150_00
    assert '-$260.00' in env.login(sam).get('/calendar?view=agenda&date=2026-11-01').text
    # Below the bills is allowed, with a note.
    resp = a.post(
        '/budget', data={'category_id': str(housing), 'amount': '50'}, follow_redirects=True
    )
    assert 'come to $86.67 a month, more than this' in resp.text
    with env.db() as db:
        assert db.execute(select(Budget.monthly_cents)).scalar_one() == 50_00
    # The bills page shows the same monthly figure, filtered by category.
    bills_page = a.get(f'/bills?category={housing}').text
    assert 'Bills: Housing' in bills_page and '$86.67' in bills_page


def test_a_bill_without_a_budget_adds_nothing_to_the_budget(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    utilities = cat_id(env, alex, 'Utilities')
    bill = create_bill(
        a, name='Power', amount='200', next_due='2026-10-20', category_id=str(utilities)
    )
    a.post(f'/bills/{bill}/occurrence/2026-10-20/paid')
    with env.db() as db:
        user = db.get(User, alex.id)
        assert user is not None
        start, end = budgeting.period(db, user, date(2026, 10, 1))
        rows = budgeting.rows(db, user, start, end, date(2026, 10, 1))
        s = budgeting.summary(rows, 0)
    assert s.budget_cents == 0 and s.spent_cents == 200_00


def _saved(env: Env, user: User, month: date) -> dict[str, tuple[int | None, str]]:
    with env.db() as db:
        u = db.get(User, user.id)
        assert u is not None
        start, end = budgeting.period_starting_in(db, u, month)
        rows = budgeting.rows(db, u, start, end, date(2026, 10, 1))
        return {r.category.name: (r.saved_cents, r.state) for r in rows}


def test_a_yearly_cost_is_saved_up_not_a_month_over(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    resp = a.post(
        '/budget',
        data={'new_category': 'Car rego', 'amount': '900', 'cycle_months': '12'},
        follow_redirects=True,
    )
    assert 'Saving up $75.00 a month for Car rego, spent every year' in resp.text
    page = a.get('/budget?month=2026-10').text
    assert 'Saving up $900.00 every year' in page
    assert _saved(env, alex, date(2026, 10, 1))['Car rego'] == (75_00, 'ok')
    assert _saved(env, alex, date(2026, 12, 1))['Car rego'] == (225_00, 'ok')
    # Paid in December: out of what is saved, not December's $75.
    with env.db() as db:
        cid = db.execute(select(Category.id).where(Category.name == 'Car rego')).scalar_one()
    a.post('/budget/spend', data={'category_id': str(cid), 'amount': '900', 'on': '2026-12-10'})
    assert _saved(env, alex, date(2026, 12, 1))['Car rego'] == (-675_00, 'over')
    # Already had some put away: say so, and December is covered.
    a.post(
        '/budget',
        data={'category_id': str(cid), 'amount': '900', 'cycle_months': '12', 'saved': '700'},
    )
    assert _saved(env, alex, date(2026, 12, 1))['Car rego'] == (25_00, 'ok')
    assert _saved(env, alex, date(2027, 1, 1))['Car rego'] == (100_00, 'ok')
    # Changing the amount later keeps what is saved so far.
    env.clock.set(datetime(2027, 1, 5, 2, tzinfo=UTC))
    a = env.login(alex)
    a.post('/budget', data={'category_id': str(cid), 'amount': '1200', 'cycle_months': '12'})
    with env.db() as db:
        row = db.execute(select(Budget)).scalar_one()
        assert (row.saved_since, row.saved_opening_cents) == (date(2027, 1, 1), 25_00)
    assert _saved(env, alex, date(2027, 1, 1))['Car rego'] == (125_00, 'ok')
    # The forecast spends it evenly, a month at a time.
    with env.db() as db:
        assert db.execute(select(Budget.monthly_cents)).scalar_one() == 100_00
    # Back to monthly: an ordinary budget again.
    a.post('/budget', data={'category_id': str(cid), 'amount': '100', 'cycle_months': '1'})
    with env.db() as db:
        row = db.execute(select(Budget)).scalar_one()
        assert (row.cycle_months, row.saved_since) == (1, None)
    assert _saved(env, alex, date(2027, 1, 1))['Car rego'] == (None, 'ok')
    assert (
        a.post(
            '/budget', data={'category_id': str(cid), 'amount': '1', 'cycle_months': '5'}
        ).status_code
        == 303
    )


def test_a_quarterly_bill_paid_comes_out_of_what_is_saved(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    housing = cat_id(env, alex, 'Housing')
    bill = create_bill(
        a,
        name='Council rates',
        amount='300',
        frequency='quarterly',
        next_due='2026-11-15',
        category_id=str(housing),
    )
    # Not saved up yet: the form suggests every 3 months, at the quarter's amount.
    page = a.get('/budget?month=2026-10').text
    assert 'Some of its bills are not monthly' in page
    assert 'value="300.00"' in page and '<option value="3" selected>' in page
    a.post('/budget', data={'category_id': str(housing), 'amount': '300', 'cycle_months': '3'})
    with env.db() as db:
        row = db.execute(select(Budget)).scalar_one()
        assert (row.monthly_cents, row.cycle_months) == (100_00, 3)
    a.post(f'/bills/{bill}/occurrence/2026-11-15/paid')
    assert _saved(env, alex, date(2026, 11, 1))['Housing'] == (-100_00, 'over')
    assert _saved(env, alex, date(2027, 1, 1))['Housing'] == (100_00, 'ok')
    nov = a.get('/budget?month=2026-11').text
    assert '1 category over' in nov


def test_a_saved_up_line_says_whether_it_will_hold_its_next_bill(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    car = cat_id(env, alex, 'Transport')
    create_bill(
        a,
        name='Roadside assistance',
        amount='330',
        frequency='yearly',
        next_due='2027-03-15',
        category_id=str(car),
    )
    a.post('/budget', data={'category_id': str(car), 'amount': '330', 'cycle_months': '12'})
    # Started in October: six months of 27.50 by March is 165, half of it.
    page = a.get('/budget').text
    assert '$330.00 due 15 Mar 2027: $165.00 short by then' in page
    a.post(
        '/budget',
        data={'category_id': str(car), 'amount': '330', 'cycle_months': '12', 'saved': '165'},
    )
    assert '$330.00 due 15 Mar 2027: on track' in a.get('/budget').text

    # Put aside into a goal: out of the line, but still kept for the bill.
    goal = a.post('/goals', data={'name': 'Yearly bills', 'category_id': str(car)})
    gid = int(goal.headers['location'].rsplit('/', 1)[1])
    a.post(f'/goals/{gid}/move', data={'amount': '192.50', 'as_spent': 'true'})
    page = a.get('/budget').text
    assert '$0.00' in page and '$330.00 due 15 Mar 2027: on track' in page


def test_budget_in_forecast_is_only_the_part_beyond_bills(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    utilities = cat_id(env, alex, 'Utilities')
    create_bill(a, name='Power', amount='200', next_due='2026-10-20', category_id=str(utilities))
    a.post(
        '/budget',
        data={'category_id': str(utilities), 'amount': '250', 'use_in_forecast': 'true'},
    )
    with env.db() as db:
        assert db.execute(select(Budget.monthly_cents)).scalar_one() == 250_00
        est = forecasting.estimates(db, db.get(User, alex.id), date(2026, 10, 1))  # type: ignore[arg-type]
    # Power is forecast from its own date; the budget adds only the other 50.
    assert [(e.name, e.monthly_cents, e.source) for e in est] == [('Utilities', -50_00, 'budget')]


def test_old_housing_address_lands_on_bills(env: Env) -> None:
    a = env.login(env.user('alex'))
    resp = a.get('/housing')
    assert resp.status_code == 308 and resp.headers['location'] == '/bills'


# --------------------------------------------------------------- bill import

FIELDS = ['name', 'amount', 'frequency', 'category', 'due_date', 'shared_with', 'split_percent']
MAP = {f'map_{f}': str(i) for i, f in enumerate(FIELDS)}


def upload_bills(client: CsrfClient, filename: str, body: bytes, kind: str = 'text/csv') -> str:
    resp = client.post('/bills/import', files={'file': (filename, body, kind)})
    assert resp.status_code == 303, resp.text
    return str(resp.headers['location'].rsplit('/', 1)[1])


def test_bill_templates_download(env: Env) -> None:
    a = env.login(env.user('alex'))
    csv_t = a.get('/bills/template.csv')
    assert csv_t.status_code == 200 and csv_t.text.startswith(
        'name,amount,frequency,category,due_date'
    )
    js = a.get('/bills/template.json')
    assert json.loads(js.text)['bills'][0]['name'] == 'Council rates'
    assert 'Upload and preview' in a.get('/bills/import').text


def test_bill_csv_import_preview_and_reimport_diff(env: Env) -> None:
    partner = env.user('partner')
    alex = env.user('alex')
    a = env.login(alex)
    # A bill made by hand earlier, under a name the file also uses.
    create_bill(a, name='Water', amount='300', frequency='quarterly', next_due='2026-10-10')
    first = a.get('/bills/template.csv').text
    name = upload_bills(a, 'h.csv', first.encode())
    preview = a.get(f'/bills/import/{name}').text
    assert 'Added (2)' in preview and 'Changed (1)' in preview
    with env.db() as db:
        assert len(db.execute(select(Bill)).scalars().all()) == 1  # nothing until applied
    a.post(f'/bills/import/{name}/apply', data=MAP)
    with env.db() as db:
        bills = {b.name: b for b in db.execute(select(Bill)).scalars()}
        assert set(bills) == {'Council rates', 'Home insurance', 'Water'}
        water = bills['Water']
        # Matched by name: updated, not duplicated, and the file's due date taken
        # only when it gives one (the template's Water has none).
        assert water.amount_cents == 310_00 and water.next_due == date(2026, 10, 10)
        assert {s.user_id for s in water.shares} == {alex.id, partner.id}
        rates = bills['Council rates']
        assert rates.next_due == date(2026, 11, 15) and len(rates.shares) == 1
        mine = next(s for s in bills['Home insurance'].shares if s.user_id == alex.id)
        insurance = db.execute(select(Category).where(Category.id == mine.category_id)).scalar_one()
        assert insurance.name == 'Insurance'
    # Re-import with one changed and one left out, which is stopped on request.
    second = '\n'.join(first.splitlines()[:3]).replace('520.00', '560.00') + '\n'
    name = upload_bills(a, 'h2.csv', second.encode())
    diff = a.get(f'/bills/import/{name}').text
    assert 'Changed (1)' in diff and 'Not in the file (1)' in diff and 'Added (0)' in diff
    a.post(f'/bills/import/{name}/apply', data={'remove_missing': 'on', **MAP})
    with env.db() as db:
        bills = {b.name: b for b in db.execute(select(Bill)).scalars()}
        assert bills['Council rates'].amount_cents == 560_00
        assert bills['Water'].active is False and bills['Home insurance'].active is True


def test_bill_import_keeps_a_split_the_file_cannot_describe(env: Env) -> None:
    alex = env.user('alex')
    sam = env.user('sam')
    kim = env.user('kim')
    a = env.login(alex)
    create_bill(
        a,
        name='Internet',
        amount='90',
        next_due='2026-10-12',
        participants=[str(sam.id), str(kim.id)],
    )
    name = upload_bills(a, 'i.csv', b'name,amount,frequency\nInternet,99,monthly\n')
    assert 'shared (kept as it is)' in a.get(f'/bills/import/{name}').text
    a.post(
        f'/bills/import/{name}/apply',
        data={'map_name': '0', 'map_amount': '1', 'map_frequency': '2'},
    )
    with env.db() as db:
        bill = db.execute(select(Bill)).scalar_one()
        assert bill.amount_cents == 99_00 and len(bill.shares) == 3


def test_bill_import_leaves_what_the_file_does_not_mention(env: Env) -> None:
    alex = env.user('alex')
    sam = env.user('sam')
    a = env.login(alex)
    utilities = cat_id(env, alex, 'Utilities')
    create_bill(
        a,
        name='Power',
        amount='200',
        next_due='2026-10-20',
        category_id=str(utilities),
        participants=[str(sam.id)],
    )
    # No category or sharing columns: only the amount moves.
    name = upload_bills(a, 'p.csv', b'name,amount,frequency\nPower,240,monthly\n')
    preview = a.get(f'/bills/import/{name}').text
    assert 'Changed (1)' in preview
    a.post(
        f'/bills/import/{name}/apply',
        data={'map_name': '0', 'map_amount': '1', 'map_frequency': '2'},
    )
    with env.db() as db:
        bill = db.execute(select(Bill)).scalar_one()
        assert bill.amount_cents == 240_00 and bill.next_due == date(2026, 10, 20)
        assert {s.user_id for s in bill.shares} == {alex.id, sam.id}
        mine = next(s for s in bill.shares if s.user_id == alex.id)
        assert mine.category_id == utilities
    # The same file again changes nothing.
    name = upload_bills(a, 'p.csv', b'name,amount,frequency\nPower,240,monthly\n')
    assert 'Nothing would change' in a.get(f'/bills/import/{name}').text


def test_bill_csv_with_other_headers_needs_mapping(env: Env) -> None:
    a = env.login(env.user('alex'))
    name = upload_bills(a, 'o.csv', b'Item,Cost,Every,Group\nStrata,1200,quarterly,Housing\n')
    assert 'Map the columns' in a.get(f'/bills/import/{name}').text
    preview = a.get(f'/bills/import/{name}?map_name=0&map_amount=1&map_frequency=2&map_category=3')
    assert 'Added (1)' in preview.text and 'Strata' in preview.text


def test_bill_json_import_takes_the_old_housing_format(env: Env) -> None:
    a = env.login(env.user('alex'))
    doc = {
        'items': [
            {'name': 'Water', 'amount': '310', 'frequency': 'quarterly'},
            {'name': 'Bad', 'amount': 'x', 'frequency': 'monthly'},
            {'name': 'Bad2', 'amount': '5', 'frequency': 'sometimes'},
        ]
    }
    name = upload_bills(a, 'h.json', json.dumps(doc).encode(), 'application/json')
    page = a.get(f'/bills/import/{name}').text
    assert 'Added (1)' in page and '2 rows skipped' in page


def test_another_users_staged_bill_file_is_unreachable(env: Env) -> None:
    a = env.login(env.user('alex'))
    b = env.login(env.user('blake'))
    name = upload_bills(a, 'h.csv', b'name,amount,frequency\nX,1,monthly\n')
    assert b.get(f'/bills/import/{name}').status_code == 404
    assert b.post(f'/bills/import/{name}/apply').status_code == 404
    assert a.get('/bills/import/../../fiscus.db').status_code == 404


def test_future_payslip_is_one_off_income(env: Env) -> None:
    a, alex, acct = setup_household(env)
    before = chart_data(a.get('/forecast?months=1').text)
    # A bonus paid on 20 Oct, and one already paid (in the balance already).
    a.post(
        '/payslips',
        data={
            'date': '2026-10-20',
            'source': 'Acme bonus',
            'gross': '5000',
            'tax_withheld': '1500',
            'net': '3500',
            'account_id': str(acct),
        },
    )
    a.post(
        '/payslips',
        data={'date': '2026-09-20', 'gross': '1000', 'tax_withheld': '0', 'net': '1000'},
    )
    after = chart_data(a.get('/forecast?months=1').text)
    i19, i20 = after['dates'].index('2026-10-19'), after['dates'].index('2026-10-20')
    assert after['combined'][i19] == before['combined'][i19]
    assert after['combined'][i20] == before['combined'][i20] + 3500_00
    assert any(m['label'] == 'Acme bonus' and m['kind'] == 'pay' for m in after['markers'])
    page = a.get('/pay').text
    assert 'Expected' in page
    cal = a.get('/calendar?view=agenda&date=2026-10-01').text
    assert 'Acme bonus' in cal
    with env.db() as db:
        user = db.get(User, alex.id)
        assert user is not None
        tv = taxes.view(db, user, env.clock.today())
    # Past payslip only in the year to date; the bonus adds to the projection.
    assert tv.ytd.income_cents == 1000_00
    with env.db() as db:
        db.execute(delete(Payslip).where(Payslip.source == 'Acme bonus'))
        db.commit()
        user = db.get(User, alex.id)
        assert user is not None
        without = taxes.view(db, user, env.clock.today())
    assert tv.projected.income_cents - without.projected.income_cents == 5000_00


# ----------------------------------------------------------------------- tax


def test_tax_page_estimates_and_set_aside(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    set_aside = make_account(a, 'Tax pot', 'savings', '1000')
    with env.db() as db:
        from fiscus.models import Account

        acc = db.get(Account, set_aside)
        assert acc is not None
        acc.is_set_aside = True
        db.commit()
    a.post(
        '/payslips',
        data={'date': '2026-07-15', 'gross': '20000', 'tax_withheld': '4000', 'net': '16000'},
    )
    page = a.get('/tax').text
    assert 'Tax 2026-27' in page and 'Estimate' in page and 'not tax advice' in page
    assert 'Income (gross)' in page and 'Sole trader' not in page and 'Business' not in page
    assert 'Low income tax offset' not in page
    # Taxable 20,000: (20,000 - 18,200) x 15 % = 270, no Medicare, no LITO:
    # 270 against 4,000 withheld, a refund of 3,730.
    assert 'Estimated refund' in page and '$3,730.00' in page
    # Nothing owing so far: set-aside more than covers it.
    assert 'That covers it' in page


def test_set_aside_targets_the_projected_year_end() -> None:
    from fiscus.domain import tax

    fy = tax.load_year('2026-27')
    est = tax.estimate(fy, 100_000_00)
    # 7,300 still to pay at 30 June beyond withholding.
    projected = tax.position(fy, 100_000_00, est.total_cents - 7_300_00, 0)
    ytd = tax.position(fy, 25_000_00, 5_000_00, 0)
    v = taxes.TaxView(fy, ytd, projected, 1_000_00, 73, 365)
    assert v.year_end_set_aside_cents == 7_300_00
    assert v.should_set_aside_cents == 1_460_00  # 73 of 365 days
    assert v.set_aside_gap_cents == -460_00
    # 6,300 left over 42 weeks.
    assert v.left_to_save_per_week_cents == 150_00
    # A refund needs nothing put away.
    refund = tax.position(fy, 100_000_00, est.total_cents + 1_00, 0)
    v = taxes.TaxView(fy, ytd, refund, 0, 73, 365)
    assert v.should_set_aside_cents == 0 and v.left_to_save_per_week_cents == 0


def test_tax_deductions_are_saved_and_come_off_income(env: Env) -> None:
    a = env.login(env.user('alex'))
    b = env.login(env.user('blake'))
    a.post(
        '/payslips',
        data={'date': '2026-07-15', 'gross': '20000', 'tax_withheld': '4000', 'net': '16000'},
    )
    assert a.post('/tax/deductions', data={'amount': '1,500.50'}).status_code in (200, 303)
    page = a.get('/tax').text
    assert 'value="1500.50"' in page
    assert '$18,499.50' in page  # taxable income
    # Updating replaces the figure rather than adding to it.
    a.post('/tax/deductions', data={'amount': '2000'})
    page = a.get('/tax').text
    assert 'value="2000.00"' in page and '$18,000.00' in page
    # A bad or negative amount changes nothing.
    a.post('/tax/deductions', data={'amount': '-5'})
    a.post('/tax/deductions', data={'amount': 'lots'})
    assert 'value="2000.00"' in a.get('/tax').text
    # Clearing the box sets it back to nothing.
    a.post('/tax/deductions', data={'amount': ''})
    assert '$20,000.00' in a.get('/tax').text
    # Someone else's figure is their own.
    a.post('/tax/deductions', data={'amount': '2000'})
    assert 'value="2000.00"' not in b.get('/tax').text


# -------------------------------------------------------------- dashboard etc


def test_every_page_renders(env: Env) -> None:
    a, _alex, acct = setup_household(env)
    import_csv(a, acct, ANZ_SEPT)
    for page in [
        '/',
        '/more',
        '/accounts',
        f'/accounts/{acct}',
        f'/accounts/{acct}/edit',
        '/accounts/new',
        '/transactions',
        '/transactions?category_id=none',
        '/import',
        '/categories',
        '/recategorise',
        '/bills',
        '/bills/new',
        '/pay',
        '/calendar',
        '/loans',
        '/loans/new',
        '/forecast',
        '/forecast?months=24',
        '/scenarios',
        '/budget',
        '/bills/import',
        f'/bills?category={acct}',
        '/tax',
        '/settings',
    ]:
        resp = a.get(page)
        assert resp.status_code == 200, f'{page}: {resp.status_code}'
        assert 'style="' not in resp.text, f'{page} has an inline style the CSP would block'
        assert resp.headers['cache-control'] == 'no-store'


def test_dashboard_contents(env: Env) -> None:
    a, *_ = setup_household(env)
    dash = a.get('/').text
    assert 'All accounts' in dash and '$3,000.00' in dash
    assert 'Next 14 days' in dash and 'Rent' in dash and 'Acme wages' in dash
    assert '<polyline' in dash
    assert 'Tax set aside' in dash and 'Offset saved' in dash


def test_loan_list_cards_and_a_loan_not_yet_settled(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    resp = a.post(
        '/loans',
        data={
            'name': 'Leda',
            'principal': '606980',
            'start_date': '2027-01-04',
            'term_years': '30',
            'initial_rate': '6.34',
            'balance': '606980',
            'balance_date': '2026-10-01',
        },
    )
    loan = int(resp.headers['location'].rsplit('/', 1)[1])
    page = a.get('/loans').text
    assert '<a class="card"' not in page  # an inline link wrapping a card broke the layout
    assert '$3,772.88' in page and 'Loan starts 4 Jan 2027' in page
    assert 'Savings count from then' in a.get(f'/loans/{loan}').text
    assert 'Loan starts 4 Jan 2027' in a.get('/').text


def test_a_bimonthly_bill_in_bills_and_budget(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    utilities = cat_id(env, alex, 'Utilities')
    bill = create_bill(
        a,
        name='Water',
        amount='180',
        frequency='bimonthly',
        next_due='2026-10-20',
        category_id=str(utilities),
    )
    assert 'Every 2 months' in a.get(f'/bills/{bill}').text
    cal = a.get('/calendar?view=agenda&date=2026-12-01').text
    assert 'Water' in cal
    # 90 a month; the budget form suggests saving it up every 2 months.
    page = a.get('/budget?month=2026-10').text
    assert 'Bills here come to $90.00 a month' in page
    assert 'value="180.00"' in page and '<option value="2" selected>' in page
    a.post('/budget', data={'category_id': str(utilities), 'amount': '180', 'cycle_months': '2'})
    with env.db() as db:
        row = db.execute(select(Budget)).scalar_one()
        assert (row.monthly_cents, row.cycle_months) == (90_00, 2)


def test_a_yearly_line_reads_back_and_saves_exactly_what_was_typed(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    a.post(
        '/budget', data={'new_category': 'Council rates', 'amount': '2480', 'cycle_months': '12'}
    )
    page = a.get('/budget?month=2026-10').text
    assert 'Saving up $2,480.00 every year' in page
    assert 'value="2480.00"' in page
    # Twelve budget months save $2,480.00, not 12 x $206.67.
    assert _saved(env, alex, date(2027, 9, 1))['Council rates'] == (2480_00, 'ok')


def test_an_overspent_line_shows_a_red_bar(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    a.post('/budget', data={'new_category': 'Eating out', 'amount': '250'})
    cid = cat_id(env, alex, 'Eating out')
    a.post('/budget/spend', data={'category_id': str(cid), 'amount': '250'})
    page = a.get('/budget').text
    # At the budget: amber, as from 90 %.
    assert 'low="22500" high="25000" optimum="0" value="25000"' in page
    a.post('/budget/spend', data={'category_id': str(cid), 'amount': '10'})
    page = a.get('/budget').text
    # Over it: the value sits above `high`, which the meter draws red.
    assert 'low="22500" high="24999" optimum="0" value="25000"' in page


def test_tax_projection_uses_each_jobs_own_figures(env: Env) -> None:
    a = env.login(env.user('alex'))
    acct = make_account(a)
    # A salaried job with PAYG, and contract pay with no figures at all.
    a.post(
        '/pay/schedules',
        data={
            'name': 'Acme',
            'frequency': 'fortnightly',
            'anchor_date': '2026-10-08',
            'net': '3000',
            'gross': '4000',
            'tax_withheld': '1000',
            'account_id': str(acct),
        },
    )
    a.post(
        '/pay/schedules',
        data={
            'name': 'Consulting',
            'frequency': 'quarterly',
            'anchor_date': '2026-12-30',
            'net': '4500',
            'account_id': str(acct),
        },
    )
    with env.db() as db:
        u = db.execute(select(User).where(User.username == 'alex')).scalar_one()
        v = taxes.view(db, u, date(2026, 10, 1), env.ctx.tax_dir)
    acme = 19  # 8 Oct to 24 Jun, fortnightly
    consulting = 3  # 30 Dec, 30 Mar, 30 Jun
    assert v.projected.income_cents == acme * 4000_00 + consulting * 4500_00
    # Consulting's pay counts as having nothing withheld, and the page says so.
    assert v.projected.withheld_cents == acme * 1000_00
    assert v.untaxed == ('Consulting',)
    page = a.get('/tax').text
    assert 'Consulting has no gross or tax figures' in page
    # Once Consulting has a payslip of its own, its figures are used.
    a.post(
        '/payslips',
        data={
            'date': '2026-09-30',
            'gross': '6000',
            'tax_withheld': '1500',
            'net': '4500',
            'payday': '',
        },
    )
    with env.db() as db:
        sid = db.execute(select(PaySchedule.id).where(PaySchedule.name == 'Consulting')).scalar()
        slip = db.execute(select(Payslip)).scalar_one()
        slip.schedule_id, slip.payday = sid, date(2026, 9, 30)
        db.commit()
    with env.db() as db:
        u = db.execute(select(User).where(User.username == 'alex')).scalar_one()
        v = taxes.view(db, u, date(2026, 10, 1), env.ctx.tax_dir)
    assert v.untaxed == ()
    assert v.projected.withheld_cents == acme * 1000_00 + 1500_00 + consulting * 1500_00


def test_tax_page_lists_paydays_without_a_payslip(env: Env) -> None:
    a = env.login(env.user('alex'))
    a.post(
        '/pay/schedules',
        data={
            'name': 'Acme',
            'frequency': 'fortnightly',
            'anchor_date': '2026-09-03',
            'net': '3000',
        },
    )
    page = a.get('/tax').text
    # 3 and 17 Sep have passed with no payslip; 1 Oct is today and is not asked yet.
    assert '2 paydays without a payslip' in page
    assert '/pay?payday=' in page and '3 Sep 2026' in page and '17 Sep 2026' in page
    a.post('/pay/schedules/1/no-pay', data={'payday': '2026-09-03'})
    assert '1 payday without a payslip' in a.get('/tax').text


def test_offset_savings_use_the_balance_known_on_each_day(env: Env) -> None:
    alex = env.user('alex')
    # The offset is first entered on 1 Sep at $10,000 and updated to $30,000
    # on 15 Sep. Today is 1 Oct.
    env.clock.set(datetime(2026, 9, 1, 2, tzinfo=UTC))
    a = env.login(alex)
    resp = a.post('/accounts', data={'name': 'Offset', 'type': 'offset', 'balance': '10000'})
    offset = int(resp.headers['location'].rsplit('/', 1)[1])
    resp = a.post(
        '/loans',
        data={
            'name': 'Home',
            'principal': '500000',
            'start_date': '2024-01-15',
            'initial_rate': '6',
            'balance': '480000',
            'balance_date': '2026-09-01',
        },
    )
    loan_id = int(resp.headers['location'].rsplit('/', 1)[1])
    a.post(f'/loans/{loan_id}/offsets', data={'account_id': str(offset)})
    env.clock.set(datetime(2026, 9, 15, 2, tzinfo=UTC))
    a.post(f'/accounts/{offset}/balance', data={'balance': '30000'})
    env.clock.set(datetime(2026, 10, 1, 2, tzinfo=UTC))
    from fiscus.services import loans as loan_service

    with env.db() as db:
        u = db.get(User, alex.id)
        loan = db.get(Loan, loan_id)
        assert u is not None and loan is not None
        s = loan_service.summarise(db, u, loan, date(2026, 10, 1))
    # 1 to 14 Sep on $10,000 and 15 Sep to 1 Oct on $30,000, at 6 %; nothing
    # before 1 Sep, when the offset was first entered.
    expected = (14 * 10_000_00 + 17 * 30_000_00) * Decimal('0.06') / 365
    assert s.saved_year_cents == L.cents(expected)
    assert s.saved_year_from == date(2026, 9, 1)
    assert s.saved_month_cents == L.cents(30_000_00 * Decimal('0.06') / 365)
    assert s.saved_month_from is None
    page = a.get(f'/loans/{loan_id}').text
    assert "since 1 Sep, when the offset's balance was first entered" in page


def test_an_overdue_bill_shows_on_the_dashboard_budget_and_agenda(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    acct = make_account(a)
    a.post(
        '/budget',
        data={
            'new_category': 'Council rates',
            'amount': '2480',
            'cycle_months': '12',
            'saved': '620',
        },
    )
    cid = cat_id(env, alex, 'Council rates')
    bill = create_bill(
        a,
        name='Council rates',
        amount='620',
        frequency='quarterly',
        next_due='2026-09-28',
        account_id=str(acct),
        category_id=str(cid),
    )
    dash = a.get('/').text
    assert 'id="overdue"' in dash and 'was due 28 Sep 2026' in dash
    assert f'/bills/{bill}/occurrence/2026-09-28/paid' in dash and 'value="/"' in dash
    budget = a.get('/budget').text
    assert '$620.00 overdue since 28 Sep 2026: the line holds it' in budget
    agenda = a.get('/calendar?view=agenda').text
    assert 'id="cal-overdue"' in agenda and 'Council rates, due 28 Sep' in agenda
    # Marked paid from the dashboard, it is gone everywhere.
    resp = a.post(f'/bills/{bill}/occurrence/2026-09-28/paid', data={'back': '/'})
    assert resp.headers['location'] == '/'
    assert 'id="overdue"' not in a.get('/').text
    assert 'id="cal-overdue"' not in a.get('/calendar?view=agenda').text
    assert 'overdue since' not in a.get('/budget').text
