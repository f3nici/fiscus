"""Importing a budget spreadsheet (XLSX or CSV) into monthly budgets."""

from __future__ import annotations

import io
from typing import Any

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from fiscus.domain import budget_import as dom
from fiscus.models import Budget, BudgetSpend, Category
from fiscus.services import budget_import as svc
from tests.conftest import CsrfClient, Env


def household_xlsx() -> bytes:
    """The shape of a real household running-costs sheet: an assumptions tab
    first, then the budget with a title, a summary, sections and totals."""
    wb = Workbook()
    a = wb.active
    assert a is not None
    a.title = 'Assumptions'
    a.append(['Assumptions & Rate Inputs'])
    a.append(['Loan amount', 606980])
    b = wb.create_sheet('Monthly Budget')
    b.append(['  House Running Costs'])
    b.append(['SUMMARY'])
    b.append(['Total cost per month', None, None, 7667.71])
    b.append(['Item', 'Amount', 'Frequency', 'Monthly cost', 'Notes & assumptions'])
    b.append(['HOME & PROPERTY'])
    b.append(['Mortgage repayment', 3772.881689016039, 'Monthly', None, 'P&I'])
    b.append(['Council rates', 2960, 'Annual', None, 'Replace once the notice arrives.'])
    b.append(['Building & contents insurance', 1253.92, 'Annual'])
    b.append(['Water usage', '=Assumptions!B9', 'Annual'])  # formula, no cached value
    b.append([])
    b.append(['FOOD & HOUSEHOLD'])
    b.append(['Groceries', 240, 'Weekly'])
    b.append(['Takeaway & eating out', 150, 'Monthly'])
    b.append(['TRANSPORT'])
    b.append(['Fuel', 100, 'Weekly'])
    b.append(['Vehicle registration & CTP', 1109, 'Annual'])
    b.append(['TOTAL COST PER MONTH', None, None, 7667.71])
    b.append(['ONE-OFF SETTLEMENT COSTS — paid once, NOT part of the monthly total above'])
    b.append(['Transfer duty (stamp duty)', None, 'One-off', 13243])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# --------------------------------------------------------------------- domain


def test_finds_the_budget_sheet_and_header() -> None:
    parsed = svc.parse(household_xlsx(), 'x.xlsx', None, has_loan=False)
    assert parsed.sheet == 'Monthly Budget'
    assert parsed.sheets == ['Assumptions', 'Monthly Budget']
    names = [line.name for line in parsed.lines]
    assert names == [
        'Mortgage repayment',
        'Council rates',
        'Building & contents insurance',
        'Water usage',
        'Groceries',
        'Takeaway & eating out',
        'Fuel',
        'Vehicle registration & CTP',
        'Transfer duty (stamp duty)',
    ]
    by = {line.name: line for line in parsed.lines}
    assert by['Groceries'].section == 'Food & household'
    assert by['Council rates'].section == 'Home & property'
    # A heading that mentions "total" is still a heading, not a total row.
    assert by['Transfer duty (stamp duty)'].section == 'One-off settlement costs'


def test_monthly_normalisation_in_cents() -> None:
    by = {
        line.name: line
        for line in svc.parse(household_xlsx(), 'x.xlsx', None, has_loan=False).lines
    }
    assert by['Mortgage repayment'].amount_cents == 3772_88
    assert by['Council rates'].monthly_cents == 246_67  # 2,960 / 12
    assert by['Building & contents insurance'].monthly_cents == 104_49  # 1,253.92 / 12
    assert by['Groceries'].monthly_cents == 1040_00  # 240 x 52 / 12
    assert by['Takeaway & eating out'].monthly_cents == 150_00


def test_exclusions_explain_themselves() -> None:
    by = {
        line.name: line for line in svc.parse(household_xlsx(), 'x.xlsx', None, has_loan=True).lines
    }
    assert not by['Transfer duty (stamp duty)'].include
    assert 'One-off' in by['Transfer duty (stamp duty)'].reason
    assert not by['Water usage'].include and 'No amount' in by['Water usage'].reason
    assert not by['Mortgage repayment'].include and 'home loan' in by['Mortgage repayment'].reason
    without_loan = {
        line.name: line
        for line in svc.parse(household_xlsx(), 'x.xlsx', None, has_loan=False).lines
    }
    assert without_loan['Mortgage repayment'].include


def test_csv_works_too() -> None:
    parsed = svc.parse(svc.template_csv().encode(), 'b.csv', None, has_loan=False)
    assert [(line.name, line.monthly_cents) for line in parsed.lines] == [
        ('Groceries', 1040_00),
        ('Internet', 79_00),
        ('Council rates', 246_67),
        ('Car rego', 92_42),
    ]


def test_daily_and_unknown_frequencies() -> None:
    rows: list[list[Any]] = [
        ['Item', 'Amount', 'Frequency'],
        ['Coffee', 5, 'Daily'],
        ['Odd', 9, 'Sometimes'],
    ]
    lines = dom.parse(rows, dom.find_header(rows))  # type: ignore[arg-type]
    assert lines[0].monthly_cents == 152_08  # 5 x 365 / 12
    assert not lines[1].include and 'not recognised' in lines[1].reason


def test_every_few_months_keeps_its_cycle() -> None:
    rows: list[list[Any]] = [
        ['Item', 'Amount', 'Frequency'],
        ['Water', 180, 'Bi-monthly'],
        ['Power', 300, '2 monthly'],
        ['Rates', 600, 'Quarterly'],
        ['Water', 300, '3 monthly'],
        ['Pest control', 240, 'Every 3 months'],
        ['Car service', 600, '6 monthly'],
        ['Gutters', 300, 'Half-yearly'],
        ['Rego', 1200, 'Annual'],
        ['Insurance', 1200, 'p.a.'],
        ['Phone', 50, 'Monthly'],
    ]
    lines = dom.parse(rows, dom.find_header(rows))  # type: ignore[arg-type]
    assert [(line.name, line.monthly_cents, line.cycle_months) for line in lines] == [
        ('Water', 90_00, 2),
        ('Power', 150_00, 2),
        ('Rates', 200_00, 3),
        ('Water', 100_00, 3),
        ('Pest control', 80_00, 3),
        ('Car service', 100_00, 6),
        ('Gutters', 50_00, 6),
        ('Rego', 100_00, 12),
        ('Insurance', 100_00, 12),
        ('Phone', 50_00, 1),
    ]


def test_a_sheet_without_headings_is_refused() -> None:
    from fiscus.domain.csvimport import ImportError_

    with pytest.raises(ImportError_, match='Item, Amount and Frequency'):
        svc.parse(b'a,b,c\n1,2,3\n', 'x.csv', None, has_loan=False)


def test_not_a_spreadsheet() -> None:
    from fiscus.domain.csvimport import ImportError_

    with pytest.raises(ImportError_):
        svc.read_sheets(b'PK\x03\x04garbage', 'x.xlsx')


# ---------------------------------------------------------------------- routes


def upload(client: CsrfClient, data: bytes, name: str = 'budget.xlsx') -> str:
    resp = client.post('/budget/import', files={'file': (name, data, 'application/octet-stream')})
    assert resp.status_code == 303, resp.text
    return str(resp.headers['location'])


def test_import_end_to_end(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    assert 'Import spreadsheet' in a.get('/budget').text
    a.post('/budget', data={'category_id': str(_cat(env, alex.id, 'Groceries')), 'amount': '500'})
    url = upload(a, household_xlsx())
    page = a.get(url)
    assert page.status_code == 200
    assert 'Home &amp; property' in page.text and 'One-off, not a monthly cost' in page.text
    assert '$500.00' in page.text  # current Groceries budget shown
    # Tick Groceries, Fuel and Council rates; send Fuel and Takeaway to one category.
    form: dict[str, str] = {'count': '9', 'mode': 'replace'}
    lines = svc.parse(household_xlsx(), 'x.xlsx', None, has_loan=False).lines
    for line in lines:
        form[f'name_{line.index}'] = line.name
        form[f'category_{line.index}'] = line.name
        form[f'monthly_{line.index}'] = f'{(line.monthly_cents or 0) / 100:.2f}'
    for i, n in enumerate(['Groceries', 'Takeaway & eating out', 'Fuel', 'Council rates']):
        idx = next(line.index for line in lines if line.name == n)
        form[f'include_{idx}'] = 'on'
        if n in ('Takeaway & eating out', 'Fuel'):
            form[f'category_{idx}'] = 'Car and food'
        del i
    resp = a.post(url + '/apply', data=form)
    assert resp.status_code == 303 and resp.headers['location'] == '/budget'
    with env.db() as db:
        budgets = {
            db.get(Category, b.category_id).name: b.monthly_cents  # type: ignore[union-attr]
            for b in db.execute(select(Budget).where(Budget.user_id == alex.id)).scalars()
        }
    assert budgets == {
        'Groceries': 1040_00,  # replaced the 500
        'Car and food': 150_00 + 433_33,
        'Council rates': 246_67,
    }
    # The staged file is gone after applying.
    assert a.get(url).status_code == 404


def test_import_saves_up_yearly_and_quarterly_lines(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    url = upload(a, household_xlsx())
    page = a.get(url).text
    lines = svc.parse(household_xlsx(), 'x.xlsx', None, has_loan=False).lines
    rates = next(line for line in lines if line.name == 'Council rates')
    assert f'name="cycle_{rates.index}"' in page
    assert '<option value="12" selected>Every year</option>' in page
    # The file's frequency is shown plainly beside its amount.
    assert '<span class="pill pill-upcoming">Yearly</span>' in page
    assert 'Weekly in the file, budgeted by the month' in page
    form: dict[str, str] = {'count': '9', 'mode': 'replace'}
    for line in lines:
        form[f'name_{line.index}'] = line.name
        form[f'category_{line.index}'] = line.name
        form[f'monthly_{line.index}'] = f'{(line.monthly_cents or 0) / 100:.2f}'
        form[f'cycle_{line.index}'] = str(line.cycle_months)
    for n in ('Groceries', 'Council rates', 'Fuel', 'Vehicle registration & CTP'):
        form[f'include_{next(line.index for line in lines if line.name == n)}'] = 'on'
    # Fuel (weekly) and rego (yearly) share a category: it is saved up.
    for n in ('Fuel', 'Vehicle registration & CTP'):
        form[f'category_{next(line.index for line in lines if line.name == n)}'] = 'Car'
    assert a.post(url + '/apply', data=form).status_code == 303
    with env.db() as db:
        budgets = {
            db.get(Category, b.category_id).name: (b.monthly_cents, b.cycle_months)  # type: ignore[union-attr]
            for b in db.execute(select(Budget).where(Budget.user_id == alex.id)).scalars()
        }
    assert budgets == {
        'Groceries': (1040_00, 1),
        'Council rates': (246_67, 12),
        'Car': (433_33 + 92_42, 12),
    }
    assert 'Saving up $2,960.04 every year' in a.get('/budget').text


def test_import_refuses_an_unknown_cycle(env: Env) -> None:
    a = env.login(env.user('alex'))
    url = upload(a, svc.template_csv().encode(), 'b.csv')
    data = {
        'count': '1',
        'include_0': 'on',
        'name_0': 'Groceries',
        'category_0': 'Groceries',
        'monthly_0': '10',
        'cycle_0': '5',
    }
    resp = a.post(url + '/apply', data=data, follow_redirects=True)
    assert 'choose how often' in resp.text
    with env.db() as db:
        assert db.execute(select(Budget)).first() is None


def test_add_mode_and_forecast_flag(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    gid = _cat(env, alex.id, 'Groceries')
    a.post('/budget', data={'category_id': str(gid), 'amount': '100'})
    url = upload(a, svc.template_csv().encode(), 'b.csv')
    resp = a.post(
        url + '/apply',
        data={
            'count': '1',
            'include_0': 'on',
            'name_0': 'Groceries',
            'category_0': 'groceries',
            'monthly_0': '1040.00',
            'mode': 'add',
            'use_in_forecast': 'on',
        },
    )
    assert resp.status_code == 303
    with env.db() as db:
        b = db.execute(select(Budget).where(Budget.category_id == gid)).scalar_one()
        assert b.monthly_cents == 1140_00 and b.use_in_forecast


def test_clear_mode_starts_the_budget_again(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    gid = _cat(env, alex.id, 'Groceries')
    a.post('/budget', data={'category_id': str(gid), 'amount': '100'})
    a.post('/budget', data={'new_category': 'Old thing', 'amount': '50'})
    a.post('/budget/spend', data={'category_id': str(gid), 'amount': '20'})
    url = upload(a, svc.template_csv().encode(), 'b.csv')
    page = a.get(url).text
    assert 'value="clear"' in page
    resp = a.post(
        url + '/apply',
        data={
            'count': '1',
            'include_0': 'on',
            'name_0': 'Internet',
            'category_0': 'Internet',
            'monthly_0': '79.00',
            'mode': 'clear',
        },
        follow_redirects=True,
    )
    assert 'Old budget cleared (2 lines)' in resp.text
    with env.db() as db:
        budgets = {
            db.get(Category, b.category_id).name: b.monthly_cents  # type: ignore[union-attr]
            for b in db.execute(select(Budget).where(Budget.user_id == alex.id)).scalars()
        }
        assert budgets == {'Internet': 79_00}
        # What was taken off by hand stays.
        assert db.execute(select(BudgetSpend)).scalar_one().amount_cents == 20_00


def test_bad_amount_is_reported_not_saved(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    url = upload(a, svc.template_csv().encode(), 'b.csv')
    resp = a.post(
        url + '/apply',
        data={
            'count': '1',
            'include_0': 'on',
            'name_0': 'Groceries',
            'category_0': 'Groceries',
            'monthly_0': 'lots',
        },
    )
    assert resp.status_code == 303 and resp.headers['location'] == url
    with env.db() as db:
        assert db.execute(select(Budget).where(Budget.user_id == alex.id)).first() is None


def test_another_users_staged_file_is_unreachable(env: Env) -> None:
    a = env.login(env.user('alex'))
    b = env.login(env.user('blake'))
    url = upload(a, household_xlsx())
    assert b.get(url).status_code == 404
    assert b.post(url + '/apply', data={'count': '0'}).status_code == 404
    assert a.get('/budget/import/../fiscus.db').status_code == 404


def test_unreadable_upload_goes_back_with_a_message(env: Env) -> None:
    a = env.login(env.user('alex'))
    url = upload(a, b'PK\x03\x04not really a zip', 'x.xlsx')
    resp = a.get(url)
    assert resp.status_code == 303 and resp.headers['location'] == '/budget'


def test_template_download(env: Env) -> None:
    resp = env.login(env.user('alex')).get('/budget/template.csv')
    assert resp.status_code == 200 and resp.text.startswith('Item,Amount,Frequency')


def _cat(env: Env, user_id: int, name: str) -> int:
    with env.db() as db:
        return int(
            db.execute(
                select(Category.id).where(Category.user_id == user_id, Category.name == name)
            ).scalar_one()
        )
