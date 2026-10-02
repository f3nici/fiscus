"""Calendar, loans, forecast caching, budget, housing costs, tax and the dashboard."""

from __future__ import annotations

import json
import re
from datetime import date

from sqlalchemy import select

from fiscus.models import Bill, Budget, Category, ForecastCache, HousingItem, User
from fiscus.services import forecasting
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


# ------------------------------------------------------------------ forecast


def test_forecast_page_and_cache_invalidation(env: Env) -> None:
    a, alex, acct = setup_household(env)
    page = a.get('/forecast?months=3').text
    data = json.loads(re.search(r'id="forecast-data">(.*?)</script>', page, re.S).group(1))  # type: ignore[union-attr]
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
    data = json.loads(re.search(r'id="forecast-data">(.*?)</script>', page, re.S).group(1))  # type: ignore[union-attr]
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
    data = json.loads(re.search(r'id="forecast-data">(.*?)</script>', page, re.S).group(1))  # type: ignore[union-attr]
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


def test_budget_vs_actual(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    acct = make_account(a)
    with env.db() as db:
        groceries = (
            db.execute(
                select(Category).where(Category.user_id == alex.id, Category.name == 'Groceries')
            )
            .scalar_one()
            .id
        )
    a.post('/rules', data={'pattern': 'coles', 'category_id': str(groceries)})
    import_csv(a, acct, '01/10/2026,"-450.00","COLES"\n')
    a.post('/budget', data={'category_id': str(groceries), 'amount': '400'})
    page = a.get('/budget?month=2026-10').text
    assert 'Groceries' in page and '$450.00' in page and '-$50.00' in page
    assert 'Categories over' in page
    assert a.get('/budget?month=2026-09').status_code == 200


# ------------------------------------------------------------------- housing


def test_housing_manual_item_becomes_budget_and_shared_bill(env: Env) -> None:
    alex = env.user('alex')
    sam = env.user('sam')
    a = env.login(alex)
    resp = a.post(
        '/housing',
        data={
            'name': 'Council rates',
            'amount': '520',
            'frequency': 'quarterly',
            'category': 'Rates',
            'due_date': '2026-11-15',
            'shared_with': 'sam',
            'split_percent': '50',
        },
    )
    assert resp.status_code == 303
    with env.db() as db:
        item = db.execute(select(HousingItem)).scalar_one()
        bill = db.execute(select(Bill).where(Bill.housing_item_id == item.id)).scalar_one()
        assert bill.frequency == 'quarterly' and bill.next_due == date(2026, 11, 15)
        assert sorted(s.user_id for s in bill.shares) == sorted([alex.id, sam.id])
        budget = db.execute(select(Budget).where(Budget.user_id == alex.id)).scalar_one()
        # My half of 520 a quarter is 260, which is 86.67 a month.
        assert budget.monthly_cents == 86_67
    assert '-$260.00' in env.login(sam).get('/calendar?view=agenda&date=2026-11-01').text
    # Editing the item updates the bill and the budget.
    a.post(
        f'/housing/{item.id}',
        data={
            'name': 'Council rates',
            'amount': '600',
            'frequency': 'quarterly',
            'category': 'Rates',
            'due_date': '2026-11-15',
        },
    )
    with env.db() as db:
        bill = db.execute(select(Bill).where(Bill.housing_item_id == item.id)).scalar_one()
        assert bill.amount_cents == 600_00 and len(bill.shares) == 1
        assert (
            db.execute(select(Budget).where(Budget.user_id == alex.id)).scalar_one().monthly_cents
            == 200_00
        )


def test_housing_templates_download(env: Env) -> None:
    a = env.login(env.user('alex'))
    csv_t = a.get('/housing/template.csv')
    assert csv_t.status_code == 200 and csv_t.text.startswith(
        'name,amount,frequency,category,due_date'
    )
    js = a.get('/housing/template.json')
    assert json.loads(js.text)['items'][0]['name'] == 'Council rates'


def test_housing_csv_import_preview_and_reimport_diff(env: Env) -> None:
    env.user('partner')
    a = env.login(env.user('alex'))
    first = a.get('/housing/template.csv').text
    resp = a.post('/housing/import', files={'file': ('h.csv', first.encode(), 'text/csv')})
    name = resp.headers['location'].rsplit('/', 1)[1]
    preview = a.get(f'/housing/import/{name}')
    assert 'Added (3)' in preview.text
    with env.db() as db:
        assert db.execute(select(HousingItem)).first() is None  # nothing until applied
    a.post(
        f'/housing/import/{name}/apply',
        data={
            f'map_{f}': str(i)
            for i, f in enumerate(
                [
                    'name',
                    'amount',
                    'frequency',
                    'category',
                    'due_date',
                    'shared_with',
                    'split_percent',
                ]
            )
        },
    )
    with env.db() as db:
        assert len(db.execute(select(HousingItem)).scalars().all()) == 3
    # Re-import with one changed, one removed.
    second = '\n'.join(first.splitlines()[:3]).replace('520.00', '560.00') + '\n'
    resp = a.post('/housing/import', files={'file': ('h2.csv', second.encode(), 'text/csv')})
    name = resp.headers['location'].rsplit('/', 1)[1]
    diff = a.get(f'/housing/import/{name}').text
    assert 'Changed (1)' in diff and 'Not in the file (1)' in diff and 'Added (0)' in diff
    a.post(
        f'/housing/import/{name}/apply',
        data={
            'remove_missing': 'on',
            **{
                f'map_{f}': str(i)
                for i, f in enumerate(
                    [
                        'name',
                        'amount',
                        'frequency',
                        'category',
                        'due_date',
                        'shared_with',
                        'split_percent',
                    ]
                )
            },
        },
    )
    with env.db() as db:
        items = {i.name: i.amount_cents for i in db.execute(select(HousingItem)).scalars()}
        assert items == {'Council rates': 560_00, 'Home insurance': 1850_00}
        water = db.execute(select(Bill).where(Bill.name == 'Water')).scalar_one()
        assert water.active is False


def test_housing_csv_with_other_headers_needs_mapping(env: Env) -> None:
    a = env.login(env.user('alex'))
    text = 'Item,Cost,Every,Group\nStrata,1200,quarterly,Housing\n'
    name = (
        a.post('/housing/import', files={'file': ('o.csv', text.encode(), 'text/csv')})
        .headers['location']
        .rsplit('/', 1)[1]
    )
    assert 'Map the columns' in a.get(f'/housing/import/{name}').text
    preview = a.get(
        f'/housing/import/{name}?map_name=0&map_amount=1&map_frequency=2&map_category=3'
    )
    assert 'Added (1)' in preview.text and 'Strata' in preview.text


def test_housing_json_import_and_errors(env: Env) -> None:
    a = env.login(env.user('alex'))
    doc = {
        'items': [
            {'name': 'Water', 'amount': '310', 'frequency': 'quarterly'},
            {'name': 'Bad', 'amount': 'x', 'frequency': 'monthly'},
            {'name': 'Bad2', 'amount': '5', 'frequency': 'sometimes'},
        ]
    }
    name = (
        a.post(
            '/housing/import',
            files={'file': ('h.json', json.dumps(doc).encode(), 'application/json')},
        )
        .headers['location']
        .rsplit('/', 1)[1]
    )
    page = a.get(f'/housing/import/{name}').text
    assert 'Added (1)' in page and '2 rows skipped' in page


def test_another_users_staged_housing_file_is_unreachable(env: Env) -> None:
    a = env.login(env.user('alex'))
    b = env.login(env.user('blake'))
    name = (
        a.post(
            '/housing/import',
            files={'file': ('h.csv', b'name,amount,frequency\nX,1,monthly\n', 'text/csv')},
        )
        .headers['location']
        .rsplit('/', 1)[1]
    )
    assert b.get(f'/housing/import/{name}').status_code == 404
    assert b.post(f'/housing/import/{name}/apply').status_code == 404
    assert a.get('/housing/import/../../fiscus.db').status_code == 404


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
        '/housing',
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
