"""Savings goals: money kept aside inside an account, off the free money, not in the budget."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from fiscus.domain import goals as calc
from fiscus.models import Account, BudgetSpend, Category, Goal, GoalMove, Transaction
from tests.conftest import Env
from tests.test_bills import make_account


def test_months_left_counts_budget_months_to_the_one_holding_the_date() -> None:
    assert calc.months_left(date(2026, 10, 1), date(2027, 3, 15), 1) == 6
    assert calc.months_left(date(2026, 10, 1), date(2026, 10, 31), 1) == 1
    assert calc.months_left(date(2026, 10, 1), date(2026, 9, 30), 1) == 0
    # A budget month starting on the 15th: 15 Oct to 14 Nov holds 1 Nov.
    assert calc.months_left(date(2026, 10, 15), date(2026, 11, 1), 15) == 1
    assert calc.months_left(date(2026, 10, 15), date(2026, 11, 15), 15) == 2


def test_per_month_rounds_up_and_stops_at_the_target() -> None:
    assert calc.per_month(500_000, 6) == 83_334
    assert calc.per_month(600_000, 6) == 100_000
    assert calc.per_month(0, 6) == 0
    assert calc.per_month(-100, 6) == 0
    assert calc.per_month(100, 0) == 0
    assert calc.Progress(150_000, 600_000).pct == 25
    assert calc.Progress(700_000, 600_000).reached
    assert calc.Progress(700_000, None).remaining_cents == 0
    assert calc.unallocated(1_000, [600, 700]) == -300


def _goal(client: object, **data: str | list[str]) -> int:
    resp = client.post('/goals', data=data)  # type: ignore[attr-defined]
    assert resp.status_code == 303, resp.text
    return int(resp.headers['location'].rsplit('/', 1)[1])


def test_a_goal_splits_an_account_and_stays_out_of_the_budget(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    offset = make_account(a, 'Offset', 'offset', '15000')
    japan = _goal(
        a,
        name='Japan trip',
        account_id=str(offset),
        target='6000',
        target_date='2027-03-15',
        saved='1000',
        keep_aside='true',
    )
    # A bucket with no target, counted as free to spend.
    savings = _goal(a, name='Savings', account_id=str(offset), saved='2000')

    page = a.get(f'/goals/{japan}').text
    assert '$1,000.00' in page and '$833.34' in page and '6 months left' in page
    # Goals have no budget line.
    budget = a.get('/budget?month=2026-10').text
    assert 'Japan trip' not in budget and '$833.34' not in budget

    resp = a.post(
        f'/goals/{japan}/move',
        data={'amount': '500', 'note': 'Pay day', 'back': '/budget?month=2026-10'},
        follow_redirects=True,
    )
    assert '$500.00 into Japan trip' in resp.text
    # Free in accounts: 15,000 less 1,500 kept in Japan (Savings is not kept
    # aside), and nothing budgeted for the goal.
    assert '$13,500.00' in resp.text and '$1,500.00 kept in' in resp.text

    accounts = a.get('/accounts').text
    assert 'Unallocated $11,500.00' in accounts
    show = a.get(f'/accounts/{offset}').text
    assert 'Japan trip' in show and '(counted as free)' in show and '$11,500.00' in show

    # Taking money out and correcting what it holds.
    a.post(f'/goals/{japan}/move', data={'amount': '200', 'direction': 'out'})
    a.post(
        f'/goals/{japan}',
        data={
            'name': 'Japan trip',
            'account_id': str(offset),
            'target': '6000',
            'target_date': '2027-03-15',
            'holds': '1400',
            'keep_aside': 'true',
        },
    )
    with env.db() as db:
        moves = db.execute(select(GoalMove).where(GoalMove.goal_id == japan)).scalars().all()
        assert sum(m.amount_cents for m in moves) == 140_000
        assert [m.amount_cents for m in moves] == [100_000, 50_000, -20_000, 10_000]

    # Done: its money free again.
    a.post(f'/goals/{japan}/done')
    budget = a.get('/budget?month=2026-10').text
    assert 'kept in' not in budget
    with env.db() as db:
        goal = db.get(Goal, japan)
        assert goal is not None and goal.archived

    a.post(f'/goals/{savings}/delete')
    with env.db() as db:
        assert db.get(Goal, savings) is None
        assert db.execute(select(GoalMove).where(GoalMove.goal_id == savings)).first() is None


def test_a_goal_needs_a_target_for_a_date(env: Env) -> None:
    a = env.login(env.user('alex'))
    resp = a.post(
        '/goals', data={'name': 'Car', 'target_date': '2027-01-01'}, follow_redirects=True
    )
    assert 'Give a target amount to save by a date' in resp.text
    with env.db() as db:
        assert db.execute(select(Goal)).first() is None


def test_goals_are_not_in_the_dashboard_budget(env: Env) -> None:
    a = env.login(env.user('alex'))
    make_account(a, 'Offset', 'offset', '5000')
    _goal(a, name='Japan trip', target='1200', target_date='2027-09-30')
    page = a.get('/').text
    assert 'Japan trip' in page and '-$100.00' not in page


def test_a_tax_goal_counts_as_set_aside_for_tax(env: Env) -> None:
    a = env.login(env.user('alex'))
    offset = make_account(a, 'Offset', 'offset', '5000')
    tax = _goal(
        a, name='Tax', account_id=str(offset), saved='1200', is_tax='true', keep_aside='true'
    )
    other = _goal(a, name='Other', saved='50', keep_aside='true')
    page = a.get('/tax').text
    assert '$1,200.00</strong> set aside for tax' in page and 'in your <a' in page
    goal_page = a.get(f'/goals/{tax}').text
    assert 'This goal is your tax money' in goal_page

    # Kept aside like any goal: off the free money, not in the budget.
    budget = a.get('/budget').text
    assert '$1,250.00 kept in' in budget

    # Only one goal is the tax money.
    a.post(f'/goals/{other}', data={'name': 'Other', 'is_tax': 'true', 'keep_aside': 'true'})
    with env.db() as db:
        assert [g.name for g in db.execute(select(Goal).where(Goal.is_tax)).scalars()] == ['Other']
    assert '$50.00</strong> set aside for tax' in a.get('/tax').text


def test_a_tax_goal_in_a_set_aside_account_is_not_counted_twice(env: Env) -> None:
    a = env.login(env.user('alex'))
    acct = make_account(a, 'Tax', 'savings', '3000')
    with env.db() as db:
        row = db.get(Account, acct)
        assert row is not None
        row.is_set_aside = True
        db.commit()
    _goal(a, name='Tax', account_id=str(acct), saved='1000', is_tax='true')
    assert '$3,000.00</strong> set aside for tax' in a.get('/tax').text


def test_a_move_from_another_goal_is_not_found(env: Env) -> None:
    a = env.login(env.user('alex'))
    one = _goal(a, name='One', saved='10')
    two = _goal(a, name='Two', saved='20')
    with env.db() as db:
        mid = db.execute(select(GoalMove.id).where(GoalMove.goal_id == one)).scalar_one()
    assert a.post(f'/goals/{two}/moves/{mid}/delete').status_code == 404
    assert a.post(f'/goals/{one}/moves/{mid}/delete').status_code == 303


def test_putting_aside_from_a_budget_line_spends_it_and_keeps_it_aside(env: Env) -> None:
    a = env.login(env.user('alex'))
    main = make_account(a, 'Main', 'transaction', '5000')
    a.post('/budget', data={'new_category': 'Utilities', 'amount': '250'})
    with env.db() as db:
        cid = db.execute(select(Category.id).where(Category.name == 'Utilities')).scalar_one()
    goal = _goal(a, name='Utilities', account_id=str(main), category_id=str(cid), keep_aside='true')
    budget = a.get('/budget').text
    assert 'Put aside' in budget and '$4,750.00' in budget

    # One click: spent on the line and into the goal, free money unchanged.
    resp = a.post(
        f'/goals/{goal}/move',
        data={'amount': '250', 'as_spent': 'true', 'back': '/budget'},
        follow_redirects=True,
    )
    assert '$250.00 put aside into Utilities' in resp.text
    assert '$4,750.00' in resp.text and '$250.00 kept in' in resp.text
    with env.db() as db:
        spend = db.execute(select(BudgetSpend)).scalar_one()
        move = db.execute(select(GoalMove)).scalar_one()
        assert (spend.category_id, spend.amount_cents) == (cid, 25_000)
        assert (move.amount_cents, move.spend_id) == (25_000, spend.id)

    # Paying the bill comes out of the goal and is not spent again.
    a.post(f'/goals/{goal}/move', data={'amount': '200', 'direction': 'out', 'as_spent': 'true'})
    with env.db() as db:
        assert db.execute(select(func.count(BudgetSpend.id))).scalar() == 1

    # Removing the spend takes the goal move with it, and the other way round.
    a.post(f'/budget/spend/{spend.id}/delete')
    with env.db() as db:
        assert db.get(GoalMove, move.id) is None
    a.post(f'/goals/{goal}/move', data={'amount': '100', 'as_spent': 'true'})
    with env.db() as db:
        again = db.execute(select(GoalMove).where(GoalMove.spend_id.is_not(None))).scalar_one()
        sid = again.spend_id
    a.post(f'/goals/{goal}/moves/{again.id}/delete')
    with env.db() as db:
        assert db.get(BudgetSpend, sid) is None

    # Deleting the category leaves the goal holding what was put aside.
    a.post(f'/goals/{goal}/move', data={'amount': '50', 'as_spent': 'true'})
    a.post(f'/categories/{cid}/delete')
    with env.db() as db:
        g = db.get(Goal, goal)
        assert g is not None and db.get(Category, cid) is None
        assert sum(m.amount_cents for m in g.moves) == -200_00 + 50_00


def test_a_put_aside_changed_on_the_budget_keeps_its_goal_in_step(env: Env) -> None:
    a = env.login(env.user('alex'))
    main = make_account(a, 'Main', 'transaction', '5000')
    ids = {}
    for name in ('Utilities', 'Rates'):
        a.post('/budget', data={'new_category': name, 'amount': '250'})
        with env.db() as db:
            ids[name] = db.execute(select(Category.id).where(Category.name == name)).scalar_one()
    goal = _goal(a, name='Bills', account_id=str(main), category_id=str(ids['Utilities']))
    a.post(f'/goals/{goal}/move', data={'amount': '250', 'as_spent': 'true'})
    with env.db() as db:
        spend = db.execute(select(BudgetSpend.id)).scalar_one()
    # It writes no transaction (D49), so it has no account to change.
    page = a.get('/budget').text
    assert f'id="spend-cat-{spend}"' in page and f'id="spend-account-{spend}"' not in page

    def change(amount: str) -> str:
        form = {'category_id': str(ids['Rates']), 'amount': amount, 'on': '2026-10-03'}
        form |= {'note': 'Into Bills', 'account_id': str(main)}
        return a.post(f'/budget/spend/{spend}', data=form, follow_redirects=True).text

    assert '$200.00 moved from Utilities to Rates' in change('200')
    with env.db() as db:
        move = db.execute(select(GoalMove)).scalar_one()
        assert (move.amount_cents, move.date, move.note) == (
            200_00,
            date(2026, 10, 3),
            'From Rates budget',
        )
        assert db.execute(select(func.count(Transaction.id))).scalar() == 0
    # Money into a goal is never a refund.
    assert 'Amount: Amount cannot be negative' in change('-50')
    with env.db() as db:
        assert db.execute(select(GoalMove.amount_cents)).scalar_one() == 200_00


def test_one_goal_saves_from_several_lines(env: Env) -> None:
    a = env.login(env.user('alex'))
    ids = {}
    for name in ('Car insurance', 'Rego', 'Roadside'):
        a.post('/budget', data={'new_category': name, 'amount': '30'})
        with env.db() as db:
            ids[name] = db.execute(select(Category.id).where(Category.name == name)).scalar_one()
    goal = _goal(a, name='Yearly bills', category_id=[str(ids['Car insurance']), str(ids['Rego'])])
    page = a.get(f'/goals/{goal}').text
    assert 'Car insurance</a> and <a' in page and 'budget lines' in page
    assert a.get('/budget').text.count('Saved into <a href="/goals/') == 2

    # Each line's Put aside spends from that line.
    for name, amount in (('Car insurance', '30'), ('Rego', '20')):
        a.post(
            f'/goals/{goal}/move',
            data={'amount': amount, 'as_spent': 'true', 'category_id': str(ids[name])},
        )
    # A line that is not the goal's is not spent from: an ordinary move in.
    a.post(
        f'/goals/{goal}/move',
        data={'amount': '5', 'as_spent': 'true', 'category_id': str(ids['Roadside'])},
    )
    with env.db() as db:
        spent = {s.category_id: s.amount_cents for s in db.execute(select(BudgetSpend)).scalars()}
        held = sum(m.amount_cents for m in db.execute(select(GoalMove)).scalars())
    assert spent == {ids['Car insurance']: 30_00, ids['Rego']: 20_00} and held == 55_00

    # A line saves into one goal: ticking it on another takes it over.
    other = _goal(a, name='Car', category_id=str(ids['Rego']))
    with env.db() as db:
        assert db.get(Category, ids['Rego']).goal_id == other  # type: ignore[union-attr]
        assert db.get(Category, ids['Car insurance']).goal_id == goal  # type: ignore[union-attr]
    # Unticking everything leaves the goal with no lines.
    a.post(f'/goals/{goal}', data={'name': 'Yearly bills'})
    with env.db() as db:
        assert db.get(Category, ids['Car insurance']).goal_id is None  # type: ignore[union-attr]


def test_a_goal_saves_only_from_a_spending_line(env: Env) -> None:
    a = env.login(env.user('alex'))
    a.post('/budget', data={'new_category': 'Wages', 'amount': '100'})
    with env.db() as db:
        cat = db.execute(select(Category).where(Category.name == 'Wages')).scalar_one()
        cat.is_income = True
        db.commit()
        cid = cat.id
    resp = a.post('/goals', data={'name': 'X', 'category_id': str(cid)}, follow_redirects=True)
    assert 'Choose a spending category to save from' in resp.text


def test_a_goal_cannot_give_out_more_than_it_holds(env: Env) -> None:
    a = env.login(env.user('alex'))
    main = make_account(a, 'Main', 'transaction', '5000')
    trip = _goal(a, name='Trip', account_id=str(main), saved='300', keep_aside='true')
    resp = a.post(f'/goals/{trip}/move', data={'amount': '301', 'direction': 'out'})
    assert 'the%20most%20to%20take%20out' in resp.headers['set-cookie']
    a.post(f'/goals/{trip}/move', data={'amount': '300', 'direction': 'out'})
    with env.db() as db:
        g = db.get(Goal, trip)
        assert g is not None and sum(m.amount_cents for m in g.moves) == 0
    # Holds now cannot be set below nothing either.
    resp = a.post(f'/goals/{trip}', data={'name': 'Trip', 'holds': '-50', 'keep_aside': 'true'})
    assert 'cannot%20be%20negative' in resp.headers['set-cookie']


def test_a_goal_below_nothing_frees_no_extra_money(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    main = make_account(a, 'Main', 'transaction', '5000')
    trip = _goal(a, name='Trip', account_id=str(main), keep_aside='true')
    # An old goal left below nothing (from before taking out was checked).
    with env.db() as db:
        db.add(
            GoalMove(
                user_id=alex.id,
                goal_id=trip,
                date=date(2026, 9, 1),
                amount_cents=-1_000_00,
                note='',
                created_at=env.clock.now(),
            )
        )
        db.commit()
    assert '$5,000.00 in accounts' in a.get('/budget').text


def test_deleting_a_goal_removes_what_was_put_aside_into_it(env: Env) -> None:
    a = env.login(env.user('alex'))
    main = make_account(a, 'Main', 'transaction', '5000')
    a.post('/budget', data={'new_category': 'Utilities', 'amount': '250'})
    with env.db() as db:
        cid = db.execute(select(Category.id).where(Category.name == 'Utilities')).scalar_one()
    goal = _goal(a, name='Pot', account_id=str(main), category_id=str(cid), keep_aside='true')
    a.post(f'/goals/{goal}/move', data={'amount': '250', 'as_spent': 'true'})
    a.post(f'/goals/{goal}/delete')
    with env.db() as db:
        assert db.execute(select(func.count(BudgetSpend.id))).scalar() == 0
        assert db.execute(select(func.count(GoalMove.id))).scalar() == 0
