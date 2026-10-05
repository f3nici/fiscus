"""User A's finances are invisible and untouchable to user B.

One of everything is created for A. Then B tries every read and every write
route with A's ids. Each must be a 404 (never a 403, so ids cannot be probed)
and A's rows must come out exactly as they went in.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import select, text

from fiscus.models import (
    Account,
    Bill,
    BudgetGroup,
    BudgetSpend,
    Category,
    Goal,
    GoalMove,
    Import,
    Loan,
    LoanRate,
    PaySchedule,
    Payslip,
    RecurringTransfer,
    Rule,
    Scenario,
    ScenarioAdjustment,
    Transaction,
    User,
)
from tests.conftest import CsrfClient, Env
from tests.test_imports_bills import ANZ_SEPT, create_bill, import_csv, make_account, upload

OWNED_TABLES = [
    'accounts',
    'transactions',
    'categories',
    'rules',
    'imports',
    'import_mappings',
    'bills',
    'bill_shares',
    'bill_payments',
    'pay_schedules',
    'payslips',
    'loans',
    'loan_rates',
    'budgets',
    'budget_spends',
    'budget_groups',
    'tax_deductions',
    'goals',
    'goal_moves',
    'scenarios',
    'scenario_adjustments',
    'recurring_transfers',
    'user_settings',
    'square_connections',
    'square_invoices',
]


def snapshot(env: Env) -> str:
    """A digest of every finance table, to prove nothing moved."""
    out = {}
    with env.db() as db:
        for table in OWNED_TABLES:
            rows = db.execute(text(f'SELECT * FROM {table} ORDER BY 1')).all()  # noqa: S608
            out[table] = [list(map(str, r)) for r in rows]
    return hashlib.sha256(json.dumps(out, sort_keys=True).encode()).hexdigest()


@dataclass
class Ids:
    account: int
    txn: int
    category: int
    rule: int
    imp: int
    pending_imp: int
    bill: int
    schedule: int
    payslip: int
    loan: int
    rate: int
    staged: str  # a bill import file, uploaded and not yet applied
    scenario: int
    adjustment: int
    transfer: int
    spend: int
    goal: int
    goal_move: int
    group: int


@pytest.fixture
def seeded(env: Env) -> tuple[CsrfClient, CsrfClient, Ids, User, User]:
    alex, blake = env.user('alex'), env.user('blake')
    a, b = env.login(alex), env.login(blake)
    acct = make_account(a)
    savings = make_account(a, 'Savings', 'savings')
    import_csv(a, acct, ANZ_SEPT)
    pending = upload(a, acct, ANZ_SEPT + '30/09/2026,"-1.00","X"\n')
    bill = create_bill(a, account_id=str(acct))
    a.post(
        '/pay/schedules',
        data={
            'name': 'Acme wages',
            'frequency': 'fortnightly',
            'anchor_date': '2026-10-02',
            'net': '2500',
            'account_id': str(acct),
        },
    )
    a.post(
        '/payslips',
        data={'date': '2026-09-18', 'gross': '3500', 'tax_withheld': '800', 'net': '2700'},
    )
    resp = a.post(
        '/loans',
        data={
            'name': 'Kingsley St mortgage',
            'principal': '500000',
            'start_date': '2024-01-15',
            'term_years': '30',
            'initial_rate': '6.1',
            'balance': '480000',
            'balance_date': '2026-10-01',
            'repayment_account_id': str(acct),
        },
    )
    assert resp.status_code == 303
    rates = (
        b'name,amount,frequency,category,due_date\n'
        b'Stirling council rates,520,quarterly,Housing,2026-11-15\n'
    )
    resp = a.post('/bills/import', files={'file': ('r.csv', rates, 'text/csv')})
    staged = resp.headers['location'].rsplit('/', 1)[1]
    a.post(
        f'/bills/import/{staged}/apply',
        data={
            'map_name': '0',
            'map_amount': '1',
            'map_frequency': '2',
            'map_category': '3',
            'map_due_date': '4',
        },
    )
    resp = a.post('/bills/import', files={'file': ('r.csv', rates, 'text/csv')})
    staged = resp.headers['location'].rsplit('/', 1)[1]
    resp = a.post('/scenarios', data={'name': 'What if'})
    scenario = int(resp.headers['location'].rsplit('/', 1)[1])
    a.post(
        f'/scenarios/{scenario}/adjustments',
        data={'kind': 'one_off', 'date': '2026-11-01', 'account_id': str(acct), 'amount': '100'},
    )
    a.post(
        '/transfers',
        data={
            'name': 'Tax',
            'from_account_id': str(acct),
            'to_account_id': str(savings),
            'amount': '100',
            'frequency': 'monthly',
            'anchor_date': '2026-10-15',
        },
    )
    with env.db() as db:
        cat = db.execute(select(Category).where(Category.user_id == alex.id)).scalars().first()
        assert cat is not None
        a.post('/rules', data={'pattern': 'coffee', 'category_id': str(cat.id)})
        a.post('/budget', data={'category_id': str(cat.id), 'amount': '300'})
        a.post(
            '/budget/spend', data={'category_id': str(cat.id), 'amount': '42', 'note': 'Bunnings'}
        )
        a.post(
            '/goals',
            data={
                'name': 'Hokkaido trip',
                'account_id': str(savings),
                'target': '6000',
                'target_date': '2027-03-15',
                'saved': '1000',
                'keep_aside': 'true',
            },
        )

        a.post('/budget/groups', data={'name': 'Bills'})

        def one(model: Any) -> int:
            found = db.execute(select(model.id).where(model.user_id == alex.id)).scalars().first()
            assert found is not None
            return int(found)

        ids = Ids(
            account=acct,
            txn=one(Transaction),
            category=cat.id,
            rule=one(Rule),
            imp=int(
                db.execute(select(Import.id).where(Import.status == 'committed')).scalars().first()
                or 0
            ),
            pending_imp=pending,
            bill=bill,
            schedule=one(PaySchedule),
            payslip=one(Payslip),
            loan=one(Loan),
            rate=one(LoanRate),
            staged=staged,
            scenario=scenario,
            adjustment=one(ScenarioAdjustment),
            transfer=one(RecurringTransfer),
            spend=one(BudgetSpend),
            goal=one(Goal),
            goal_move=one(GoalMove),
            group=one(BudgetGroup),
        )
    return a, b, ids, alex, blake


READS = [
    '/accounts/{account}',
    '/accounts/{account}/edit',
    '/import/{pending_imp}',
    '/bills/{bill}',
    '/bills/{bill}/edit',
    '/bills/{bill}/occurrence/2026-10-15',
    '/loans/{loan}',
    '/loans/{loan}/edit',
    '/scenarios/{scenario}',
    '/forecast?scenario_id={scenario}',
    '/pay?edit={schedule}',
    '/pay?slip={payslip}',
    '/pay?payday={schedule}:2026-10-02',
    '/bills/import/{staged}',
    '/goals/{goal}',
]

WRITES: list[tuple[str, dict[str, Any]]] = [
    ('/accounts/{account}', {'name': 'pwned', 'type': 'transaction'}),
    ('/accounts/{account}/delete', {}),
    ('/transactions/{txn}/category', {'category_id': '0'}),
    ('/transactions/{txn}/delete', {}),
    (
        '/transactions',
        {'account_id': '{account}', 'date': '2026-10-01', 'amount': '-1', 'description': 'x'},
    ),
    ('/categories/{category}', {'name': 'pwned'}),
    ('/categories/{category}/delete', {}),
    ('/rules/{rule}', {'pattern': 'x', 'category_id': '{category}'}),
    ('/rules/{rule}/delete', {}),
    ('/import', {'account_id': '{account}'}),
    ('/import/{imp}/undo', {}),
    ('/import/{pending_imp}/commit', {}),
    ('/import/{pending_imp}/discard', {}),
    ('/import/{pending_imp}/account', {'account_id': '{account}'}),
    (
        '/bills/{bill}',
        {'name': 'x', 'amount': '1', 'frequency': 'monthly', 'next_due': '2026-10-01'},
    ),
    ('/bills/{bill}/delete', {}),
    ('/bills/{bill}/my-share', {'account_id': '0'}),
    ('/bills/{bill}/occurrence/2026-10-15/paid', {}),
    ('/bills/{bill}/occurrence/2026-10-15/unpaid', {}),
    (
        '/pay/schedules/{schedule}',
        {'name': 'x', 'frequency': 'weekly', 'anchor_date': '2026-10-01', 'net': '1'},
    ),
    ('/pay/schedules/{schedule}/delete', {}),
    ('/payslips/{payslip}/delete', {}),
    ('/payslips/{payslip}', {'date': '2026-10-02', 'gross': '1', 'tax_withheld': '0', 'net': '1'}),
    (
        '/payslips',
        {
            'date': '2026-10-02',
            'gross': '1',
            'tax_withheld': '0',
            'net': '1',
            'payday': '{schedule}:2026-10-02',
        },
    ),
    ('/pay/schedules/{schedule}/no-pay', {'payday': '2026-10-02'}),
    (
        '/loans/{loan}',
        {
            'name': 'x',
            'principal': '1',
            'start_date': '2026-01-01',
            'balance': '1',
            'balance_date': '2026-01-01',
        },
    ),
    ('/loans/{loan}/rates', {'effective_date': '2026-10-01', 'new_rate': '1'}),
    ('/loans/{loan}/rates/{rate}/delete', {}),
    ('/loans/{loan}/offsets', {'account_id': '{account}'}),
    ('/loans/{loan}/delete', {}),
    ('/bills/import/{staged}/apply', {}),
    ('/scenarios/{scenario}/delete', {}),
    ('/scenarios/{scenario}/adjustments', {'kind': 'one_off'}),
    ('/scenarios/{scenario}/adjustments/{adjustment}/delete', {}),
    ('/transfers/{transfer}/delete', {}),
    ('/budget', {'category_id': '{category}', 'amount': '1'}),
    ('/budget/spend', {'category_id': '{category}', 'amount': '1'}),
    ('/budget/spend/{spend}/delete', {}),
    ('/budget/{category}/remove', {}),
    ('/budget/{category}/move', {'direction': 'up'}),
    ('/budget/groups/{group}', {'name': 'pwned'}),
    ('/budget/groups/{group}/move', {'direction': 'up'}),
    ('/budget/groups/{group}/delete', {}),
    ('/budget', {'category_id': '0', 'new_category': 'x', 'amount': '1', 'group_id': '{group}'}),
    ('/accounts/{account}/balance', {'balance': '1'}),
    ('/goals/{goal}', {'name': 'pwned'}),
    ('/goals/{goal}/move', {'amount': '1'}),
    ('/goals/{goal}/moves/{goal_move}/delete', {}),
    ('/goals/{goal}/done', {}),
    ('/goals/{goal}/delete', {}),
    ('/goals', {'name': 'x', 'account_id': '{account}'}),
    ('/goals', {'name': 'x', 'category_id': '{category}'}),
    ('/square', {'account_id': '{account}', 'start_date': '2026-10-01'}),
    ('/square', {'fee_category_id': '{category}', 'start_date': '2026-10-01'}),
]


def fill(template: str, ids: Ids) -> str:
    return template.format(**ids.__dict__)


def test_b_cannot_read_a(seeded: tuple[CsrfClient, CsrfClient, Ids, User, User]) -> None:
    a, b, ids, *_ = seeded
    for path in READS:
        url = fill(path, ids)
        assert a.get(url).status_code == 200, f'owner could not read {url}'
        assert b.get(url).status_code == 404, f'{url} leaked to another user'


def test_b_cannot_write_a(env: Env, seeded: tuple[CsrfClient, CsrfClient, Ids, User, User]) -> None:
    _a, b, ids, *_ = seeded
    before = snapshot(env)
    for path, data in WRITES:
        url = fill(path, ids)
        form = {k: fill(v, ids) if isinstance(v, str) else v for k, v in data.items()}
        files = (
            {'file': ('x.csv', b'01/10/2026,"-1.00","X"\n', 'text/csv')}
            if path == '/import'
            else None
        )
        resp = b.post(url, data=form, files=files)
        assert resp.status_code == 404, f'POST {url} answered {resp.status_code}'
    # The bulk balance form only reads B's own accounts: A's id is ignored.
    b.post('/accounts/balances', data={f'balance_{ids.account}': '1'})
    assert snapshot(env) == before


def test_b_lists_show_none_of_a(seeded: tuple[CsrfClient, CsrfClient, Ids, User, User]) -> None:
    a, b, ids, _alex, _blake = seeded
    pages = [
        '/',
        '/accounts',
        '/transactions',
        '/bills',
        '/pay',
        '/calendar?view=agenda',
        '/loans',
        '/forecast',
        '/scenarios',
        '/budget',
        f'/bills?category={ids.category}',
        '/tax',
        '/import',
        '/recategorise',
        '/square',
        '/goals',
    ]
    markers = [
        'Everyday',
        'COFFEE',
        'Electricity',
        'Acme wages',
        'Kingsley St',
        'What if',
        'Stirling council',
        'Bunnings',
        'Hokkaido',
        '$2,500',
    ]
    for page in pages:
        assert a.get(page).status_code == 200
        body = b.get(page).text
        for m in markers:
            assert m not in body, f"{m!r} from A appears on B's {page}"
    export = b.get('/transactions/export.csv').text
    assert 'COFFEE' not in export


def test_b_cannot_add_a_to_their_rows(
    env: Env, seeded: tuple[CsrfClient, CsrfClient, Ids, User, User]
) -> None:
    """B referencing A's account or category from B's own forms is refused too."""
    _a, b, ids, _alex, blake = seeded
    b_acct = make_account(b, 'B account')
    assert (
        b.post(
            '/bills',
            data={
                'name': 'x',
                'amount': '10',
                'frequency': 'monthly',
                'next_due': '2026-10-01',
                'account_id': str(ids.account),
            },
        ).status_code
        == 404
    )
    assert (
        b.post(
            '/transfers',
            data={
                'name': 't',
                'from_account_id': str(b_acct),
                'to_account_id': str(ids.account),
                'amount': '1',
                'frequency': 'monthly',
                'anchor_date': '2026-10-01',
            },
        ).status_code
        == 404
    )
    assert (
        b.post('/rules', data={'pattern': 'x', 'category_id': str(ids.category)}).status_code == 404
    )
    assert (
        b.post(
            '/payslips',
            data={
                'date': '2026-11-01',
                'gross': '10',
                'tax_withheld': '0',
                'net': '10',
                'account_id': str(ids.account),
            },
        ).status_code
        == 404
    )
    with env.db() as db:
        assert db.execute(select(Bill).where(Bill.owner_id == blake.id)).first() is None


def test_forecast_and_tax_use_only_own_data(
    seeded: tuple[CsrfClient, CsrfClient, Ids, User, User],
) -> None:
    _a, b, *_ = seeded
    forecast = b.get('/forecast').text
    assert '2,500' not in forecast
    tax_page = b.get('/tax').text
    assert '$3,500.00' not in tax_page and '$800.00' not in tax_page


def test_account_ids_are_not_enumerable_by_status(
    seeded: tuple[CsrfClient, CsrfClient, Ids, User, User],
) -> None:
    _a, b, ids, *_ = seeded
    assert (
        b.get(f'/accounts/{ids.account}').status_code
        == b.get('/accounts/999999').status_code
        == 404
    )


def test_scenarios_never_change_real_data(
    env: Env, seeded: tuple[CsrfClient, CsrfClient, Ids, User, User]
) -> None:
    a, _b, ids, *_ = seeded
    a.post(
        f'/scenarios/{ids.scenario}/adjustments',
        data={
            'kind': 'rate_change',
            'loan_id': str(ids.loan),
            'from_date': '2026-10-01',
            'rate': '9',
        },
    )
    a.post(
        f'/scenarios/{ids.scenario}/adjustments',
        data={
            'kind': 'new_bill',
            'name': 'Boat',
            'amount': '500',
            'frequency': 'monthly',
            'start_date': '2026-10-05',
            'account_id': str(ids.account),
        },
    )
    before = snapshot(env)
    base = a.get('/forecast').text
    compared = a.get(f'/forecast?scenario_id={ids.scenario}').text
    assert snapshot(env) == before
    assert 'What if' in compared
    assert base != compared
    with env.db() as db:
        assert db.execute(select(Bill).where(Bill.name == 'Boat')).first() is None
        loan = db.get(Loan, ids.loan)
        assert loan is not None and [r.rate for r in loan.rates] == ['6.1']


def test_adjustment_from_another_scenario_cannot_be_deleted_through_mine(
    env: Env, seeded: tuple[CsrfClient, CsrfClient, Ids, User, User]
) -> None:
    a, _b, ids, *_ = seeded
    other = int(a.post('/scenarios', data={'name': 'Other'}).headers['location'].rsplit('/', 1)[1])
    a.post(f'/scenarios/{other}/adjustments/{ids.adjustment}/delete')
    with env.db() as db:
        sc = db.get(Scenario, ids.scenario)
        assert sc is not None and len(sc.adjustments) == 1
        assert db.get(Account, ids.account) is not None
