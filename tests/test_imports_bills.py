"""CSV import end to end, rules, recategorising, bills, shares and matching."""

from __future__ import annotations

import re
from datetime import date

from sqlalchemy import func, select

from fiscus.models import (
    Account,
    Bill,
    BillPayment,
    Category,
    Import,
    ImportMapping,
    Transaction,
    User,
)
from tests.conftest import CsrfClient, Env

ANZ_SEPT = (
    '01/09/2026,"-4.50","EFTPOS COFFEE CO"\n'
    '01/09/2026,"-4.50","EFTPOS COFFEE CO"\n'
    '02/09/2026,"2500.00","PAY/SALARY ACME PTY LTD"\n'
    '15/09/2026,"-121.40","ORIGIN ENERGY BPAY 1234"\n'
)
ANZ_OVERLAP = ANZ_SEPT + '20/09/2026,"-15.99","NETFLIX.COM"\n'


def make_account(
    client: CsrfClient, name: str = 'Everyday', type_: str = 'transaction', opening: str = '1000'
) -> int:
    resp = client.post('/accounts', data={'name': name, 'type': type_, 'opening_balance': opening})
    assert resp.status_code == 303, resp.text
    return int(resp.headers['location'].rsplit('/', 1)[1])


def upload(client: CsrfClient, account_id: int, text: str, name: str = 'anz.csv') -> int:
    resp = client.post(
        '/import',
        data={'account_id': str(account_id)},
        files={'file': (name, text.encode(), 'text/csv')},
    )
    assert resp.status_code == 303, resp.text
    return int(resp.headers['location'].rsplit('/', 1)[1])


def import_csv(client: CsrfClient, account_id: int, text: str) -> Import:
    imp_id = upload(client, account_id, text)
    preview = client.get(f'/import/{imp_id}')
    assert preview.status_code == 200
    assert client.post(f'/import/{imp_id}/commit').status_code == 303
    return imp_id  # type: ignore[return-value]


def count(env: Env, model: type, **where: object) -> int:
    with env.db() as db:
        q = select(func.count()).select_from(model)
        for k, v in where.items():
            q = q.where(getattr(model, k) == v)
        return int(db.scalar(q) or 0)


# ------------------------------------------------------------------- import


def test_anz_import_preview_commit_and_dedup(env: Env) -> None:
    client = env.login(env.user('alex'))
    acct = make_account(client)
    imp_id = upload(client, acct, ANZ_SEPT)
    page = client.get(f'/import/{imp_id}')
    assert 'ANZ format' in page.text
    assert 'Import 4 transactions' in page.text
    # Nothing is written until commit.
    assert count(env, Transaction) == 0
    client.post(f'/import/{imp_id}/commit')
    assert count(env, Transaction) == 4

    # An overlapping export adds only the new row.
    second = upload(client, acct, ANZ_OVERLAP)
    assert 'Import 1 transaction' in client.get(f'/import/{second}').text
    client.post(f'/import/{second}/commit')
    assert count(env, Transaction) == 5
    with env.db() as db:
        imp = db.get(Import, second)
        assert imp is not None
        assert (imp.row_count, imp.imported_count, imp.duplicate_count) == (5, 1, 4)
        assert imp.status == 'committed'
        assert imp.stored_path == ''  # upload file removed after commit
    history = client.get('/import').text
    assert 'anz.csv' in history


def test_undo_import(env: Env) -> None:
    client = env.login(env.user('alex'))
    acct = make_account(client)
    first = upload(client, acct, ANZ_SEPT)
    client.post(f'/import/{first}/commit')
    second = upload(client, acct, ANZ_OVERLAP)
    client.post(f'/import/{second}/commit')
    assert client.post(f'/import/{second}/undo').status_code == 303
    assert count(env, Transaction) == 4
    with env.db() as db:
        assert db.get(Import, second).status == 'undone'  # type: ignore[union-attr]
    # Undoing twice is refused politely.
    assert client.post(f'/import/{second}/undo').status_code == 303
    assert count(env, Transaction) == 4


def test_other_bank_needs_mapping_which_is_then_remembered(env: Env) -> None:
    client = env.login(env.user('alex'))
    acct = make_account(client, 'Credit card', 'credit_card', '0')
    text = 'Date,Description,Debit,Credit\n2026-09-03,Coles,55.20,\n2026-09-10,Payment,,200.00\n'
    imp = upload(client, acct, text, 'card.csv')
    page = client.get(f'/import/{imp}')
    assert 'Map the columns' in page.text
    resp = client.post(
        f'/import/{imp}/mapping',
        data={
            'has_header': 'on',
            'date_col': '0',
            'date_format': 'YYYY-MM-DD',
            'amount_col': '-1',
            'debit_col': '2',
            'credit_col': '3',
            'description_cols': '1',
        },
    )
    assert resp.status_code == 303
    assert 'Preview' in client.get(f'/import/{imp}').text
    client.post(f'/import/{imp}/commit')
    with env.db() as db:
        amounts = sorted(t.amount_cents for t in db.execute(select(Transaction)).scalars())
        assert amounts == [-5520, 20000]
        assert db.execute(select(ImportMapping)).scalar_one().account_id == acct
    # The next file from this bank goes straight to the preview.
    again = upload(client, acct, text + '2026-09-12,Bunnings,12.00,\n', 'card2.csv')
    assert 'saved column mapping' in client.get(f'/import/{again}').text


def test_rules_apply_on_import_and_rerun(env: Env) -> None:
    alex = env.user('alex')
    client = env.login(alex)
    with env.db() as db:
        utilities = (
            db.execute(
                select(Category).where(Category.user_id == alex.id, Category.name == 'Utilities')
            )
            .scalar_one()
            .id
        )
        salary = (
            db.execute(
                select(Category).where(Category.user_id == alex.id, Category.name == 'Salary')
            )
            .scalar_one()
            .id
        )
    client.post(
        '/rules',
        data={'pattern': 'origin energy', 'category_id': str(utilities), 'priority': '100'},
    )
    acct = make_account(client)
    import_csv(client, acct, ANZ_SEPT)
    with env.db() as db:
        origin = db.execute(
            select(Transaction).where(Transaction.description.like('ORIGIN%'))
        ).scalar_one()
        assert origin.category_id == utilities
    client.post(
        '/rules', data={'pattern': r'^PAY/SALARY', 'is_regex': 'true', 'category_id': str(salary)}
    )
    assert client.post('/rules/rerun').status_code == 303
    with env.db() as db:
        pay = db.execute(
            select(Transaction).where(Transaction.amount_cents == 2500_00)
        ).scalar_one()
        assert pay.category_id == salary


def test_recategorise_single_and_bulk(env: Env) -> None:
    alex = env.user('alex')
    client = env.login(alex)
    acct = make_account(client)
    import_csv(client, acct, ANZ_SEPT)
    page = client.get('/recategorise')
    assert page.status_code == 200 and 'COFFEE' in page.text
    with env.db() as db:
        dining = (
            db.execute(
                select(Category).where(Category.user_id == alex.id, Category.name == 'Dining out')
            )
            .scalar_one()
            .id
        )
        coffee = [
            t.id
            for t in db.execute(
                select(Transaction).where(Transaction.description.like('%COFFEE%'))
            ).scalars()
        ]
    resp = client.post(
        f'/transactions/{coffee[0]}/category',
        data={'category_id': str(dining)},
        headers={'HX-Request': 'true'},
    )
    assert resp.status_code == 200 and 'Dining out' in resp.text
    resp = client.post(
        '/recategorise/bulk', data={'category_id': str(dining), 'txn': [str(coffee[1])]}
    )
    assert resp.status_code == 303
    assert count(env, Transaction, category_id=dining) == 2


def test_csv_export_guards_formulas(env: Env) -> None:
    client = env.login(env.user('alex'))
    acct = make_account(client)
    import_csv(client, acct, '01/09/2026,"-1.00","=HYPERLINK(""http://x"")"\n')
    resp = client.get('/transactions/export.csv')
    assert resp.status_code == 200
    assert "'=HYPERLINK" in resp.text
    assert resp.headers['cache-control'] == 'no-store'


def test_oversized_and_binary_uploads_are_refused(env: Env) -> None:
    client = env.login(env.user('alex'))
    acct = make_account(client)
    resp = client.post(
        '/import',
        data={'account_id': str(acct)},
        files={'file': ('x.csv', b'\xff\xfe\x00\x00' * 10, 'text/csv')},
    )
    assert resp.status_code == 303 and '/import?' in resp.headers['location']
    assert count(env, Import) == 0


# --------------------------------------------------------------------- bills


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


def test_bill_auto_matches_imported_payment(env: Env) -> None:
    client = env.login(env.user('alex'))
    acct = make_account(client)
    bill = create_bill(client, account_id=str(acct), description_hint='origin')
    import_csv(client, acct, ANZ_SEPT)
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


def test_manual_match_and_unmatch(env: Env) -> None:
    client = env.login(env.user('alex'))
    acct = make_account(client)
    import_csv(client, acct, ANZ_SEPT)
    bill = create_bill(client, name='Gas', amount='300', next_due='2026-09-15')
    with env.db() as db:
        txn = db.execute(
            select(Transaction).where(Transaction.description.like('ORIGIN%'))
        ).scalar_one()
    sheet = client.get(f'/bills/{bill}/occurrence/2026-09-15')
    assert 'ORIGIN ENERGY' in sheet.text  # offered as a candidate
    client.post(f'/bills/{bill}/occurrence/2026-09-15/paid', data={'transaction_id': str(txn.id)})
    with env.db() as db:
        assert db.execute(select(BillPayment)).scalar_one().transaction_id == txn.id
    client.post(f'/bills/{bill}/occurrence/2026-09-15/unpaid')
    assert count(env, BillPayment) == 0


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
    import_csv(s, sam_acct, '11/09/2026,"-100.00","TRANSFER TO ALEX WATER"\n')
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
    import_csv(client, acct, ANZ_SEPT)
    assert client.post(f'/accounts/{acct}/delete').status_code == 303
    assert count(env, Transaction) == 0
    assert count(env, Account) == 0
    assert count(env, User) == 1
