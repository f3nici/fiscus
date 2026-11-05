"""A home loan repayment marked paid from the budget comes off its account (D76).

ANZ's weekly loan of D70 and D71: settled Monday 26 October 2026, repaid
$914.63 on Thursdays from 5 November, out of Everyday, whose balance was
typed in as $5,000 on Sunday 1 November.
"""

from __future__ import annotations

import html
from datetime import UTC, date, datetime

from sqlalchemy import select

from fiscus.models import LoanRepayment, Transaction, User
from fiscus.services import calendar_events, forecasting, ledger, loans
from tests.conftest import CsrfClient, Env
from tests.test_bills import make_account

LOAN = {
    'name': 'Mortgage',
    'principal': '623774.90',
    'start_date': '2026-10-26',
    'term_years': '30',
    'repayment_frequency': 'weekly',
    'initial_rate': '6.59',
    'balance': '623774.90',
    'balance_date': '2026-10-26',
    'first_repayment_date': '2026-11-05',
}
PAID = '/loans/{loan}/repayments/{day}/paid'


def at(env: Env, day: date) -> None:
    """10:00 in Perth on `day`."""
    env.clock.set(datetime(day.year, day.month, day.day, 2, tzinfo=UTC))


def household(env: Env, *, tracked: bool = True) -> tuple[CsrfClient, User, int, int]:
    alex = env.user('alex')
    at(env, date(2026, 11, 1))
    a = env.login(alex)
    acct = make_account(a, opening='0')
    a.post(f'/accounts/{acct}/balance', data={'balance': '5000'})
    resp = a.post('/loans', data={**LOAN, 'repayment_account_id': str(acct) if tracked else '0'})
    loan = int(resp.headers['location'].rsplit('/', 1)[1])
    at(env, date(2026, 11, 5))
    return env.login(alex), alex, acct, loan


def balance(env: Env, user: User, acct: int) -> int:
    with env.db() as db:
        u = db.get(User, user.id)
        assert u is not None
        return ledger.balances(db, u, env.clock.today())[acct]


def free_in_accounts(page: str) -> str:
    """The Free in accounts now figure on the budget page."""
    part = page[page.index('Free in accounts now') :]
    part = part[part.index('class="value') :]
    return html.unescape(part[part.index('>') + 1 : part.index('</div>')])


def test_a_repayment_due_is_marked_paid_from_the_budget(env: Env) -> None:
    a, alex, acct, loan = household(env)
    page = a.get('/budget').text
    assert '0 of 4 repayments made this month' in page
    assert '$914.63 due today, not off Everyday yet' in page
    assert f'action="{PAID.format(loan=loan, day="2026-11-05")}"' in page
    # Still to spend: every repayment this month, so the $914.63 still in
    # Everyday is not free to spend.
    assert free_in_accounts(page) == '$1,341.48'
    resp = a.post(
        PAID.format(loan=loan, day='2026-11-05'),
        data={'back': '/budget?month=2026-11#loan-1'},
        follow_redirects=True,
    )
    assert (
        'Mortgage repayment of $914.63 on 5 Nov marked paid. Everyday is now $4,085.37'
        in html.unescape(resp.text)
    )
    assert balance(env, alex, acct) == 4_085_37
    page = a.get('/budget').text
    assert '1 of 4 repayments made this month' in page
    assert 'not off Everyday yet' not in page
    # The money left the account and the budget together: free is as it was.
    assert free_in_accounts(page) == '$1,341.48'
    with env.db() as db:
        txn = db.execute(
            select(Transaction).where(Transaction.loan_repayment_id.is_not(None))
        ).scalar_one()
        assert (txn.account_id, txn.date, txn.amount_cents, txn.description) == (
            acct,
            date(2026, 11, 5),
            -914_63,
            'Mortgage repayment',
        )
        assert txn.dedup_hash.startswith('loan-repayment:')
    # Marked twice is still once.
    a.post(PAID.format(loan=loan, day='2026-11-05'))
    assert balance(env, alex, acct) == 4_085_37
    # Not paid puts it back on the account, and it is due again.
    assert 'Not paid' in page and f'id="repaid-{loan}"' in page
    resp = a.post(f'/loans/{loan}/repayments/2026-11-05/unpaid', follow_redirects=True)
    assert 'Mortgage repayment on 5 Nov marked not paid. Everyday is now $5,000.00' in (
        html.unescape(resp.text)
    )
    assert balance(env, alex, acct) == 5_000_00
    assert '$914.63 due today, not off Everyday yet' in a.get('/budget').text
    with env.db() as db:
        assert db.execute(select(Transaction)).scalars().all() == []
    assert a.post(f'/loans/{loan}/repayments/2026-11-05/unpaid').status_code == 404


def test_the_forecast_takes_a_repayment_not_marked_off_tomorrow(env: Env) -> None:
    a, alex, acct, loan = household(env)
    with env.db() as db:
        u = db.get(User, alex.id)
        assert u is not None
        r = forecasting.base(db, env.clock, u, 1)
        assert r.accounts[acct][:3] == [5_000_00, 4_085_37, 4_085_37]
        # Free to spend today is the budget's: all four still to come.
        held = forecasting.still_to_spend(db, u, env.clock.today(), 30)
        assert held[:2] == [3_658_52, 2_743_89]
        db.commit()
    a.post(PAID.format(loan=loan, day='2026-11-05'))
    with env.db() as db:
        u = db.get(User, alex.id)
        assert u is not None
        r = forecasting.base(db, env.clock, u, 1)
        # Off today, and not again tomorrow; the next on Thursday week.
        assert r.accounts[acct][:3] == [4_085_37, 4_085_37, 4_085_37]
        assert r.accounts[acct][7] == 4_085_37 - 914_63
        held = forecasting.still_to_spend(db, u, env.clock.today(), 30)
        assert held[:2] == [2_743_89, 2_743_89]
        # Loan repayments are forecast from the loan, not as spending.
        assert forecasting.estimates(db, u, env.clock.today()) == []


def test_a_balance_typed_in_after_a_repayment_has_it(env: Env) -> None:
    a, alex, acct, loan = household(env)
    # Typed in on the day it is due, it may not have it yet.
    a.post(f'/accounts/{acct}/balance', data={'balance': '4085.37'})
    assert '$914.63 due today' in a.get('/budget').text
    # Typed in a day later, it has it: nothing to mark.
    at(env, date(2026, 11, 6))
    a = env.login(alex)
    a.post(f'/accounts/{acct}/balance', data={'balance': '4085.37'})
    page = a.get('/budget').text
    assert '1 of 4 repayments made this month' in page
    assert 'not off Everyday yet' not in page and 'Not paid' not in page
    assert free_in_accounts(page) == '$1,341.48'
    # Marked all the same, it moves nothing, and says so.
    resp = a.post(PAID.format(loan=loan, day='2026-11-05'), follow_redirects=True)
    assert 'The balance typed in for Everyday on 06 Nov already has it' in resp.text
    assert balance(env, alex, acct) == 4_085_37


def test_only_a_repayment_fallen_due_can_be_marked(env: Env) -> None:
    a, alex, acct, loan = household(env)
    resp = a.post(PAID.format(loan=loan, day='2026-11-12'), follow_redirects=True)
    assert 'That repayment has not fallen due yet' in resp.text
    # Not a repayment day, or not a day.
    assert a.post(PAID.format(loan=loan, day='2026-11-04')).status_code == 404
    assert a.post(PAID.format(loan=loan, day='5-11-2026')).status_code == 404
    assert balance(env, alex, acct) == 5_000_00
    # The way back is only ever to this site.
    resp = a.post(PAID.format(loan=loan, day='2026-11-05'), data={'back': '//evil.example'})
    assert resp.headers['location'] == '/budget'


def test_repayments_not_tracked_in_an_account_have_nothing_to_mark(env: Env) -> None:
    a, _alex, _acct, loan = household(env, tracked=False)
    page = a.get('/budget').text
    # Spent as each day passes, as before.
    assert '1 of 4 repayments made this month' in page
    assert 'not off' not in page and '/repayments/' not in page
    resp = a.post(PAID.format(loan=loan, day='2026-11-05'), follow_redirects=True)
    assert 'Choose the account Mortgage repayments come from on the loan first' in resp.text
    with env.db() as db:
        assert db.execute(select(LoanRepayment)).scalars().all() == []


def test_what_was_paid_is_kept_and_stays_off_when_the_loan_goes(env: Env) -> None:
    a, alex, acct, loan = household(env)
    a.post(PAID.format(loan=loan, day='2026-11-05'))
    # The bank asks more from now on: what was paid stays what it was.
    a.post(
        f'/loans/{loan}',
        data={**LOAN, 'repayment_override': '1000', 'repayment_account_id': str(acct)},
    )
    page = a.get('/budget').text
    # 914.63 paid and three of 1,000 to come.
    assert '$3,914.63' in page and '$914.63' in page
    a.post(f'/loans/{loan}/delete')
    # The money left the bank: deleting the loan does not bring it back.
    assert balance(env, alex, acct) == 4_085_37
    with env.db() as db:
        txn = db.execute(select(Transaction)).scalar_one()
        assert txn.loan_repayment_id is None
        assert txn.dedup_hash.startswith(ledger.KEPT)
        u = db.get(User, alex.id)
        assert u is not None
        # Still out of the spending average.
        assert forecasting.estimates(db, u, env.clock.today()) == []


def test_a_repayment_not_marked_shows_on_the_calendar_and_dashboard(env: Env) -> None:
    a, alex, _acct, loan = household(env)

    def status() -> str:
        with env.db() as db:
            u = db.get(User, alex.id)
            assert u is not None
            today = env.clock.today()
            found = calendar_events.events(db, u, date(2026, 11, 5), date(2026, 11, 5), today)
            return next(e.status for e in found if e.kind == 'loan')

    assert status() == 'due_soon'
    page = a.get('/').text
    assert 'A home loan repayment, $914.63 on 5 Nov, is still to mark paid' in page
    assert f'href="/budget#loan-{loan}"' in page
    # Two days on, it is overdue, and the agenda lists it above today.
    at(env, date(2026, 11, 7))
    a = env.login(alex)
    assert status() == 'overdue'
    assert 'Mortgage repayment, due 5 Nov' in a.get('/calendar?view=agenda').text
    a.post(PAID.format(loan=loan, day='2026-11-05'))
    assert status() == 'paid'
    assert 'still to mark paid' not in a.get('/').text
    assert 'Mortgage repayment, due 5 Nov' not in a.get('/calendar?view=agenda').text


def test_one_from_last_budget_month_is_marked_on_this_one(env: Env) -> None:
    a, alex, acct, loan = household(env)
    for day in ('2026-11-05', '2026-11-12', '2026-11-19'):
        at(env, date.fromisoformat(day))
        a = env.login(alex)
        a.post(PAID.format(loan=loan, day=day))
    at(env, date(2026, 12, 2))
    a = env.login(alex)
    page = a.get('/budget').text
    # December's own figures: none made yet, the five Thursdays to come.
    assert '0 of 5 repayments made this month' in page
    assert '$914.63 due 26 Nov, not off Everyday yet' in page
    a.post(PAID.format(loan=loan, day='2026-11-26'))
    assert balance(env, alex, acct) == 5_000_00 - 4 * 914_63
    page = a.get('/budget?month=2026-11').text
    assert '4 of 4 repayments made this month' in page
    with env.db() as db:
        u = db.get(User, alex.id)
        assert u is not None
        assert loans.due(db, u, env.clock.today()) == []
