"""Tax paid with a BAS (D75): PAYG instalments counted against the year's tax."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from fiscus.domain import tax
from fiscus.models import Goal, GoalMove, TaxPayment, Transaction, User
from fiscus.money import round_div
from fiscus.services import goals, ledger, taxes
from tests.conftest import CsrfClient, Env
from tests.test_bills import make_account

TODAY = date(2026, 10, 1)  # day 93 of 2026-27


def me(db: Session, user: User) -> User:
    found = db.get(User, user.id)
    assert found is not None
    return found


def household(env: Env) -> tuple[CsrfClient, User, int, int]:
    """Business pay with nothing withheld, and a tax goal holding $3,000."""
    alex = env.user('alex')
    a = env.login(alex)
    acct = make_account(a, opening='10000')
    resp = a.post(
        '/pay/schedules',
        data={
            'name': 'Studio',
            'frequency': 'monthly',
            'anchor_date': '2026-07-15',
            'net': '6000',
            'gross': '6000',
            'tax_withheld': '0',
            'account_id': str(acct),
        },
    )
    assert resp.status_code == 303
    resp = a.post(
        '/goals',
        data={'name': 'FFC Tax', 'account_id': str(acct), 'keep_aside': 'true', 'is_tax': 'true'},
    )
    goal = int(resp.headers['location'].rsplit('/', 1)[1])
    a.post(f'/goals/{goal}/move', data={'amount': '3000', 'on': '2026-09-01'})
    return a, alex, acct, goal


def pay(client: CsrfClient, path: str = '/tax/payments', **form: str) -> str:
    resp = client.post(path, data=form)
    assert resp.status_code == 303, resp.text
    return resp.headers.get('set-cookie', '')


def payments(env: Env) -> list[TaxPayment]:
    with env.db() as db:
        found = db.execute(select(TaxPayment).order_by(TaxPayment.id)).scalars().all()
        db.expunge_all()
        return list(found)


# ------------------------------------------------------------------ domain


def test_quarters() -> None:
    assert tax.quarter_of(date(2026, 8, 31)) == date(2026, 7, 1)
    assert tax.quarter_of(date(2026, 12, 1)) == date(2026, 10, 1)
    assert tax.quarter_before(date(2026, 10, 20)) == date(2026, 7, 1)
    assert tax.quarter_before(date(2026, 2, 1)) == date(2025, 10, 1)
    assert tax.quarter_before(date(2026, 2, 1), 2) == date(2025, 7, 1)
    assert tax.quarter_label(date(2026, 4, 1)) == 'April to June 2026'
    # April to June 2026 is in 2025-26, though it is paid in July.
    assert tax.fy_label(date(2026, 4, 1)) == '2025-26'


def test_tax_paid_with_a_bas_comes_off_what_is_owed() -> None:
    year = tax.load_year('2026-27')
    before = tax.position(year, 72000_00, 0, 0)
    after = tax.position(year, 72000_00, 0, 0, paid=3000_00)
    assert after.due_cents == before.owing_cents == before.due_cents
    assert after.owing_cents == before.owing_cents - 3000_00
    part = tax.share_of(after, 18000_00, 0, paid=1000_00)
    assert part.paid_cents == 1000_00 and part.owing_cents == part.due_cents - 1000_00


# ---------------------------------------------------------------- the tax page


def test_a_bas_payment_out_of_the_tax_goal(env: Env) -> None:
    a, alex, acct, goal = household(env)
    with env.db() as db:
        due = taxes.view(db, me(db, alex), TODAY).projected.due_cents
    assert due > 0
    page = a.get('/tax').text
    assert 'No BAS payments yet' in page
    # The quarter just gone and the tax goal are the defaults.
    assert '<option value="2026-07-01" selected>July to September 2026</option>' in page
    assert '<option value="goal" selected>Out of FFC Tax, your tax goal</option>' in page

    flash = pay(a, amount='1200', quarter='2026-07-01', paid_on='2026-09-30', paid_from='goal')
    assert 'paid%20with%20your%20BAS%20for%20July%20to%20September%202026' in flash
    [row] = payments(env)
    assert row.goal_move_id is not None
    with env.db() as db:
        user = me(db, alex)
        assert goals.held(db, user)[goal] == 1800_00
        # Out of the goal and off its account (D73).
        assert ledger.balances(db, user, TODAY)[acct] == 10000_00 - 1200_00
        v = taxes.view(db, user, TODAY)
        assert v.projected.paid_cents == 1200_00 and v.ytd.paid_cents == 1200_00
        assert v.year_end_set_aside_cents == due - 1200_00
        # Saved evenly, less what was paid: not the rest saved evenly.
        assert v.should_set_aside_cents == round_div(due * 93, 365) - 1200_00
        assert v.set_aside_actual_cents == 1800_00
    page = a.get('/tax').text
    assert 'Paid 30 Sep 2026, out of FFC Tax' in page
    assert 'Paid with your BAS' in page and 'after the $1,200.00 paid with your BAS' in page

    # Changed: the goal move and its transaction follow, and keep when they
    # were put in.
    with env.db() as db:
        put_in = db.execute(select(Transaction.created_at)).scalar_one()
    env.clock.set(env.clock.now().replace(hour=11))
    pay(
        a,
        f'/tax/payments/{row.id}',
        amount='1500',
        quarter='2026-07-01',
        paid_on='2026-10-01',
        paid_from='goal',
    )
    with env.db() as db:
        user = me(db, alex)
        move = db.get(GoalMove, row.goal_move_id)
        assert move is not None and (move.amount_cents, move.date) == (-1500_00, TODAY)
        txn = db.execute(select(Transaction)).scalar_one()
        assert (txn.amount_cents, txn.created_at) == (-1500_00, put_in)
        assert goals.held(db, user)[goal] == 1500_00

    # More than the goal holds is refused.
    resp = a.post(
        '/tax/payments',
        data={'amount': '1600', 'quarter': '2026-07-01', 'paid_from': 'goal'},
    )
    assert 'FFC%20Tax%20holds%20%241%2C500.00' in resp.headers['set-cookie']
    assert len(payments(env)) == 1

    # Moved to no account: the goal gets it back and no balance moves.
    pay(a, f'/tax/payments/{row.id}', amount='1500', quarter='2026-07-01', paid_from='')
    [row] = payments(env)
    assert row.goal_move_id is None
    with env.db() as db:
        user = me(db, alex)
        assert goals.held(db, user)[goal] == 3000_00
        assert ledger.balances(db, user, TODAY)[acct] == 10000_00
        assert taxes.view(db, user, TODAY).projected.paid_cents == 1500_00


def test_a_bas_payment_from_an_account_and_for_last_year(env: Env) -> None:
    a, alex, _acct, _goal = household(env)
    other = make_account(a, 'Tax account', 'savings', '5000')
    # April to June, paid in July: last financial year's tax.
    pay(
        a,
        amount='900',
        quarter='2026-04-01',
        paid_on='2026-07-20',
        paid_from=f'account:{other}',
    )
    [row] = payments(env)
    with env.db() as db:
        user = me(db, alex)
        assert ledger.balances(db, user, TODAY)[other] == 4100_00
        assert ledger.linked(db, user, row) is not None
        v = taxes.view(db, user, TODAY)
        assert v.projected.paid_cents == 0
        assert taxes.paid_for(db, user, date(2025, 7, 1), date(2026, 6, 30)) == 900_00
    page = a.get('/tax').text
    assert 'April to June 2026' in page and 'counts in 2025-26' in page
    form = a.get(f'/tax?payment={row.id}').text
    assert 'Change the payment for April to June 2026' in form
    assert f'<option value="account:{other}" selected>' in form
    assert '<option value="2026-04-01" selected>' in form
    # Removed: the account has it back.
    flash = a.post(f'/tax/payments/{row.id}/delete').headers['set-cookie']
    assert 'BAS%20payment%20removed' in flash
    assert payments(env) == []
    with env.db() as db:
        assert ledger.balances(db, me(db, alex), TODAY)[other] == 5000_00


def test_the_goal_entry_and_the_payment_go_together(env: Env) -> None:
    a, alex, acct, goal = household(env)
    pay(a, amount='1000', quarter='2026-07-01', paid_from='goal')
    [row] = payments(env)
    # Removing the goal entry removes the payment it made.
    a.post(f'/goals/{goal}/moves/{row.goal_move_id}/delete')
    assert payments(env) == []
    # Deleting the goal keeps the payment, and the money stays off the account.
    pay(a, amount='1000', quarter='2026-07-01', paid_from='goal')
    a.post(f'/goals/{goal}/delete')
    [row] = payments(env)
    assert row.goal_move_id is None
    with env.db() as db:
        user = me(db, alex)
        assert db.get(Goal, goal) is None
        assert ledger.balances(db, user, TODAY)[acct] == 10000_00 - 1000_00
        assert taxes.view(db, user, TODAY).projected.paid_cents == 1000_00
    # With no tax goal, the goal is not offered and not taken.
    page = a.get('/tax').text
    assert 'value="goal"' not in page
    resp = a.post(
        '/tax/payments', data={'amount': '5', 'quarter': '2026-07-01', 'paid_from': 'goal'}
    )
    assert 'Mark%20a%20goal%20as%20your%20tax%20money' in resp.headers['set-cookie']
    resp = a.post('/tax/payments', data={'amount': '5', 'quarter': '2026-07-02'})
    assert 'Choose%20the%20quarter' in resp.headers['set-cookie']
    assert len(payments(env)) == 1


def test_another_persons_bas_payments_are_out_of_reach(env: Env) -> None:
    a, _alex, acct, _goal = household(env)
    pay(a, amount='100', quarter='2026-07-01', paid_from=f'account:{acct}')
    [row] = payments(env)
    b = env.login(env.user('blake'))
    assert b.get(f'/tax?payment={row.id}').status_code == 404
    assert b.post(f'/tax/payments/{row.id}/delete').status_code == 404
    form = {'amount': '1', 'quarter': '2026-07-01', 'paid_from': ''}
    assert b.post(f'/tax/payments/{row.id}', data=form).status_code == 404
    resp = b.post('/tax/payments', data=form | {'paid_from': f'account:{acct}'})
    assert resp.status_code == 404
    assert 'No BAS payments yet' in b.get('/tax').text
    assert len(payments(env)) == 1
