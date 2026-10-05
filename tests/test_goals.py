"""Savings goals: money kept aside inside an account, off the free money, not in the budget."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from fiscus.domain import goals as calc
from fiscus.models import Account, Goal, GoalMove
from tests.conftest import Env
from tests.test_imports_bills import make_account


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


def _goal(client: object, **data: str) -> int:
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
