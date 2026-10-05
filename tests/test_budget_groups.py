"""Budget lines grouped under headings and put in order by hand."""

from __future__ import annotations

import re

from sqlalchemy import select

from fiscus.domain import arrange
from fiscus.models import BudgetGroup, Category
from tests.conftest import Env


def test_moved_swaps_with_a_neighbour_and_stops_at_the_ends() -> None:
    assert arrange.moved([1, 2, 3], 2, -1) == [2, 1, 3]
    assert arrange.moved([1, 2, 3], 2, 1) == [1, 3, 2]
    assert arrange.moved([1, 2, 3], 1, -1) == [1, 2, 3]
    assert arrange.moved([1, 2, 3], 3, 1) == [1, 2, 3]
    assert arrange.moved([1, 2, 3], 9, 1) == [1, 2, 3]


def _order(page: str) -> list[str]:
    """Line and group names in the order the budget table shows them."""
    body = page[page.index('<tbody>') : page.index('</tbody>')]
    return re.findall(r'id="(?:line|group)-(?:\d+|none)">\s*<t[dh]>\s*([^<{\n]+?)\s*(?:<|\n)', body)


def _cat(env: Env, name: str) -> int:
    with env.db() as db:
        return int(db.execute(select(Category.id).where(Category.name == name)).scalar_one())


def test_lines_are_grouped_and_put_in_order(env: Env) -> None:
    a = env.login(env.user('alex'))
    for name, amount in (('Groceries', '600'), ('Power', '200'), ('Water', '100'), ('Fun', '50')):
        a.post('/budget', data={'new_category': name, 'amount': amount})
    # With nothing moved, bigger budgets come first.
    assert _order(a.get('/budget').text) == ['Groceries', 'Power', 'Water', 'Fun']

    a.post(f'/budget/{_cat(env, "Fun")}/move', data={'direction': 'up'})
    assert _order(a.get('/budget').text) == ['Groceries', 'Power', 'Fun', 'Water']
    resp = a.post(f'/budget/{_cat(env, "Groceries")}/move', data={'direction': 'up'})
    assert resp.headers['location'].endswith(f'#line-{_cat(env, "Groceries")}')

    # A group, with Power and Water in it, shows first with its own totals.
    a.post('/budget/groups', data={'name': 'Utilities'})
    with env.db() as db:
        gid = db.execute(select(BudgetGroup.id)).scalar_one()
    for name, amount in (('Power', '200'), ('Water', '100')):
        a.post(
            '/budget',
            data={'category_id': str(_cat(env, name)), 'amount': amount, 'group_id': str(gid)},
        )
    page = a.get('/budget').text
    assert _order(page) == ['Utilities', 'Power', 'Water', 'Not in a group', 'Groceries', 'Fun']
    assert '$300.00' in page
    a.post(f'/budget/{_cat(env, "Water")}/move', data={'direction': 'up'})
    assert _order(a.get('/budget').text)[:3] == ['Utilities', 'Water', 'Power']

    # A second group, moved above the first; then renamed and removed.
    a.post('/budget/groups', data={'name': 'Fun stuff'})
    with env.db() as db:
        fun = db.execute(select(BudgetGroup.id).where(BudgetGroup.name == 'Fun stuff')).scalar()
    a.post(f'/budget/groups/{fun}/move', data={'direction': 'up'})
    assert _order(a.get('/budget').text)[0] == 'Fun stuff'
    a.post(f'/budget/groups/{gid}', data={'name': 'Bills'})
    a.post(f'/budget/groups/{gid}/delete')
    order = _order(a.get('/budget').text)
    assert 'Bills' not in order and {'Power', 'Water'} <= set(order)
    with env.db() as db:
        power = db.get(Category, _cat(env, 'Power'))
        assert power is not None and power.budget_group_id is None

    # Editing a line without a group choice leaves its group alone.
    a.post(
        '/budget',
        data={'category_id': str(_cat(env, 'Fun')), 'amount': '50', 'group_id': str(fun)},
    )
    a.post('/budget', data={'category_id': str(_cat(env, 'Fun')), 'amount': '60'})
    with env.db() as db:
        cat = db.get(Category, _cat(env, 'Fun'))
        assert cat is not None and cat.budget_group_id == fun


def test_a_group_needs_a_name(env: Env) -> None:
    a = env.login(env.user('alex'))
    resp = a.post('/budget/groups', data={'name': '  '}, follow_redirects=True)
    with env.db() as db:
        assert db.execute(select(BudgetGroup)).first() is None
    assert resp.status_code == 200
