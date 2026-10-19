"""The menu and the bottom tabs (D67)."""

from __future__ import annotations

import re

import pytest

from fiscus import nav
from fiscus.models import User, UserSettings
from tests.conftest import Env


def bottom_tabs(html: str) -> list[str]:
    bar = re.search(r'<nav class="bottom-nav".*?</nav>', html, re.S)
    assert bar is not None
    return re.findall(r'href="([^"]+)"', bar.group(0))


def current_tabs(html: str) -> list[str]:
    bar = re.search(r'<nav class="bottom-nav".*?</nav>', html, re.S)
    assert bar is not None
    return re.findall(r'href="([^"]+)"[^>]*class="active"', bar.group(0))


# ------------------------------------------------------------------ domain


def test_one_change_at_a_time() -> None:
    keys = ('home', 'tax', 'pay')
    assert nav.changed(keys, 'earlier:tax') == ('tax', 'home', 'pay')
    assert nav.changed(keys, 'later:tax') == ('home', 'pay', 'tax')
    assert nav.changed(keys, 'earlier:home') == keys  # already first
    assert nav.changed(keys, 'remove:tax') == ('home', 'pay')
    assert nav.changed(keys, 'add:bills') == ('home', 'tax', 'pay', 'bills')
    assert nav.changed(keys, 'reset') == nav.DEFAULT_TABS
    # From a page that was already out of date: nothing to move or add twice.
    assert nav.changed(keys, 'later:goals') == keys
    assert nav.changed(keys, 'add:tax') == keys
    assert nav.changed(keys, 'remove:goals') == keys
    with pytest.raises(nav.TabsError, match='between 2 and 5'):
        nav.changed(('home', 'tax'), 'remove:tax')
    with pytest.raises(nav.TabsError, match='between 2 and 5'):
        nav.changed(nav.DEFAULT_TABS, 'add:tax')
    for bad in ('', 'add:admin', 'swap:tax', 'tax'):
        with pytest.raises(nav.TabsError):
            nav.changed(keys, bad)


def test_stored_falls_back_to_the_default() -> None:
    assert nav.stored('') == nav.DEFAULT_TABS
    assert nav.stored('not json') == nav.DEFAULT_TABS
    assert nav.stored('{"home": true}') == nav.DEFAULT_TABS
    assert nav.stored('["tax", "pay"]') == ('tax', 'pay')
    # A screen gone since is left out; too few left is the default again.
    assert nav.stored('["tax", "gone", "pay", "tax"]') == ('tax', 'pay')
    assert nav.stored('["tax", "gone"]') == nav.DEFAULT_TABS
    assert nav.stored(nav.dump(('goals', 'loans'))) == ('goals', 'loans')
    assert nav.dump(nav.DEFAULT_TABS) == ''


def test_the_editor_turns_off_what_cannot_change() -> None:
    first, middle, last = nav.editor(('home', 'tax', 'pay')).tabs
    assert (first.earlier, first.later, first.remove) == (False, True, True)
    assert (middle.earlier, middle.later) == (True, True)
    assert (last.earlier, last.later) == (True, False)
    ed = nav.editor(('home', 'tax', 'pay'))
    assert [s.key for s in ed.others][:2] == ['calendar', 'accounts']
    assert not ed.full and not ed.is_default
    # Two left: none comes off. Five: none goes on.
    assert not any(t.remove for t in nav.editor(('home', 'tax')).tabs)
    full = nav.editor(nav.DEFAULT_TABS)
    assert full.full and full.is_default


def test_a_screen_is_current_under_its_paths() -> None:
    home, forecast = nav.BY_KEY['home'], nav.BY_KEY['forecast']
    assert home.current('/') and not home.current('/bills')
    assert forecast.current('/forecast') and forecast.current('/scenarios/3')
    assert not forecast.current('/')


# ------------------------------------------------------------------ routes


def test_the_default_tabs_and_the_menu(env: Env) -> None:
    client = env.login(env.user('alex'))
    page = client.get('/bills').text
    assert bottom_tabs(page) == ['/', '/calendar', '/budget', '/bills', '/accounts']
    assert current_tabs(page) == ['/bills']
    # No More tab: the menu button opens the panel, with every screen in it.
    assert '/more' not in bottom_tabs(page)
    menu = re.search(r'<dialog class="drawer".*?</dialog>', page, re.S)
    assert menu is not None
    for s in nav.SCREENS:
        assert f'href="{s.href}"' in menu.group(0)
    assert 'action="/auth/logout"' in menu.group(0)
    # The page it falls back to lists them too.
    more = client.get('/more').text
    assert all(f'href="{s.href}"' in more for s in nav.SCREENS)


def set_tabs(env: Env, user: User, keys: tuple[str, ...]) -> None:
    with env.db() as db:
        prefs = db.get(UserSettings, user.id) or UserSettings(user_id=user.id)
        prefs.tabs = nav.dump(keys)
        db.add(prefs)
        db.commit()


def test_tabs_are_chosen_and_kept(env: Env) -> None:
    alex = env.user('alex')
    client = env.login(alex)
    with env.db() as db:
        before = db.get(User, alex.id).data_version  # type: ignore[union-attr]
    resp = client.post('/settings/tabs', data={'change': 'add:tax'})
    assert resp.status_code == 400  # five already
    for change in ('remove:bills', 'remove:budget', 'add:tax', 'earlier:tax', 'later:home'):
        resp = client.post('/settings/tabs', data={'change': change})
        assert resp.status_code == 303 and resp.headers['location'] == '/settings#tabs'
    page = client.get('/scenarios').text
    assert bottom_tabs(page) == ['/calendar', '/', '/tax', '/accounts']
    assert current_tabs(page) == []
    assert current_tabs(client.get('/accounts/new').text) == ['/accounts']
    with env.db() as db:
        prefs = db.get(UserSettings, alex.id)
        assert prefs is not None and prefs.tabs == '["calendar", "home", "tax", "accounts"]'
        # No forecast reads the tabs, so a cached one stays good.
        assert db.get(User, alex.id).data_version == before  # type: ignore[union-attr]
    # Back to the default is saved as nothing, so it follows the default.
    client.post('/settings/tabs', data={'change': 'reset'})
    with env.db() as db:
        prefs = db.get(UserSettings, alex.id)
        assert prefs is not None and prefs.tabs == ''


def test_a_page_left_open_changes_only_what_it_says(env: Env) -> None:
    """Settings open on the computer, a tab added on the phone since: the
    computer's next press keeps the phone's tab."""
    alex = env.user('alex')
    computer = env.login(alex)
    phone = env.login(alex)
    set_tabs(env, alex, ('home', 'bills'))
    computer.get('/settings')
    phone.post('/settings/tabs', data={'change': 'add:tax'})
    computer.post('/settings/tabs', data={'change': 'later:home'})
    assert bottom_tabs(phone.get('/').text) == ['/bills', '/', '/tax']


def test_tabs_out_of_bounds_are_refused(env: Env) -> None:
    alex = env.user('alex')
    client = env.login(alex)
    set_tabs(env, alex, ('tax', 'pay'))
    for bad in ('remove:tax', 'add:users', 'tax,pay,home', ''):
        resp = client.post('/settings/tabs', data={'change': bad})
        assert resp.status_code == 400, bad
        assert 'id="tabs"' in resp.text
    assert bottom_tabs(client.get('/').text) == ['/tax', '/pay']


def test_tabs_are_each_persons_own(env: Env) -> None:
    alex = env.login(env.user('alex'))
    sam = env.login(env.user('sam'))
    alex.post('/settings/tabs', data={'change': 'remove:accounts'})
    assert bottom_tabs(alex.get('/').text) == ['/', '/calendar', '/budget', '/bills']
    assert bottom_tabs(sam.get('/').text) == ['/', '/calendar', '/budget', '/bills', '/accounts']


def test_the_settings_page_edits_the_tabs(env: Env) -> None:
    alex = env.user('alex')
    client = env.login(alex)
    set_tabs(env, alex, ('home', 'tax', 'pay'))
    page = client.get('/settings').text
    card = re.search(r'<div class="card" id="tabs">.*?</form>', page, re.S)
    assert card is not None
    values = re.findall(r'name="change" value="([^"]*)"', card.group(0))
    assert {'earlier:tax', 'later:tax', 'remove:tax', 'add:bills', 'reset'} <= set(values)
    assert 'add:tax' not in values
