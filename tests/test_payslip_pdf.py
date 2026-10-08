"""Payslip PDFs: reading the figures, then filling in the payslip form."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from fiscus.domain import payslip
from fiscus.domain.uploads import ImportError_
from fiscus.models import Payslip
from tests.conftest import Env
from tests.pdfgen import Text, build, right


def payslip_pdf() -> bytes:
    """Laid out like a common payroll app's payslip: summary line, THIS PAY then YTD."""
    texts: list[Text] = [
        (427, 804, 'PAID BY'),
        (427, 790, 'Example Care Services Pty Ltd'),
        (427, 774, '1 Example Street'),
        (427, 744, 'ABN 11 111 111 111'),
        (427, 681, 'Pay Frequency: Weekly'),
        (104, 664, 'Sam Example'),
        (28, 579, 'Pay Period: 19/09/2026 - 25/09/2026'),
        (192, 579, 'Payment Date: 30/09/2026'),
        (359, 579, 'Total Earnings: $803.85'),
        (494, 579, 'Net Pay: $618.85'),
        (448, 544, 'THIS PAY'),
        (552, 544, 'YTD'),
        (26, 513, 'SALARY & WAGES'),
        (385, 513, 'RATE'),
        (26, 498, 'Casual Loading'),
        right(330, 498, '13.2500'),
        right(410, 498, '$9.0575'),
        right(490, 498, '$120.01'),
        right(575, 498, '$867.20'),
        (26, 468, 'Saturday'),
        right(330, 468, '7.2500'),
        right(410, 468, '$54.3450'),
        right(490, 468, '$394.00'),
        right(575, 468, '$1,643.94'),
        (382, 412, 'TOTAL'),
        right(490, 412, '$803.85'),
        right(575, 412, '$6,207.82'),
        (26, 378, 'TAX'),
        (26, 363, 'PAYG'),
        right(490, 363, '$185.00'),
        right(575, 363, '$1,381.00'),
        (382, 338, 'TOTAL'),
        right(490, 338, '$185.00'),
        right(575, 338, '$1,381.00'),
        (26, 304, 'SUPERANNUATION'),
        (26, 288, 'SGC - Example Super'),
        right(490, 288, '$96.46'),
        (26, 229, 'PAYMENT DETAILS'),
        (360, 229, 'REFERENCE'),
        (535, 229, 'AMOUNT'),
        (26, 213, '(000-000)*****0000'),
        (389, 213, 'Pay'),
        right(575, 213, '$618.85'),
    ]
    return build([texts])


def test_reads_the_figures_for_this_pay_not_the_year_to_date() -> None:
    slip = payslip.read(payslip_pdf())
    assert slip.pay_date == date(2026, 9, 30)
    assert slip.period_end == date(2026, 9, 25)
    assert slip.employer == 'Example Care Services Pty Ltd'
    assert slip.gross_cents == 803_85
    assert slip.tax_withheld_cents == 185_00
    assert slip.net_cents == 618_85
    assert slip.notes() == []


def test_boxed_layout_with_values_below_and_no_gross_label() -> None:
    pdf = build(
        [
            [
                (40, 800, 'Employer: Acme Widgets Pty Ltd'),
                (40, 760, 'Pay date'),
                (200, 760, 'Period ending'),
                (40, 745, '14 Oct 2026'),
                (200, 745, '12 Oct 2026'),
                (40, 700, 'Earnings'),
                (40, 685, 'Ordinary hours'),
                right(400, 685, '2,000.00'),
                (40, 670, 'Total'),
                right(400, 670, '2,000.00'),
                (40, 640, 'Tax withheld'),
                right(400, 640, '400.00'),
                (40, 620, 'Salary sacrifice'),
                right(400, 620, '100.00'),
                (40, 600, 'Net pay'),
                right(400, 600, '1,500.00'),
            ]
        ]
    )
    slip = payslip.read(pdf)
    assert slip.employer == 'Acme Widgets Pty Ltd'
    assert slip.pay_date == date(2026, 10, 14)
    assert slip.gross_cents == 2000_00
    assert slip.tax_withheld_cents == 400_00
    assert slip.net_cents == 1500_00
    assert any('something else was taken out' in n for n in slip.notes())


def test_missing_figures_are_pointed_out() -> None:
    slip = payslip.read(build([[(40, 800, 'Week ending 03/10/2026'), (40, 780, 'Net: $500.00')]]))
    assert slip.net_cents == 500_00
    assert slip.pay_date is None
    assert slip.period_end == date(2026, 10, 3)
    notes = slip.notes()
    assert 'Could not find the pay date, gross pay or tax withheld.' in notes[0]
    assert 'end of the pay period' in notes[1]


def test_not_a_payslip_is_refused() -> None:
    with pytest.raises(ImportError_, match='not a PDF'):
        payslip.read(b'date,amount\n')
    with pytest.raises(ImportError_, match='no text'):
        payslip.read(build([[]]))
    with pytest.raises(ImportError_, match='does not look like a payslip'):
        payslip.read(build([[(40, 800, 'Electricity bill'), (40, 780, 'Due 01/11/2026')]]))
    with pytest.raises(ImportError_, match='could not be read'):
        payslip.read(b'%PDF-1.4\nnot really a pdf')


def test_upload_fills_the_form_and_saves_nothing(env: Env) -> None:
    client = env.login(env.user('alice'))
    resp = client.post(
        '/payslips/read', files={'file': ('slip.pdf', payslip_pdf(), 'application/pdf')}
    )
    assert resp.status_code == 200, resp.text
    page = resp.text
    assert 'Read from slip.pdf' in page
    assert 'value="2026-09-30"' in page
    assert 'value="Example Care Services Pty Ltd"' in page
    assert 'value="803.85"' in page
    assert 'value="185.00"' in page
    assert 'value="618.85"' in page
    with env.db() as db:
        assert db.execute(select(Payslip)).first() is None

    form = {
        'date': '2026-09-30',
        'source': 'Example Care Services Pty Ltd',
        'gross': '803.85',
        'tax_withheld': '185.00',
        'net': '618.85',
    }
    assert client.post('/payslips', data=form).status_code == 303
    again = client.post(
        '/payslips/read', files={'file': ('slip.pdf', payslip_pdf(), 'application/pdf')}
    )
    assert 'already have a payslip for this date' in again.text


def test_unreadable_upload_says_why(env: Env) -> None:
    client = env.login(env.user('alice'))
    resp = client.post('/payslips/read', files={'file': ('slip.csv', b'a,b\n', 'text/csv')})
    assert resp.status_code == 303
    assert 'not%20a%20PDF' in resp.headers['set-cookie']
