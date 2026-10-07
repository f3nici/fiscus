"""Bad input gets an answer, not a crash; redirects stay on this site."""

from __future__ import annotations

import socket
from typing import Any

import httpx
import pytest

from fiscus.forms import FormError, int_or, pct_bp, rate
from fiscus.models import UserSettings
from fiscus.money import MoneyError, parse_cents
from fiscus.services import ntfy
from fiscus.web import local_path
from tests.conftest import ORIGIN, CsrfClient, Env
from tests.test_imports_bills import create_bill, make_account


def test_money_refuses_exponents() -> None:
    with pytest.raises(MoneyError):
        parse_cents('1e3')
    with pytest.raises(MoneyError):
        parse_cents('2E-2')
    assert parse_cents('$1,000.00') == 100000


@pytest.mark.parametrize('value', ['NaN', 'sNaN', 'Infinity', '-inf', 'abc', ''])
def test_rates_and_percentages_refuse_non_numbers(value: str) -> None:
    with pytest.raises(FormError):
        rate(value, 'Rate')
    with pytest.raises(FormError):
        pct_bp(value, 'Split')


def test_percentages_are_exact() -> None:
    assert pct_bp('12.345', 'Split') == 1235
    assert pct_bp('33.33%', 'Split') == 3333
    with pytest.raises(FormError):
        pct_bp('101', 'Split')


def test_int_or() -> None:
    assert int_or('12') == 12
    assert int_or(' 7 ') == 7
    assert int_or('abc') == 0
    assert int_or('²', 5) == 5
    assert int_or(None, 3) == 3


@pytest.mark.parametrize(
    ('target', 'expected'),
    [
        ('/budget?month=2026-10', '/budget?month=2026-10'),
        ('//example.com/x', '/fallback'),
        ('/\\example.com', '/fallback'),
        ('https://example.com', '/fallback'),
        (None, '/fallback'),
    ],
)
def test_local_path(target: str | None, expected: str) -> None:
    assert local_path(target, '/fallback') == expected


def test_odd_urls_are_not_found_or_ignored(env: Env) -> None:
    client = env.login(env.user('alex'))
    acct = make_account(client)
    bill = create_bill(client, account_id=str(acct))
    assert client.get(f'/bills/{bill}/occurrence/not-a-date').status_code == 404
    assert client.get('/pay?slip=abc').status_code == 200
    assert client.get('/pay?edit=abc').status_code == 200


def test_mark_paid_never_redirects_off_site(env: Env) -> None:
    client = env.login(env.user('alex'))
    acct = make_account(client)
    bill = create_bill(client, account_id=str(acct), next_due='2026-10-15')
    resp = client.post(f'/bills/{bill}/occurrence/2026-10-15/paid', data={'back': '//example.com'})
    assert resp.headers['location'] == f'/bills/{bill}'
    resp = client.post(f'/bills/{bill}/occurrence/2026-10-15/unpaid', data={'back': '/calendar'})
    assert resp.headers['location'] == '/calendar'


def test_bad_form_values_are_refused_politely(env: Env) -> None:
    alex = env.user('alex')
    sam = env.user('sam')
    client = env.login(alex)
    acct = make_account(client)
    savings = make_account(client, name='Savings', type_='savings')
    # A split of infinity percent.
    resp = client.post(
        '/bills',
        data={
            'name': 'Power',
            'amount': '100',
            'frequency': 'monthly',
            'next_due': '2026-11-01',
            'split_mode': 'percent',
            'participants': str(sam.id),
            f'split_{alex.id}': 'inf',
            f'split_{sam.id}': '0',
        },
    )
    assert resp.status_code == 400
    # A loan rate that is not a number.
    loan = client.post(
        '/loans',
        data={
            'name': 'Home',
            'principal': '500000',
            'start_date': '2024-01-01',
            'initial_rate': '6',
            'balance': '480000',
            'balance_date': '2026-10-01',
        },
    )
    loan_id = int(loan.headers['location'].rsplit('/', 1)[1])
    resp = client.post(
        f'/loans/{loan_id}/rates', data={'effective_date': '2026-11-01', 'new_rate': 'NaN'}
    )
    assert resp.status_code in (303, 400)
    # A scenario naming an account that is not a number.
    sc = client.post('/scenarios', data={'name': 'What if'})
    sc_id = int(sc.headers['location'].rsplit('/', 1)[1])
    resp = client.post(
        f'/scenarios/{sc_id}/adjustments',
        data={'kind': 'one_off', 'account_id': 'x', 'amount': '1', 'date': '2026-12-01'},
    )
    assert resp.status_code == 404
    resp = client.post(
        f'/scenarios/{sc_id}/adjustments',
        data={
            'kind': 'rate_change',
            'loan_id': str(loan_id),
            'from_date': '2026-12-01',
            'rate': 'sNaN',
        },
    )
    assert resp.status_code == 303 and 'Change%20added' not in resp.headers.get('set-cookie', '')
    # End dates before the start.
    resp = client.post(
        '/pay/schedules',
        data={
            'name': 'Job',
            'frequency': 'weekly',
            'anchor_date': '2026-10-01',
            'net': '100',
            'end_date': '2026-01-01',
        },
    )
    assert 'before' in resp.headers.get('set-cookie', '')
    resp = client.post(
        '/transfers',
        data={
            'name': 'T',
            'from_account_id': str(acct),
            'to_account_id': str(savings),
            'amount': '1',
            'frequency': 'weekly',
            'anchor_date': '2026-10-10',
            'end_date': '2026-01-01',
        },
    )
    assert 'before' in resp.headers.get('set-cookie', '')


def test_bulk_payslip_review_survives_a_bad_date(env: Env) -> None:
    client = env.login(env.user('alex'))
    resp = client.post(
        '/payslips/bulk',
        data={
            'date': '2026-13-40',
            'source': 'Acme',
            'gross': '1',
            'tax_withheld': '0',
            'net': '1',
            'payday': '',
            'file_name': 'a.pdf',
            'include': '0',
        },
    )
    assert resp.status_code == 200
    assert 'enter a date' in resp.text


def test_a_crash_shows_a_page_not_a_dropped_connection(env: Env) -> None:
    def boom() -> None:
        raise RuntimeError('boom')

    env.app.add_api_route('/boom', boom)
    client = CsrfClient(env.app, base_url=ORIGIN, raise_server_exceptions=False)
    resp = client.get('/boom')
    assert resp.status_code == 500
    assert 'Something went wrong' in resp.text
    assert resp.headers['cache-control'] == 'no-store'
    assert "script-src 'self'" in resp.headers['content-security-policy']


def test_ntfy_goes_to_the_address_it_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    def resolve(host: str, *_a: Any, **_k: Any) -> list[Any]:
        calls.append(host)
        # The second lookup would rebind to loopback; it must never happen.
        ip = '192.168.1.20' if len(calls) == 1 else '127.0.0.1'
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))]

    sent: list[dict[str, Any]] = []

    def fake_post(_client: httpx.Client, url: str, **kw: Any) -> httpx.Response:
        sent.append({'url': url, **kw})
        return httpx.Response(200)

    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    monkeypatch.setattr(httpx.Client, 'post', fake_post)
    prefs = UserSettings(ntfy_url='https://ntfy.home.lan', ntfy_topic='fiscus', ntfy_token='')
    ntfy.send(prefs, 'Title', 'Body')
    assert calls == ['ntfy.home.lan']
    assert sent[0]['url'] == 'https://192.168.1.20/fiscus'
    assert sent[0]['headers']['Host'] == 'ntfy.home.lan'
    assert sent[0]['extensions'] == {'sni_hostname': 'ntfy.home.lan'}
