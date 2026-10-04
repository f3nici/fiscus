"""Calendar, loans, forecast caching, budget, bill import, tax and the dashboard."""

from __future__ import annotations

import html
import json
import re
from datetime import date
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
    Payslip,
    User,
)
from fiscus.services import budgeting, forecasting, ledger, taxes
from tests.conftest import CsrfClient, Env
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
    page = a.get('/budget').text
    assert '$200.00 of bills' in page and 'bills paid' not in page
    a.post(f'/bills/{bill}/occurrence/2026-10-20/paid')
    page = a.get('/budget').text
    assert '$200.00 bills paid' in page and 'nothing over' in page


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


def test_bills_count_towards_their_category_budget(env: Env) -> None:
    alex = env.user('alex')
    sam = env.user('sam')
    a = env.login(alex)
    housing = cat_id(env, alex, 'Housing')
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
    # My half of 520 a quarter is 86.67 a month: that is the budget, nothing stored.
    page = a.get('/budget?month=2026-10').text
    assert '$86.67' in page and 'Includes $86.67 of bills' in page
    with env.db() as db:
        assert db.execute(select(Budget)).first() is None
    assert '-$260.00' in env.login(sam).get('/calendar?view=agenda&date=2026-11-01').text
    # A total above the bills keeps only the rest.
    a.post('/budget', data={'category_id': str(housing), 'amount': '150'})
    with env.db() as db:
        assert db.execute(select(Budget.monthly_cents)).scalar_one() == 63_33
    assert '$150.00' in a.get('/budget?month=2026-10').text
    # Below the bills: the bills are the budget.
    resp = a.post(
        '/budget', data={'category_id': str(housing), 'amount': '50'}, follow_redirects=True
    )
    assert 'come to $86.67 a month' in resp.text
    with env.db() as db:
        assert db.execute(select(Budget)).first() is None
    # The bills page shows the same monthly figure, filtered by category.
    bills_page = a.get(f'/bills?category={housing}').text
    assert 'Bills: Housing' in bills_page and '$86.67' in bills_page


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
    assert tv.ytd.salary_gross_cents == 1000_00
    with env.db() as db:
        db.execute(delete(Payslip).where(Payslip.source == 'Acme bonus'))
        db.commit()
        user = db.get(User, alex.id)
        assert user is not None
        without = taxes.view(db, user, env.clock.today())
    assert tv.projected.salary_gross_cents - without.projected.salary_gross_cents == 5000_00


# ----------------------------------------------------------------------- tax


def test_tax_page_estimates_and_set_aside(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    acct = make_account(a)
    set_aside = make_account(a, 'Tax pot', 'savings', '1000')
    with env.db() as db:
        from fiscus.models import Account

        acc = db.get(Account, set_aside)
        assert acc is not None
        acc.is_set_aside = True
        db.commit()
        cats = {
            c.name: c.id
            for c in db.execute(select(Category).where(Category.user_id == alex.id)).scalars()
        }
    a.post('/rules', data={'pattern': 'invoice', 'category_id': str(cats['Business income'])})
    a.post('/rules', data={'pattern': 'officeworks', 'category_id': str(cats['Business expenses'])})
    import_csv(a, acct, '10/08/2026,"6000.00","INVOICE 001"\n12/08/2026,"-1000.00","OFFICEWORKS"\n')
    a.post(
        '/payslips',
        data={'date': '2026-07-15', 'gross': '20000', 'tax_withheld': '4000', 'net': '16000'},
    )
    page = a.get('/tax').text
    assert 'Tax 2026-27' in page and 'Estimate' in page and 'not tax advice' in page
    assert '$5,000.00' in page  # sole trader profit
    # Taxable 25,000: (25,000 - 18,200) x 15 % = 1,020, less LITO 700, no Medicare:
    # total 320 against 4,000 withheld, a refund.
    assert 'Estimated refund' in page
    # Nothing owing so far: set-aside more than covers it.
    assert 'That covers it' in page


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
