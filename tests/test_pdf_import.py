"""PDF statements: reading the table, then the same import as a CSV."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from fiscus.domain import pdfstatement as ps
from fiscus.domain.csvimport import ImportError_
from fiscus.models import Account, ImportMapping, Transaction
from tests.conftest import CsrfClient, Env
from tests.pdfgen import Text, build, right
from tests.test_imports_bills import make_account

TODAY = date(2026, 10, 1)


def _head(y: float) -> list[Text]:
    return [
        (40, y, 'Date'),
        (110, y, 'Transaction details'),
        right(400, y, 'Withdrawals ($)'),
        right(480, y, 'Deposits ($)'),
        right(560, y, 'Balance ($)'),
    ]


def statement() -> bytes:
    """Two pages, yearless dates, a wrapped description, opening and closing lines."""
    one: list[Text] = [
        (40, 800, 'EVERYDAY ACCOUNT STATEMENT'),
        (40, 785, 'Statement period 01/09/2026 to 30/09/2026'),
        *_head(750),
        (40, 735, '01 SEP'),
        (110, 735, 'OPENING BALANCE'),
        right(560, 735, '1,000.00'),
        (40, 720, '01 SEP'),
        (110, 720, 'EFTPOS COFFEE CO'),
        right(400, 720, '4.50'),
        right(560, 720, '995.50'),
        (110, 709, 'PERTH WA'),
        (40, 694, '01 SEP'),
        (110, 694, 'EFTPOS COFFEE CO'),
        right(400, 694, '4.50'),
        right(560, 694, '991.00'),
        (40, 679, '02 SEP'),
        (110, 679, 'PAY/SALARY ACME PTY LTD'),
        right(480, 679, '2,500.00'),
        right(560, 679, '3,491.00'),
        (40, 100, 'Page 1 of 2'),
    ]
    two: list[Text] = [
        *_head(750),
        (40, 735, '15 SEP'),
        (110, 735, 'ORIGIN ENERGY BPAY 1234'),
        right(400, 735, '121.40'),
        right(560, 735, '3,369.60'),
        (40, 720, '30 SEP'),
        (110, 720, 'CLOSING BALANCE'),
        right(560, 720, '3,369.60'),
        (40, 100, 'Page 2 of 2'),
    ]
    return build([one, two])


def test_reads_debit_credit_columns_by_position() -> None:
    table = ps.to_table(statement(), TODAY)
    assert table.rows == [
        ['Date', 'Description', 'Debit', 'Credit', 'Balance'],
        ['2026-09-01', 'EFTPOS COFFEE CO PERTH WA', '4.50', '', '995.50'],
        ['2026-09-01', 'EFTPOS COFFEE CO', '4.50', '', '991.00'],
        ['2026-09-02', 'PAY/SALARY ACME PTY LTD', '', '2500.00', '3491.00'],
        ['2026-09-15', 'ORIGIN ENERGY BPAY 1234', '121.40', '', '3369.60'],
    ]
    assert table.mapping == ps.pdf_mapping(amount_col=None, debit_col=2, credit_col=3)


def test_yearless_dates_across_new_year() -> None:
    pdf = build(
        [
            [
                (40, 800, 'Statement period 15 Dec 2026 to 14 Jan 2027'),
                *_head(750),
                (40, 735, '20 DEC'),
                (110, 735, 'GIFT SHOP'),
                right(400, 735, '80.00'),
                right(560, 735, '920.00'),
                (40, 720, '03 JAN'),
                (110, 720, 'REFUND'),
                right(480, 720, '20.00'),
                right(560, 720, '940.00'),
            ]
        ]
    )
    rows = ps.to_table(pdf, date(2027, 1, 20)).rows
    assert [r[0] for r in rows[1:]] == ['2026-12-20', '2027-01-03']


def test_credit_card_cr_marks_credits() -> None:
    pdf = build(
        [
            [
                (40, 800, 'Closing date 30/09/2026'),
                (40, 750, 'Date'),
                (110, 750, 'Description'),
                right(560, 750, 'Amount'),
                (40, 735, '03/09/26'),
                (110, 735, 'WOOLWORTHS 1234'),
                right(560, 735, '82.10'),
                (40, 720, '10/09/26'),
                (110, 720, 'PAYMENT THANK YOU'),
                right(560, 720, '500.00 CR'),
            ]
        ]
    )
    table = ps.to_table(pdf, TODAY)
    assert table.rows[1:] == [
        ['2026-09-03', 'WOOLWORTHS 1234', '-82.10'],
        ['2026-09-10', 'PAYMENT THANK YOU', '500.00'],
    ]
    assert table.mapping == ps.pdf_mapping(amount_col=2)


def test_no_header_needs_a_mapping() -> None:
    pdf = build(
        [
            [
                (40, 735, '2026-09-03'),
                (110, 735, 'WOOLWORTHS'),
                right(400, 735, '82.10'),
                right(560, 735, '917.90'),
            ]
        ]
    )
    table = ps.to_table(pdf, TODAY)
    assert table.rows[0] == ['Date', 'Description', 'Amount 1', 'Amount 2']
    assert table.mapping is None


@pytest.mark.parametrize(
    ('text', 'want'),
    [
        ('1,234.56', '1234.56'),
        ('$12.00', '12.00'),
        ('-4.50', '-4.50'),
        ('4.50-', '-4.50'),
        ('(4.50)', '-4.50'),
        ('4.50 DR', '-4.50'),
        ('4.50 CR', '+4.50'),
        ('1234', None),
        ('4.5', None),
    ],
)
def test_amounts(text: str, want: str | None) -> None:
    assert ps._amount(text) == want


def test_text_only_and_damaged_pdfs_are_refused() -> None:
    with pytest.raises(ImportError_, match='No transactions'):
        ps.to_table(build([[(40, 800, 'Nothing to see here')]]), TODAY)
    with pytest.raises(ImportError_, match='could not be read'):
        ps.to_table(b'%PDF-1.4\nnot really a pdf', TODAY)


# ------------------------------------------------------------------ end to end


def _upload(client: CsrfClient, account: int, data: bytes) -> int:
    resp = client.post(
        '/import',
        data={'account_id': str(account)},
        files={'file': ('statement.pdf', data, 'application/pdf')},
    )
    assert resp.status_code == 303, resp.text
    return int(resp.headers['location'].rsplit('/', 1)[1])


def test_pdf_import_end_to_end(env: Env) -> None:
    client = env.login(env.user('alice'))
    acct = make_account(client)
    imp = _upload(client, acct, statement())
    preview = client.get(f'/import/{imp}')
    assert preview.status_code == 200
    assert 'PDF statement' in preview.text
    assert 'EFTPOS COFFEE CO PERTH WA' in preview.text
    assert client.post(f'/import/{imp}/commit').status_code == 303
    with env.db() as db:
        amounts = sorted(
            db.execute(select(Transaction.amount_cents).where(Transaction.account_id == acct))
            .scalars()
            .all()
        )
    assert amounts == [-12140, -450, -450, 250000]

    # The same statement again is all duplicates.
    again = _upload(client, acct, statement())
    assert '>0<' in client.get(f'/import/{again}').text.replace(' ', '')
    client.post(f'/import/{again}/commit')
    with env.db() as db:
        assert len(db.execute(select(Transaction.id)).all()) == 4


def test_pdf_mapping_is_kept_apart_from_the_csv_mapping(env: Env) -> None:
    client = env.login(env.user('alice'))
    acct = make_account(client)
    csv_map = {
        'has_header': True,
        'date_col': 0,
        'date_format': 'DD/MM/YYYY',
        'amount_col': 2,
        'debit_col': None,
        'credit_col': None,
        'description_cols': [1],
        'invert': False,
    }
    with env.db() as db:
        owner = db.get(Account, acct)
        assert owner is not None
        db.add(ImportMapping(user_id=owner.user_id, account_id=acct, mapping=dict(csv_map)))
        db.commit()

    pdf = build(
        [
            [
                (40, 735, '2026-09-03'),
                (110, 735, 'WOOLWORTHS'),
                right(400, 735, '82.10'),
                right(560, 735, '917.90'),
            ]
        ]
    )
    imp = _upload(client, acct, pdf)
    page = client.get(f'/import/{imp}')
    assert 'could not tell which amounts are which' in page.text
    assert 'Amount 1' in page.text
    resp = client.post(
        f'/import/{imp}/mapping',
        data={
            'has_header': 'on',
            'date_col': '0',
            'date_format': 'YYYY-MM-DD',
            'debit_col': '2',
            'amount_col': '-1',
            'credit_col': '-1',
            'description_cols': '1',
        },
    )
    assert resp.status_code == 303
    with env.db() as db:
        saved = db.execute(select(ImportMapping)).scalar_one().mapping
    assert {k: v for k, v in saved.items() if k != 'pdf'} == csv_map
    assert saved['pdf']['debit_col'] == 2
    assert 'WOOLWORTHS' in client.get(f'/import/{imp}').text


def test_import_is_a_tab_under_transactions(env: Env) -> None:
    client = env.login(env.user('alice'))
    for path in ('/transactions', '/import'):
        page = client.get(path).text
        assert '<nav class="tabs" aria-label="Transactions">' in page
        assert 'aria-current="page">' + ('Import' if path == '/import' else 'Transactions') in page
    assert '<a href="/import" class="' not in client.get('/transactions').text
