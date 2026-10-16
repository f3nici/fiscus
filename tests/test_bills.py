"""Bills: their pages, states and payments."""

from __future__ import annotations

import csv
import hashlib
import io
from datetime import datetime

from sqlalchemy import func, select

from fiscus.clock import utcnow_naive
from fiscus.models import Account, Category, Transaction, User
from fiscus.money import parse_cents
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
