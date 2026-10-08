"""Bills, shares and payment matching."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import date, datetime

from sqlalchemy import func, select

from fiscus.clock import utcnow_naive
from fiscus.models import Account, Bill, BillPayment, Transaction, User
from fiscus.money import parse_cents
from fiscus.services import bills, ledger
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
    """Transactions in an account, as a Square sync (or an import before they
    were removed) leaves them: one per line of date (DD/MM/YYYY), amount and
    description. Rules and bill matching run on them, as a sync does."""
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
        db.flush()
        ledger.apply_rules(db, user, created)
        bills.auto_match(db, ctx.clock, user, created)
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
        'split_mode': 'equal',
    }
    data.update(fields)
    resp = client.post('/bills', data=data)
    assert resp.status_code == 303, resp.text
    return int(resp.headers['location'].rsplit('/', 1)[1])


def test_bill_auto_matches_a_synced_payment(env: Env) -> None:
    client = env.login(env.user('alex'))
    acct = make_account(client)
    bill = create_bill(client, account_id=str(acct))
    with env.db() as db:
        b = db.get(Bill, bill)
        assert b is not None
        b.description_hint = 'origin'
        db.commit()
    add_transactions(client, acct, ANZ_SEPT)
    with env.db() as db:
        pay = db.execute(select(BillPayment)).scalar_one()
        assert pay.bill_id == bill and pay.due_date == date(2026, 9, 15) and pay.transaction_id
    page = client.get(f'/bills/{bill}')
    assert 'matched' in page.text


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


def test_shared_bill_each_person_sees_their_share(env: Env) -> None:
    alex = env.user('alex')
    sam = env.user('sam')
    a = env.login(alex)
    s = env.login(sam)
    bill = create_bill(
        a,
        name='Internet',
        amount='90',
        next_due='2026-10-10',
        split_mode='percent',
        participants=[str(sam.id)],
        **{f'split_{alex.id}': '60', f'split_{sam.id}': '40'},
    )
    alex_page = a.get('/bills').text
    sam_page = s.get('/bills').text
    assert '$54.00' in alex_page and '$36.00' in sam_page
    assert 'Shared by Alex' in sam_page
    # Sam sees it on the calendar at Sam's share.
    cal = s.get('/calendar?view=agenda&date=2026-10-01').text
    assert 'Internet' in cal and '-$36.00' in cal
    # Sam cannot edit it, but can pay their own share.
    assert s.get(f'/bills/{bill}/edit').status_code == 404
    assert (
        s.post(
            f'/bills/{bill}',
            data={'name': 'x', 'amount': '1', 'frequency': 'monthly', 'next_due': '2026-10-10'},
        ).status_code
        == 404
    )
    assert s.post(f'/bills/{bill}/occurrence/2026-10-10/paid').status_code == 303
    with env.db() as db:
        pays = db.execute(select(BillPayment)).scalars().all()
        assert [(p.user_id, p.due_date) for p in pays] == [(sam.id, date(2026, 10, 10))]
    # Alex's occurrence is still unpaid.
    assert 'Paid' not in a.get(f'/bills/{bill}/occurrence/2026-10-10').text.split('Your share')[0]


def test_split_must_add_up(env: Env) -> None:
    alex = env.user('alex')
    sam = env.user('sam')
    a = env.login(alex)
    resp = a.post(
        '/bills',
        data={
            'name': 'x',
            'amount': '100',
            'frequency': 'monthly',
            'next_due': '2026-10-01',
            'split_mode': 'percent',
            'participants': [str(sam.id)],
            f'split_{alex.id}': '50',
            f'split_{sam.id}': '40',
        },
    )
    assert resp.status_code == 400 and 'add up to 100' in resp.text
    assert count(env, Bill) == 0


def test_participant_match_uses_their_share(env: Env) -> None:
    alex = env.user('alex')
    sam = env.user('sam')
    a, s = env.login(alex), env.login(sam)
    create_bill(a, name='Water', amount='200', next_due='2026-09-10', participants=[str(sam.id)])
    sam_acct = make_account(s, 'Sam everyday')
    # Sam pays Alex $100 for their half.
    add_transactions(s, sam_acct, '11/09/2026,"-100.00","TRANSFER TO ALEX WATER"\n')
    with env.db() as db:
        pay = db.execute(select(BillPayment)).scalar_one()
        assert pay.user_id == sam.id


def test_bill_page_lists_who_pays(env: Env) -> None:
    alex = env.user('alex')
    sam = env.user('sam')
    a = env.login(alex)
    bill = create_bill(a, amount='100.01', participants=[str(sam.id)])
    page = a.get(f'/bills/{bill}').text
    assert re.search(r'Alex.*\$50\.01', page, re.S) and re.search(r'Sam.*\$50\.00', page, re.S)


def test_deleting_an_account_removes_its_transactions(env: Env) -> None:
    client = env.login(env.user('alex'))
    acct = make_account(client)
    add_transactions(client, acct, ANZ_SEPT)
    assert client.post(f'/accounts/{acct}/delete').status_code == 303
    assert count(env, Transaction) == 0
    assert count(env, Account) == 0
    assert count(env, User) == 1
