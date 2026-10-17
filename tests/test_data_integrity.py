"""Figures that stay put: category names, what a paid bill cost, what a deleted
category took off accounts, and forms that save all or nothing."""

from __future__ import annotations

from sqlalchemy import select

from fiscus.models import BudgetSpend, Category, Transaction, User
from fiscus.services import budget_groups, budgeting, forecasting, ledger
from tests.conftest import CsrfClient, Env
from tests.test_bills import create_bill, make_account


def category_id(env: Env, user: User, name: str) -> int:
    with env.db() as db:
        return int(
            db.execute(
                select(Category.id).where(Category.user_id == user.id, Category.name == name)
            ).scalar_one()
        )


def balance(env: Env, user: User, account: int) -> int:
    with env.db() as db:
        fresh = db.get(User, user.id)
        assert fresh is not None
        return ledger.balances(db, fresh, env.clock.today())[account]


# ------------------------------------------------------------- category names


def test_a_category_differing_only_in_case_is_refused(env: Env) -> None:
    a = env.login(env.user('alex'))
    resp = a.post('/categories', data={'name': 'groceries'})
    assert resp.status_code == 303
    assert 'already%20have%20a%20category' in resp.headers['set-cookie']
    assert a.post('/categories', data={'name': 'Pet food'}).status_code == 303


def test_names_differing_in_case_from_before_still_work(env: Env) -> None:
    """Older data can hold both; naming one by hand or in a file picks the
    exact match rather than crashing."""
    alex = env.user('alex')
    a = env.login(alex)
    with env.db() as db:
        db.add(Category(user_id=alex.id, name='groceries'))
        db.commit()
    resp = a.post('/budget', data={'new_category': 'Groceries', 'amount': '100'})
    assert resp.status_code == 303
    with env.db() as db:
        fresh = db.get(User, alex.id)
        assert fresh is not None
        s, e = budgeting.period(db, fresh, env.clock.today())
        lines = {r.category.name for r in budgeting.rows(db, fresh, s, e) if r.has_budget}
    assert lines == {'Groceries'}
    body = b'name,amount,frequency,category\nCar,500,yearly,GROCERIES\n'
    up = a.post('/bills/import', files={'file': ('b.csv', body, 'text/csv')})
    name = up.headers['location'].rsplit('/', 1)[1]
    done = a.post(
        f'/bills/import/{name}/apply',
        data={'map_name': '0', 'map_amount': '1', 'map_frequency': '2', 'map_category': '3'},
    )
    assert done.status_code == 303


def test_renaming_a_line_moves_every_old_name_aside(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    with env.db() as db:
        db.add_all([Category(user_id=alex.id, name='pets'), Category(user_id=alex.id, name='PETS')])
        db.commit()
    other = category_id(env, alex, 'Other')
    a.post('/budget', data={'category_id': str(other), 'amount': '50', 'name': 'Pets'})
    with env.db() as db:
        names = set(db.execute(select(Category.name).where(Category.user_id == alex.id)).scalars())
    assert {'Pets', 'pets (old)', 'PETS (old 2)'} <= names


# ----------------------------------------------------------- paid bills (D57)


def paid_in_utilities(env: Env, user: User) -> int:
    cid = category_id(env, user, 'Utilities')
    with env.db() as db:
        fresh = db.get(User, user.id)
        assert fresh is not None
        s, e = budgeting.period(db, fresh, env.clock.today())
        return budgeting.bills_paid_by_category(db, fresh, s, e).get(cid, 0)


def power_bill(env: Env, a: CsrfClient, alex: User) -> tuple[int, dict[str, str]]:
    acct = make_account(a)
    fields = {
        'name': 'Power',
        'frequency': 'quarterly',
        'next_due': '2026-10-01',
        'account_id': str(acct),
        'category_id': str(category_id(env, alex, 'Utilities')),
        'active': 'on',
    }
    bill = create_bill(a, amount='300', **fields)
    assert a.post(f'/bills/{bill}/occurrence/2026-10-01/paid').status_code == 303
    return bill, fields


def test_a_paid_bill_keeps_what_was_paid(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    bill, fields = power_bill(env, a, alex)
    a.post(f'/bills/{bill}', data={**fields, 'amount': '350'})  # next quarter's estimate
    assert paid_in_utilities(env, alex) == 30000
    sheet = a.get(f'/bills/{bill}/occurrence/2026-10-01').text
    assert '$300.00' in sheet


def test_stopping_or_moving_a_paid_bill_keeps_its_payment(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    bill, fields = power_bill(env, a, alex)
    del fields['active']
    a.post(f'/bills/{bill}', data={**fields, 'amount': '300'})  # stopped
    assert paid_in_utilities(env, alex) == 30000
    fields['active'] = 'on'
    a.post(f'/bills/{bill}', data={**fields, 'amount': '300', 'next_due': '2026-10-09'})
    assert paid_in_utilities(env, alex) == 30000


def test_a_bill_paid_ahead_counts_what_was_paid_in_the_outlook(env: Env) -> None:
    """A saved-up line's outlook counts a bill paid before its budget month at
    what was paid, as the budget will take it off, not at the bill's new amount."""
    alex = env.user('alex')
    a = env.login(alex)
    utilities = category_id(env, alex, 'Utilities')
    fields = {
        'name': 'Power',
        'frequency': 'quarterly',
        'next_due': '2026-11-01',
        'account_id': str(make_account(a)),
        'category_id': str(utilities),
        'active': 'on',
    }
    bill = create_bill(a, amount='300', **fields)
    a.post('/budget', data={'category_id': str(utilities), 'amount': '900', 'cycle_months': '3'})
    assert a.post(f'/bills/{bill}/occurrence/2026-11-01/paid').status_code == 303
    a.post(f'/bills/{bill}', data={**fields, 'amount': '350'})
    with env.db() as db:
        fresh = db.get(User, alex.id)
        assert fresh is not None
        s, e = budgeting.period(db, fresh, env.clock.today())
        rows = budgeting.rows(db, fresh, s, e)
        outlook = budgeting.due_checks(db, fresh, rows, s, env.clock.today())[utilities]
    # February's bill needs November's $300 as paid, then its own $350.
    assert outlook.next.need_cents == 65000


# ------------------------------------------------- deleting a category (D58)


def test_deleting_a_category_leaves_its_spending_off_the_accounts(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    acct = make_account(a, opening='1000')
    dining = category_id(env, alex, 'Dining out')
    a.post('/budget', data={'category_id': str(dining), 'amount': '200'})
    a.post('/budget/spend', data={'category_id': str(dining), 'amount': '50', 'note': 'Pizza'})
    assert balance(env, alex, acct) == 95000
    resp = a.post(f'/categories/{dining}/delete')
    assert 'stays%20off' in resp.headers['set-cookie']
    assert balance(env, alex, acct) == 95000
    with env.db() as db:
        kept = db.execute(select(Transaction).where(Transaction.user_id == alex.id)).scalar_one()
        assert kept.budget_spend_id is None and kept.category_id is None
        assert kept.description == 'Dining out · Pizza'
        fresh = db.get(User, alex.id)
        assert fresh is not None
        # Not an average to forecast from: the person spent it by hand.
        assert forecasting.estimates(db, fresh, env.clock.today()) == []
    # A new spend can take the deleted one's id without a clash.
    other = category_id(env, alex, 'Other')
    a.post('/budget', data={'category_id': str(other), 'amount': '200'})
    assert (
        a.post('/budget/spend', data={'category_id': str(other), 'amount': '20'}).status_code == 303
    )
    assert balance(env, alex, acct) == 93000
    with env.db() as db:
        assert len(db.execute(select(BudgetSpend)).scalars().all()) == 1


# ------------------------------------------------------ forms: all or nothing


def test_typing_every_balance_saves_none_when_one_is_wrong(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    first = make_account(a, name='A first', opening='100')
    second = make_account(a, name='B second', opening='100')
    resp = a.post('/accounts/balances', data={f'balance_{first}': '555', f'balance_{second}': 'x'})
    assert 'Nothing%20was%20saved' in resp.headers['set-cookie']
    assert balance(env, alex, first) == 10000
    a.post('/accounts/balances', data={f'balance_{first}': '555', f'balance_{second}': '7'})
    assert (balance(env, alex, first), balance(env, alex, second)) == (55500, 700)


def test_a_budget_line_with_a_bad_amount_makes_no_category(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    assert a.post('/budget', data={'new_category': 'Pets', 'amount': 'lots'}).status_code == 303
    with env.db() as db:
        assert db.execute(select(Category).where(Category.name == 'Pets')).first() is None


# --------------------------------------------------------------- order (D35)


def test_a_line_never_moved_goes_after_the_moved_ones(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    ids = {n: category_id(env, alex, n) for n in ('Groceries', 'Transport', 'Health')}
    a.post('/budget', data={'category_id': str(ids['Groceries']), 'amount': '500'})
    a.post('/budget', data={'category_id': str(ids['Transport']), 'amount': '300'})
    a.post(f'/budget/{ids["Transport"]}/move', data={'direction': 'up'})
    a.post('/budget', data={'category_id': str(ids['Health']), 'amount': '900'})
    with env.db() as db:
        fresh = db.get(User, alex.id)
        assert fresh is not None
        s, e = budgeting.period(db, fresh, env.clock.today())
        rows = budgeting.rows(db, fresh, s, e)
        order = [r.category.name for r in budget_groups.sections(db, fresh, rows)[-1].rows]
    assert order[:3] == ['Transport', 'Groceries', 'Health']
