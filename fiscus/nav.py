"""The screens in the menu, and the ones a person keeps on the bottom tabs (D67).

The sidebar, the menu that slides in on a phone and the bottom tabs all come
from SCREENS, so they cannot disagree about what a screen is called or which
one the page is on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from fiscus.domain import arrange


@dataclass(frozen=True)
class Screen:
    key: str
    href: str
    label: str
    # Under its icon on a bottom tab, where there is less room.
    tab_label: str
    icon: str
    # Other paths that are this screen, so its link shows as the current one.
    also: tuple[str, ...] = ()
    # Under "You" in the menu rather than with the screens.
    you: bool = False

    @property
    def match(self) -> tuple[str, ...]:
        return (self.href, *self.also)

    def current(self, path: str) -> bool:
        if self.href == '/':
            return path == '/'
        return any(path.startswith(p) for p in self.match)


SCREENS = (
    Screen('home', '/', 'Dashboard', 'Home', 'home'),
    Screen('calendar', '/calendar', 'Calendar', 'Calendar', 'calendar'),
    Screen('accounts', '/accounts', 'Accounts', 'Accounts', 'wallet'),
    Screen('bills', '/bills', 'Bills', 'Bills', 'receipt'),
    Screen('pay', '/pay', 'Pay', 'Pay', 'cash'),
    Screen('budget', '/budget', 'Budget', 'Budget', 'pie'),
    Screen('goals', '/goals', 'Goals', 'Goals', 'target'),
    Screen('forecast', '/forecast', 'Forecast', 'Forecast', 'chart', also=('/scenarios',)),
    Screen('loans', '/loans', 'Home loan', 'Loan', 'house'),
    Screen('tax', '/tax', 'Tax', 'Tax', 'percent'),
    Screen('categories', '/categories', 'Categories', 'Categories', 'tag'),
    Screen('settings', '/settings', 'Settings', 'Settings', 'cog', you=True),
)
BY_KEY = {s.key: s for s in SCREENS}

DEFAULT_TABS = ('home', 'calendar', 'budget', 'bills', 'accounts')
FEWEST_TABS = 2
MOST_TABS = 5


class TabsError(ValueError):
    pass


def changed(keys: tuple[str, ...], change: str) -> tuple[str, ...]:
    """The tabs after one press in the editor: 'earlier:tax', 'later:tax',
    'remove:tax', 'add:tax' or 'reset'. Applied to the tabs as saved, so a
    Settings page left open on another device moves only what it says."""
    if change == 'reset':
        return DEFAULT_TABS
    verb, _, key = change.partition(':')
    if key not in BY_KEY:
        raise TabsError('That is not a screen Fiscus has.')
    if verb in ('earlier', 'later'):
        out = arrange.moved(list(keys), key, -1 if verb == 'earlier' else 1)
    elif verb == 'remove':
        out = [k for k in keys if k != key]
    elif verb == 'add':
        out = list(keys) if key in keys else [*keys, key]
    else:
        raise TabsError('That is not a change the tabs take.')
    if not FEWEST_TABS <= len(out) <= MOST_TABS:
        raise TabsError(f'Keep between {FEWEST_TABS} and {MOST_TABS} tabs.')
    return tuple(out)


def stored(text: str) -> tuple[str, ...]:
    """The tabs a person saved (a JSON list), less any screen gone since, or
    the default when none are saved or too few are left."""
    try:
        saved = json.loads(text) if text else None
    except ValueError:
        saved = None
    if not isinstance(saved, list):
        return DEFAULT_TABS
    keys: list[str] = []
    for k in saved:
        if isinstance(k, str) and k in BY_KEY and k not in keys:
            keys.append(k)
    return tuple(keys[:MOST_TABS]) if len(keys) >= FEWEST_TABS else DEFAULT_TABS


def dump(keys: tuple[str, ...]) -> str:
    """What `stored` reads back; the default is saved as nothing, so a person
    who never chose follows it."""
    return '' if keys == DEFAULT_TABS else json.dumps(list(keys))


def screens(keys: tuple[str, ...]) -> tuple[Screen, ...]:
    return tuple(BY_KEY[k] for k in keys)


@dataclass(frozen=True)
class Tab:
    """A tab in the editor, and which of its buttons can do anything."""

    screen: Screen
    earlier: bool
    later: bool
    remove: bool


@dataclass(frozen=True)
class TabEditor:
    tabs: tuple[Tab, ...]
    # The screens not on a tab, to add.
    others: tuple[Screen, ...]
    full: bool
    is_default: bool


def editor(keys: tuple[str, ...]) -> TabEditor:
    n = len(keys)
    tabs = tuple(
        Tab(BY_KEY[k], earlier=i > 0, later=i < n - 1, remove=n > FEWEST_TABS)
        for i, k in enumerate(keys)
    )
    others = tuple(s for s in SCREENS if s.key not in keys)
    return TabEditor(tabs, others, n >= MOST_TABS, keys == DEFAULT_TABS)
