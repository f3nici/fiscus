"""Bills: their pages, states and payments."""

from __future__ import annotations

import csv
import hashlib
import io
from datetime import UTC, date, datetime

from sqlalchemy import func, select

from fiscus.clock import utcnow_naive
from fiscus.models import Account, Bill, BillPayment, Budget, Category, Transaction, User
from fiscus.money import parse_cents
from fiscus.services import budgeting, reminders
from tests.conftest import CsrfClient, Env

ANZ_SEPT = (
    '01/09/2026,"-4.50","EFTPOS COFFEE CO"\n'
    '01/09/2026,"-4.50","EFTPOS COFFEE CO"\n'
    '02/09/2026,"2500.00","PAY/SALARY ACME PTY LTD"\n'
    '15/09/2026,"-121.40","ORIGIN ENERGY BPAY 1234"\n'
)


def make_account(
    client: CsrfClient, name: str = 'Everyday', type_: str = 'transaction', opening: str = '1000'
) -> int:
    resp = client.post('/accounts', data={'name': name, 'type': type_, 'opening_balance': opening})
    assert resp.status_code == 303, resp.text
    return int(resp.headers['location'].rsplit('/', 1)[1])


def add_transactions(
    client: CsrfClient, account_id: int, text: str, category_id: int | None = None
) -> list[int]:
    """Transactions in an account, as Square sync and statement imports left
    them before they were removed: one per line of date (DD/MM/YYYY), amount
    and description."""
    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    with ctx.sessions() as db:
        acct = db.get(Account, account_id)
        assert acct is not None
        user = db.get(User, acct.user_id)
        assert user is not None
        created = []
        for n, row in enumerate(csv.reader(io.StringIO(text))):
            if not row:
                continue
            day, amount, description = row[0], row[1], row[2]
            key = f'{account_id}|{day}|{amount}|{description}|{n}'
            t = Transaction(
                user_id=user.id,
                account_id=acct.id,
                date=datetime.strptime(day, '%d/%m/%Y').date(),
                amount_cents=parse_cents(amount),
                description=description,
                category_id=category_id,
                dedup_hash=hashlib.sha256(key.encode()).hexdigest(),
                created_at=utcnow_naive(ctx.clock),
            )
            db.add(t)
            created.append(t)
        db.commit()
        return [t.id for t in created]


def count(env: Env, model: type, **where: object) -> int:
    with env.db() as db:
        q = select(func.count()).select_from(model)
        for k, v in where.items():
            q = q.where(getattr(model, k) == v)
        return int(db.scalar(q) or 0)


def create_bill(client: CsrfClient, **fields: str | list[str]) -> int:
    data: dict[str, str | list[str]] = {
        'name': 'Electricity',
        'amount': '121.40',
        'frequency': 'monthly',
        'next_due': '2026-09-15',
    }
    data.update(fields)
    resp = client.post('/bills', data=data)
    assert resp.status_code == 303, resp.text
    return int(resp.headers['location'].rsplit('/', 1)[1])


def test_bill_states_overdue_due_soon_paid(env: Env) -> None:
    client = env.login(env.user('alex'))
    bill = create_bill(client, next_due='2026-09-20')
    page = client.get('/bills')
    assert 'Overdue' in page.text  # 20 Sep unpaid, today is 1 Oct
    resp = client.post(f'/bills/{bill}/occurrence/2026-09-20/paid', data={'back': '/bills'})
    assert resp.status_code == 303
    assert 'was due' not in client.get('/bills').text
    # 20 Oct is upcoming, not due soon (more than 7 days away).
    sheet = client.get(f'/bills/{bill}/occurrence/2026-10-20')
    assert 'Upcoming' in sheet.text
    # A date that is not an occurrence is a 404.
    assert client.get(f'/bills/{bill}/occurrence/2026-10-21').status_code == 404


def test_a_bill_is_its_owners_alone(env: Env) -> None:
    """Bills are not shared (D52): the form has no one to share with, and
    naming someone the old way changes nothing for them."""
    alex = env.user('alex')
    sam = env.user('sam')
    a, s = env.login(alex), env.login(sam)
    acct = make_account(a)
    with env.db() as db:
        utilities = db.execute(
            select(Category.id).where(Category.user_id == alex.id, Category.name == 'Utilities')
        ).scalar_one()
    form = a.get('/bills/new').text
    assert 'Shared with' not in form and 'split' not in form.lower()
    bill = create_bill(
        a,
        name='Internet',
        amount='90',
        next_due='2026-10-10',
        account_id=str(acct),
        category_id=str(utilities),
        split_mode='percent',
        participants=[str(sam.id)],
        **{f'split_{alex.id}': '60', f'split_{sam.id}': '40'},
    )
    page = a.get(f'/bills/{bill}').text
    assert 'Everyday' in page and 'Utilities' in page and '$90.00' in page
    assert 'Who pays' not in page and 'Your share' not in page
    listed = a.get('/bills').text
    assert 'Your share' not in listed and 'Shared' not in listed and '$90.00' in listed
    # Sam neither sees it nor can reach it.
    assert 'Internet' not in s.get('/bills').text
    assert s.get(f'/bills/{bill}').status_code == 404
    assert s.post(f'/bills/{bill}/occurrence/2026-10-10/paid').status_code == 404
    # The edit form keeps its account and category.
    edit = a.get(f'/bills/{bill}/edit').text
    assert f'<option value="{acct}" selected>' in edit
    assert f'<option value="{utilities}" selected>' in edit
    # So does a new bill's form sent back with a mistake in it.
    wrong = a.post(
        '/bills',
        data={
            'name': 'Water',
            'amount': '',
            'frequency': 'monthly',
            'next_due': '2026-10-10',
            'account_id': str(acct),
            'category_id': str(utilities),
        },
    )
    assert wrong.status_code == 400
    assert f'<option value="{acct}" selected>' in wrong.text
    assert f'<option value="{utilities}" selected>' in wrong.text


def test_deleting_an_account_removes_its_transactions(env: Env) -> None:
    client = env.login(env.user('alex'))
    acct = make_account(client)
    add_transactions(client, acct, ANZ_SEPT)
    assert client.post(f'/accounts/{acct}/delete').status_code == 303
    assert count(env, Transaction) == 0
    assert count(env, Account) == 0
    assert count(env, User) == 1


# ------------------------------------------------------- bills that vary (D66)


def utilities_id(env: Env, user: User) -> int:
    with env.db() as db:
        return int(
            db.execute(
                select(Category.id).where(Category.user_id == user.id, Category.name == 'Utilities')
            ).scalar_one()
        )


def bill_amount(env: Env, bill: int) -> int:
    with env.db() as db:
        found = db.get(Bill, bill)
        assert found is not None
        return found.amount_cents


def paid(env: Env, bill: int) -> dict[date, int]:
    with env.db() as db:
        return dict(
            db.execute(
                select(BillPayment.due_date, BillPayment.amount_cents).where(
                    BillPayment.bill_id == bill
                )
            ).all()
        )


def test_a_bill_that_varies_expects_the_average_of_the_last_three_paid(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    acct = make_account(a)
    utilities = utilities_id(env, alex)
    power = create_bill(
        a,
        name='Power',
        amount='300',
        frequency='quarterly',
        next_due='2026-07-01',
        account_id=str(acct),
        category_id=str(utilities),
        varies='on',
    )
    assert 'Varies' in a.get('/bills').text
    sheet = a.get(f'/bills/{power}/occurrence/2026-10-01').text
    assert 'Expected' in sheet and 'name="amount"' in sheet and 'value="300.00"' in sheet

    def pay(due: str, amount: str) -> str:
        resp = a.post(
            f'/bills/{power}/occurrence/{due}/paid',
            data={'amount': amount, 'back': '/bills'},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        return str(resp.text)

    page = pay('2026-07-01', '280')
    assert 'Power marked paid. It now expects $280.00, what was paid last time' in page
    page = pay('2026-10-01', '350.50')
    assert 'It now expects $315.25, the average of the last 2 paid' in page
    assert 'Everyday is now $369.50' in page  # what was paid came off its account
    pay('2027-01-01', '320')
    assert bill_amount(env, power) == 316_83  # (280 + 350.50 + 320) / 3
    page = pay('2027-04-01', '400')
    assert 'the average of the last 3 paid' in page
    assert bill_amount(env, power) == 356_83  # the oldest drops out
    assert paid(env, power) == {
        date(2026, 7, 1): 280_00,
        date(2026, 10, 1): 350_50,
        date(2027, 1, 1): 320_00,
        date(2027, 4, 1): 400_00,
    }
    with env.db() as db:
        moved = db.execute(
            select(Transaction.amount_cents).where(Transaction.bill_payment_id.is_not(None))
        ).scalars()
        assert sorted(moved) == [-400_00, -350_50, -320_00, -280_00]
    # The budget and the forecast expect it: a quarter of 356.83 is 118.94 a month.
    budget = a.get('/budget?month=2026-10').text
    assert '$118.94 a month of bills</a>' in budget
    assert '-$356.83' in a.get('/calendar?view=agenda&date=2027-07-01').text
    shown = a.get(f'/bills/{power}').text
    assert '$356.83, varies' in shown and 'paid 1 Oct 2026, $350.50' in shown
    # Marked unpaid, it goes back to the average without it.
    resp = a.post(f'/bills/{power}/occurrence/2027-04-01/unpaid', follow_redirects=True)
    assert 'It now expects $316.83, the average of the last 3 paid' in resp.text
    # Left blank, it is paid at what it expects; nothing is not a payment.
    bad = a.post(
        f'/bills/{power}/occurrence/2027-04-01/paid',
        data={'amount': '0', 'back': '/bills'},
        follow_redirects=True,
    )
    assert 'Amount paid: enter more than zero' in bad.text
    assert date(2027, 4, 1) not in paid(env, power)
    pay('2027-04-01', '')
    assert paid(env, power)[date(2027, 4, 1)] == 316_83
    env.clock.set(datetime(2027, 6, 28, 2, tzinfo=UTC))
    with env.db() as db:
        user = db.merge(alex)
        due = [r for r in reminders.due_reminders(db, env.ctx.clock, user) if r.kind == 'bill_due']
        assert due[0].message.startswith('Power: about $')


def test_a_bill_that_does_not_vary_is_paid_at_its_amount(env: Env) -> None:
    alex = env.user('alex')
    a = env.login(alex)
    rent = create_bill(a, name='Rent', amount='500', next_due='2026-10-01')
    sheet = a.get(f'/bills/{rent}/occurrence/2026-10-01').text
    assert 'name="amount"' not in sheet and 'Expected' not in sheet
    a.post(f'/bills/{rent}/occurrence/2026-10-01/paid', data={'amount': '20'})
    assert paid(env, rent) == {date(2026, 10, 1): 500_00}
    assert bill_amount(env, rent) == 500_00


def test_turning_varies_on_takes_the_average_of_what_was_paid(env: Env) -> None:
    """Paid before it varied, each payment kept what the bill said then (D57),
    so the average is there at once; after that a typed amount holds until
    the next one is paid."""
    alex = env.user('alex')
    a = env.login(alex)
    fields = {
        'name': 'Water',
        'frequency': 'monthly',
        'next_due': '2026-09-01',
        'category_id': str(utilities_id(env, alex)),
        'active': 'on',
    }
    water = create_bill(a, amount='100', **fields)
    a.post(f'/bills/{water}/occurrence/2026-09-01/paid')
    a.post(f'/bills/{water}', data={**fields, 'amount': '130'})
    a.post(f'/bills/{water}/occurrence/2026-10-01/paid')
    assert 'name="varies" checked' not in a.get(f'/bills/{water}/edit').text
    resp = a.post(
        f'/bills/{water}', data={**fields, 'amount': '130', 'varies': 'on'}, follow_redirects=True
    )
    assert 'Bill saved. It now expects $115.00, the average of the last 2 paid' in resp.text
    assert 'name="varies" checked' in a.get(f'/bills/{water}/edit').text
    # A new price typed in is what it expects until the next is paid.
    a.post(f'/bills/{water}', data={**fields, 'amount': '150', 'varies': 'on'})
    assert bill_amount(env, water) == 150_00
    a.post(f'/bills/{water}/occurrence/2026-11-01/paid', data={'amount': '160'})
    assert bill_amount(env, water) == 130_00  # (100 + 130 + 160) / 3
    # Turned off, it keeps its amount and is paid at it.
    a.post(f'/bills/{water}', data={**fields, 'amount': '130'})
    a.post(f'/bills/{water}/occurrence/2026-12-01/paid', data={'amount': '999'})
    assert paid(env, water)[date(2026, 12, 1)] == 130_00


def test_a_new_average_counts_in_a_saved_up_line_from_this_budget_month(env: Env) -> None:
    """A quarterly bill saves up its line (D63). Paid late at more than it
    expected, the months gone keep what the old amount gave them and the
    payment comes out of them, and the new average counts from now (D64)."""
    alex = env.user('alex')
    a = env.login(alex)
    utilities = utilities_id(env, alex)
    power = create_bill(
        a,
        name='Power',
        amount='300',
        frequency='quarterly',
        next_due='2026-11-01',
        category_id=str(utilities),
        varies='on',
    )
    env.clock.set(datetime(2026, 12, 3, 2, tzinfo=UTC))
    a = env.login(alex)
    a.post(f'/bills/{power}/occurrence/2026-11-01/paid', data={'amount': '360'})
    with env.db() as db:
        row = db.execute(select(Budget).where(Budget.category_id == utilities)).scalar_one()
        # October and November took in 100 each, and November paid 360.
        assert (row.saved_since, row.saved_opening_cents) == (date(2026, 12, 1), -160_00)
        user = db.merge(alex)
        start, end = budgeting.period(db, user, date(2026, 12, 3))
        found = {r.category.id: r for r in budgeting.rows(db, user, start, end)}
        assert found[utilities].saved_cents == -40_00  # December takes in 120
