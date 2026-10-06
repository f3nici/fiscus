"""Budget lines grouped under headings and put in order by hand."""

from __future__ import annotations

import re
from urllib.parse import unquote

from sqlalchemy import select

from fiscus.domain import arrange
from fiscus.models import BudgetGroup, BudgetSpend, Category
from tests.conftest import Env


def test_moved_swaps_with_a_neighbour_and_stops_at_the_ends() -> None:
    assert arrange.moved([1, 2, 3], 2, -1) == [2, 1, 3]
    assert arrange.moved([1, 2, 3], 2, 1) == [1, 3, 2]
    assert arrange.moved([1, 2, 3], 1, -1) == [1, 2, 3]
    assert arrange.moved([1, 2, 3], 3, 1) == [1, 2, 3]
    assert arrange.moved([1, 2, 3], 9, 1) == [1, 2, 3]


def _order(page: str) -> list[str]:
    """Line and group names in the order the budget table shows them."""
    body = page[page.index('<tbody') : page.rindex('</tbody>')]
    return re.findall(
        r'id="(?:line|group)-(?:\d+|none)">\s*<t[dh]>\s*'
        r'(?:<form(?:(?!</form>).)*?<span class="caret"[^>]*>.</span>\s*)?([^<{\n]+?)\s*(?:<|\n)',
        body,
        re.S,
    )


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


def test_a_group_folds_to_its_totals(env: Env) -> None:
    a = env.login(env.user('alex'))
    a.post('/budget', data={'new_category': 'Power', 'amount': '200'})
    a.post('/budget', data={'new_category': 'Fun', 'amount': '50'})
    a.post('/budget/groups', data={'name': 'Utilities'})
    with env.db() as db:
        gid = db.execute(select(BudgetGroup.id)).scalar_one()
    a.post(
        '/budget',
        data={'category_id': str(_cat(env, 'Power')), 'amount': '200', 'group_id': str(gid)},
    )

    resp = a.post(f'/budget/groups/{gid}/fold', data={'collapsed': 'true'})
    assert resp.headers['location'] == f'/budget#group-{gid}'
    page = a.get('/budget').text
    # The heading and its total stay; the line in it is gone.
    assert _order(page) == ['Utilities', 'Not in a group', 'Fun']
    assert '1 line, hidden' in page and '$200.00' in page
    assert 'Show every line' in page

    a.post(f'/budget/groups/{gid}/fold', data={'collapsed': 'false'})
    assert _order(a.get('/budget').text) == ['Utilities', 'Power', 'Not in a group', 'Fun']

    # All at once, and putting a line in a folded group opens it.
    a.post('/budget/groups/fold', data={'collapsed': 'true'})
    assert 'Power' not in _order(a.get('/budget').text)
    a.post(
        '/budget', data={'category_id': str(_cat(env, 'Fun')), 'amount': '50', 'group_id': str(gid)}
    )
    assert _order(a.get('/budget').text) == ['Utilities', 'Power', 'Fun']
    a.post('/budget/groups/fold', data={'collapsed': 'false'})
    with env.db() as db:
        assert db.get(BudgetGroup, gid).collapsed is False  # type: ignore[union-attr]


def test_a_group_needs_a_name(env: Env) -> None:
    a = env.login(env.user('alex'))
    resp = a.post('/budget/groups', data={'name': '  '}, follow_redirects=True)
    with env.db() as db:
        assert db.execute(select(BudgetGroup)).first() is None
    assert resp.status_code == 200


def test_a_budget_line_can_be_renamed(env: Env) -> None:
    a = env.login(env.user('alex'))
    a.post('/budget', data={'new_category': 'Food shop', 'amount': '600'})
    a.post('/budget', data={'new_category': 'Petrol money', 'amount': '200'})
    cid = _cat(env, 'Food shop')

    resp = a.post(
        '/budget', data={'category_id': str(cid), 'name': ' Weekly shop ', 'amount': '650'}
    )
    assert resp.status_code == 303
    assert unquote(resp.cookies['fiscus_flash']).startswith('Food shop renamed to Weekly shop.')
    assert resp.headers['location'] == f'/budget#line-{cid}'
    page = a.get('/budget').text
    with env.db() as db:
        cat = db.get(Category, cid)
        assert cat is not None and cat.name == 'Weekly shop'
    assert '$650.00' in page

    # Only the case changed: still a rename of the same line.
    a.post('/budget', data={'category_id': str(cid), 'name': 'WEEKLY SHOP', 'amount': '650'})
    with env.db() as db:
        cat = db.get(Category, cid)
        assert cat is not None and cat.name == 'WEEKLY SHOP'

    # Another category's name, in any case, is refused and nothing changes.
    resp = a.post('/budget', data={'category_id': str(cid), 'name': 'petrol money', 'amount': '1'})
    assert unquote(resp.cookies['fiscus_flash']) == 'Petrol money is already a budget line'
    with env.db() as db:
        cat = db.get(Category, cid)
        assert cat is not None and cat.name == 'WEEKLY SHOP'
    assert '$650.00' in a.get('/budget').text

    # Without a name the line keeps its own.
    a.post('/budget', data={'category_id': str(cid), 'amount': '700'})
    with env.db() as db:
        cat = db.get(Category, cid)
        assert cat is not None and cat.name == 'WEEKLY SHOP'


def test_a_line_can_take_the_name_of_one_no_longer_in_the_budget(env: Env) -> None:
    a = env.login(env.user('alex'))
    a.post('/budget', data={'new_category': 'Groceries', 'amount': '600'})
    a.post('/budget', data={'new_category': 'Food', 'amount': '500'})
    old, new = _cat(env, 'Groceries'), _cat(env, 'Food')
    a.post('/budget/spend', data={'category_id': str(old), 'amount': '20'})
    a.post(f'/budget/{old}/remove')

    resp = a.post('/budget', data={'category_id': str(new), 'name': 'groceries', 'amount': '500'})
    assert unquote(resp.cookies['fiscus_flash']).startswith('Food renamed to groceries.')
    with env.db() as db:
        assert db.get(Category, new).name == 'groceries'  # type: ignore[union-attr]
        # The old one keeps its spending, under a name out of the way.
        assert db.get(Category, old).name == 'Groceries (old)'  # type: ignore[union-attr]

    # Again, and the next old name is numbered.
    a.post('/budget', data={'new_category': 'Fuel', 'amount': '100'})
    fuel = _cat(env, 'Fuel')
    a.post(f'/budget/{new}/remove')
    a.post('/budget', data={'category_id': str(fuel), 'name': 'Groceries', 'amount': '100'})
    with env.db() as db:
        assert db.get(Category, new).name == 'groceries (old 2)'  # type: ignore[union-attr]
        assert db.get(Category, fuel).name == 'Groceries'  # type: ignore[union-attr]


def test_each_save_comes_back_to_what_it_changed(env: Env) -> None:
    a = env.login(env.user('alex'))
    back = '/budget?month=2026-10'

    def goes(path: str, data: dict[str, str]) -> str:
        resp = a.post(path, data={'back': back, **data})
        assert resp.status_code == 303
        return str(resp.headers['location'])

    assert (
        goes('/budget', {'new_category': 'Fun', 'amount': '50'})
        == f'{back}#line-{_cat(env, "Fun")}'
    )
    fun = _cat(env, 'Fun')
    assert goes('/budget', {'amount': '50'}) == f'{back}#add-line'
    assert goes('/budget/spend', {'category_id': str(fun), 'amount': '5'}) == f'{back}#spend'
    with env.db() as db:
        spend = db.execute(select(BudgetSpend.id)).scalar_one()
    assert goes(f'/budget/spend/{spend}/delete', {}) == f'{back}#spent'
    assert goes('/budget/groups', {'name': 'Bills'}).startswith(f'{back}#group-')
    assert goes(f'/budget/{fun}/remove', {}) == f'{back}#lines'
    # A back address that already carries an anchor gets this one instead.
    resp = a.post(
        '/budget', data={'back': f'{back}#elsewhere', 'category_id': str(fun), 'amount': '9'}
    )
    assert resp.headers['location'] == f'{back}#line-{fun}'
    resp = a.post('/budget/start-day', data={'start_day': '15'})
    assert resp.headers['location'] == '/budget#start-day'
