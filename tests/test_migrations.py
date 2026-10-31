"""Upgrading a database that holds data, for the migrations that move it."""

from __future__ import annotations

import importlib.util
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, inspect, text

from fiscus.db import MIGRATIONS_DIR, alembic_config
from fiscus.domain import recurrence


def test_housing_items_become_plain_bills_and_budgets_keep_their_totals(tmp_path: Path) -> None:
    url = f'sqlite:///{tmp_path / "fiscus.db"}'
    cfg = alembic_config(url)
    command.upgrade(cfg, '0002')
    engine = create_engine(url)
    with engine.begin() as c:
        for uid, name in ((1, 'alex'), (2, 'sam')):
            c.execute(
                text(
                    'INSERT INTO users (id, username, display_name, is_admin, is_active,'
                    ' user_handle, password_enabled, data_version, created_at)'
                    " VALUES (:id, :n, :n, 0, 1, :h, 0, 0, '2026-01-01')"
                ),
                {'id': uid, 'n': name, 'h': bytes([uid]) * 64},
            )
        cats = {'Housing': 10, 'Utilities': 11, 'Groceries': 12, 'Insurance': 13}
        for name, cid in cats.items():
            c.execute(
                text(
                    'INSERT INTO categories (id, user_id, name, is_business, is_deductible,'
                    ' is_income, is_transfer) VALUES (:id, 1, :n, 0, 0, 0, 0)'
                ),
                {'id': cid, 'n': name},
            )
        # Council rates: a housing item, 520 a quarter split half with sam.
        c.execute(
            text(
                'INSERT INTO housing_items (id, user_id, name, amount_cents, frequency,'
                ' category_id, due_date, shared_with_id, shared_split_bp, account_id)'
                " VALUES (1, 1, 'Council rates', 52000, 'quarterly', 10, '2026-11-15', 2,"
                ' 5000, NULL)'
            )
        )
        bills = [
            # id, name, amount, frequency, split, housing item
            (1, 'Council rates', 52000, 'quarterly', 'percent', 1),
            (2, 'Power', 20000, 'monthly', 'equal', None),
        ]
        for bid, name, amount, freq, mode, item in bills:
            c.execute(
                text(
                    'INSERT INTO bills (id, owner_id, name, amount_cents, frequency, next_due,'
                    ' split_mode, match_tolerance_pct, match_window_days, description_hint,'
                    ' active, housing_item_id, created_at) VALUES (:id, 1, :n, :a, :f,'
                    " '2026-11-15', :m, 5, 3, '', 1, :item, '2026-01-01')"
                ),
                {'id': bid, 'n': name, 'a': amount, 'f': freq, 'm': mode, 'item': item},
            )
        shares = [(1, 1, 5000, 10), (1, 2, 5000, None), (2, 1, 0, 11)]
        for bid, uid, value, cat in shares:
            c.execute(
                text(
                    'INSERT INTO bill_shares (bill_id, user_id, split_value, category_id)'
                    ' VALUES (:b, :u, :v, :c)'
                ),
                {'b': bid, 'u': uid, 'v': value, 'c': cat},
            )
        budgets = [
            # The housing budget was kept equal to the item: 86.67 a month.
            (10, 8667, 0),
            # Set by hand above the power bill.
            (11, 25000, 1),
            # Nothing to do with bills.
            (12, 80000, 0),
            # Set by hand, but below its bills (none here) and not in the forecast.
            (13, 0, 0),
        ]
        for cid, cents, fc in budgets:
            c.execute(
                text(
                    'INSERT INTO budgets (user_id, category_id, monthly_cents, use_in_forecast)'
                    ' VALUES (1, :c, :m, :f)'
                ),
                {'c': cid, 'm': cents, 'f': fc},
            )

    command.upgrade(cfg, '0003')

    insp = inspect(engine)
    assert 'housing_items' not in insp.get_table_names()
    assert 'housing_item_id' not in {col['name'] for col in insp.get_columns('bills')}
    with engine.connect() as c:
        rows = c.execute(text('SELECT id, name, amount_cents, active FROM bills ORDER BY id'))
        assert [tuple(r) for r in rows] == [
            (1, 'Council rates', 52000, 1),
            (2, 'Power', 20000, 1),
        ]
        assert c.execute(text('SELECT count(*) FROM bill_shares')).scalar_one() == 3
        budget_rows = c.execute(text('SELECT category_id, monthly_cents FROM budgets'))
        left = {r[0]: r[1] for r in budget_rows}
    # Housing: all bills now, nothing on top. Utilities: 250 less the 200 power
    # bill. Groceries: unchanged. Insurance: zero and not in the forecast, gone.
    assert left == {11: 5000, 12: 80000}

    # 0009: a budget is the whole allocation, bills paid out of it, so each
    # gets its bills back and housing, budgeted only by its bill, gets one.
    command.upgrade(cfg, '0030')
    with engine.connect() as c:
        budget_rows = c.execute(text('SELECT category_id, monthly_cents FROM budgets'))
        left = {r[0]: r[1] for r in budget_rows}
    assert left == {10: 8667, 11: 25000, 12: 80000}
    # 0031 (D64): a budget is its bills and what is on top, and only that is kept.
    command.upgrade(cfg, 'head')
    with engine.connect() as c:
        budget_rows = c.execute(text('SELECT category_id, monthly_cents FROM budgets'))
        left = {r[0]: r[1] for r in budget_rows}
    assert left == {10: 0, 11: 5000, 12: 80000}
    command.downgrade(cfg, '0008')
    with engine.connect() as c:
        budget_rows = c.execute(text('SELECT category_id, monthly_cents FROM budgets'))
        left = {r[0]: r[1] for r in budget_rows}
    assert left == {11: 5000, 12: 80000}
    engine.dispose()


def test_a_goal_line_moves_onto_the_category(tmp_path: Path) -> None:
    url = f'sqlite:///{tmp_path / "fiscus.db"}'
    cfg = alembic_config(url)
    command.upgrade(cfg, '0015')
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(
            text(
                'INSERT INTO users (id, username, display_name, is_admin, is_active,'
                ' user_handle, password_enabled, data_version, created_at)'
                " VALUES (1, 'alex', 'alex', 0, 1, :h, 0, 0, '2026-01-01')"
            ),
            {'h': bytes([1]) * 64},
        )
        for cid, name in ((10, 'Rego'), (11, 'Power')):
            c.execute(
                text(
                    'INSERT INTO categories (id, user_id, name, is_business, is_deductible,'
                    ' is_income, is_transfer, budget_position) VALUES (:id, 1, :n, 0, 0, 0, 0, 0)'
                ),
                {'id': cid, 'n': name},
            )
        # Two goals on Rego: the open one keeps it.
        goals: list[tuple[int, int | None, int]] = [(1, 10, 1), (2, 10, 0), (3, None, 0)]
        for gid, line, archived in goals:
            c.execute(
                text(
                    'INSERT INTO goals (id, user_id, name, keep_aside, archived, is_tax,'
                    " category_id, created_at) VALUES (:id, 1, 'g', 1, :a, 0, :c, '2026-01-01')"
                ),
                {'id': gid, 'c': line, 'a': archived},
            )
    command.upgrade(cfg, '0016')
    assert 'category_id' not in {col['name'] for col in inspect(engine).get_columns('goals')}
    with engine.connect() as c:
        rows = c.execute(text('SELECT id, goal_id FROM categories ORDER BY id'))
        assert [tuple(r) for r in rows] == [(10, 2), (11, None)]
    command.downgrade(cfg, '0015')
    with engine.connect() as c:
        rows = c.execute(text('SELECT id, category_id FROM goals ORDER BY id'))
        assert [tuple(r) for r in rows] == [(1, None), (2, 10), (3, None)]
    engine.dispose()


def test_transactions_rebuilt_keep_the_bill_payments_matched_to_them(tmp_path: Path) -> None:
    url = f'sqlite:///{tmp_path / "fiscus.db"}'
    cfg = alembic_config(url)
    command.upgrade(cfg, '0019')
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(
            text(
                'INSERT INTO users (id, username, display_name, is_admin, is_active,'
                ' user_handle, password_enabled, data_version, created_at)'
                " VALUES (1, 'alex', 'alex', 0, 1, :h, 0, 0, '2026-01-01')"
            ),
            {'h': bytes([1]) * 64},
        )
        c.execute(
            text(
                'INSERT INTO accounts (id, user_id, name, type, opening_balance_cents,'
                " is_set_aside, archived, created_at) VALUES (1, 1, 'Everyday', 'transaction',"
                " 0, 0, 0, '2026-01-01')"
            )
        )
        c.execute(
            text(
                'INSERT INTO transactions (id, user_id, account_id, date, amount_cents,'
                " description, dedup_hash, created_at) VALUES (1, 1, 1, '2026-09-15', -12140,"
                " 'ORIGIN ENERGY', 'h', '2026-09-15')"
            )
        )
        c.execute(
            text(
                'INSERT INTO bills (id, owner_id, name, amount_cents, frequency, next_due,'
                ' split_mode, match_tolerance_pct, match_window_days, description_hint,'
                " active, created_at) VALUES (1, 1, 'Power', 12140, 'monthly', '2026-09-15',"
                " 'equal', 5, 3, '', 1, '2026-01-01')"
            )
        )
        c.execute(
            text(
                'INSERT INTO bill_payments (id, bill_id, user_id, due_date, paid_on,'
                " transaction_id, created_at) VALUES (1, 1, 1, '2026-09-15', '2026-09-15', 1,"
                " '2026-09-15')"
            )
        )
    command.upgrade(cfg, '0020')
    cols = {col['name'] for col in inspect(engine).get_columns('transactions')}
    assert {'bill_payment_id', 'payslip_id'} <= cols
    with engine.connect() as c:
        assert c.execute(text('SELECT transaction_id FROM bill_payments')).scalar_one() == 1
        assert c.execute(text('SELECT dedup_hash FROM transactions')).scalar_one() == 'h'
    command.downgrade(cfg, '0019')
    with engine.connect() as c:
        assert c.execute(text('SELECT transaction_id FROM bill_payments')).scalar_one() == 1
    engine.dispose()


def test_transactions_rebuilt_for_spends_keep_their_links(tmp_path: Path) -> None:
    url = f'sqlite:///{tmp_path / "fiscus.db"}'
    cfg = alembic_config(url)
    command.upgrade(cfg, '0022')
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(
            text(
                'INSERT INTO users (id, username, display_name, is_admin, is_active,'
                ' user_handle, password_enabled, data_version, created_at)'
                " VALUES (1, 'alex', 'alex', 0, 1, :h, 0, 0, '2026-01-01')"
            ),
            {'h': bytes([1]) * 64},
        )
        c.execute(
            text(
                'INSERT INTO accounts (id, user_id, name, type, opening_balance_cents,'
                " is_set_aside, archived, created_at) VALUES (1, 1, 'Everyday', 'transaction',"
                " 0, 0, 0, '2026-01-01')"
            )
        )
        c.execute(
            text(
                'INSERT INTO bills (id, owner_id, name, amount_cents, frequency, next_due,'
                ' split_mode, match_tolerance_pct, match_window_days, description_hint,'
                " active, created_at) VALUES (1, 1, 'Power', 12140, 'monthly', '2026-09-15',"
                " 'equal', 5, 3, '', 1, '2026-01-01')"
            )
        )
        c.execute(
            text(
                'INSERT INTO bill_payments (id, bill_id, user_id, due_date, paid_on,'
                " created_at) VALUES (1, 1, 1, '2026-09-15', '2026-09-15', '2026-09-15')"
            )
        )
        c.execute(
            text(
                'INSERT INTO transactions (id, user_id, account_id, date, amount_cents,'
                ' description, dedup_hash, created_at, bill_payment_id) VALUES'
                " (1, 1, 1, '2026-09-15', -12140, 'Power', 'bill-payment:1', '2026-09-15', 1)"
            )
        )
    command.upgrade(cfg, '0023')
    cols = {col['name'] for col in inspect(engine).get_columns('transactions')}
    assert 'budget_spend_id' in cols
    fks = {fk['referred_table']: fk for fk in inspect(engine).get_foreign_keys('transactions')}
    assert fks['budget_spends']['options'] == {'ondelete': 'CASCADE'}
    with engine.begin() as c:
        assert c.execute(text('SELECT bill_payment_id FROM transactions')).scalar_one() == 1
        c.execute(
            text(
                'INSERT INTO categories (id, user_id, name, is_business, is_deductible,'
                " is_income, is_transfer, budget_position) VALUES (1, 1, 'Groceries', 0, 0, 0,"
                ' 0, 0)'
            )
        )
        c.execute(
            text(
                'INSERT INTO budget_spends (id, user_id, category_id, date, amount_cents, note,'
                " created_at) VALUES (1, 1, 1, '2026-10-01', 2500, '', '2026-10-01')"
            )
        )
        c.execute(
            text(
                'INSERT INTO transactions (id, user_id, account_id, date, amount_cents,'
                ' description, dedup_hash, created_at, budget_spend_id) VALUES'
                " (2, 1, 1, '2026-10-01', -2500, 'Groceries', 'budget-spend:1', '2026-10-01', 1)"
            )
        )
    command.downgrade(cfg, '0022')
    with engine.connect() as c:
        assert c.execute(text('SELECT id FROM transactions')).scalars().all() == [1]
        assert c.execute(text('SELECT bill_payment_id FROM transactions')).scalar_one() == 1
    engine.dispose()


def test_a_shared_bill_becomes_one_bill_each(tmp_path: Path) -> None:
    """0025 (D52): each person keeps their share as a bill of their own, with
    their account, category, payments and reminders, so nothing moves."""
    url = f'sqlite:///{tmp_path / "fiscus.db"}'
    cfg = alembic_config(url)
    command.upgrade(cfg, '0024')
    engine = create_engine(url)
    with engine.begin() as c:
        for uid, name in ((1, 'alex'), (2, 'sam'), (3, 'kim')):
            c.execute(
                text(
                    'INSERT INTO users (id, username, display_name, is_admin, is_active,'
                    ' user_handle, password_enabled, data_version, created_at)'
                    " VALUES (:id, :n, :n, 0, 1, :h, 0, 0, '2026-01-01')"
                ),
                {'id': uid, 'n': name, 'h': bytes([uid]) * 64},
            )
            c.execute(
                text(
                    'INSERT INTO accounts (id, user_id, name, type, opening_balance_cents,'
                    " is_set_aside, archived, created_at) VALUES (:id, :id, 'Everyday',"
                    " 'transaction', 0, 0, 0, '2026-01-01')"
                ),
                {'id': uid},
            )
            c.execute(
                text(
                    'INSERT INTO categories (id, user_id, name, is_business, is_deductible,'
                    " is_income, is_transfer, budget_position) VALUES (:id, :id, 'Utilities',"
                    ' 0, 0, 0, 0, 0)'
                ),
                {'id': 10 + uid},
            )
        # Internet, 90 a month, 60/40 with sam; Power, alex's alone.
        for bid, name, cents, mode in (
            (1, 'Internet', 9000, 'percent'),
            (2, 'Power', 20000, 'equal'),
        ):
            c.execute(
                text(
                    'INSERT INTO bills (id, owner_id, name, amount_cents, frequency, next_due,'
                    ' split_mode, match_tolerance_pct, match_window_days, description_hint,'
                    " active, created_at) VALUES (:id, 1, :n, :a, 'monthly', '2026-09-10',"
                    " :m, 5, 3, 'hint', 1, '2026-01-01')"
                ),
                {'id': bid, 'n': name, 'a': cents, 'm': mode},
            )
        shares = [(1, 1, 6000, 1, 11), (1, 2, 4000, 2, 12), (2, 1, 0, 1, 11)]
        for bid, uid, value, acct, cat in shares:
            c.execute(
                text(
                    'INSERT INTO bill_shares (bill_id, user_id, split_value, account_id,'
                    ' category_id) VALUES (:b, :u, :v, :a, :c)'
                ),
                {'b': bid, 'u': uid, 'v': value, 'a': acct, 'c': cat},
            )
        # Each paid September's; kim paid once before leaving the bill.
        for pid, uid, cents in ((1, 1, -5400), (2, 2, -3600), (3, 3, -3000)):
            c.execute(
                text(
                    'INSERT INTO bill_payments (id, bill_id, user_id, due_date, paid_on,'
                    " created_at) VALUES (:id, 1, :u, '2026-09-10', '2026-09-10', '2026-09-10')"
                ),
                {'id': pid, 'u': uid},
            )
            c.execute(
                text(
                    'INSERT INTO transactions (id, user_id, account_id, date, amount_cents,'
                    ' description, dedup_hash, created_at, bill_payment_id) VALUES'
                    " (:id, :u, :u, '2026-09-10', :a, 'Internet', :h, '2026-09-10', :id)"
                ),
                {'id': pid, 'u': uid, 'a': cents, 'h': f'bill-payment:{pid}'},
            )
        c.execute(
            text(
                'INSERT INTO notification_log (user_id, kind, key, sent_at) VALUES'
                " (2, 'overdue', '1:2026-09-10', '2026-09-11'),"
                " (1, 'overdue', '1:2026-09-10', '2026-09-11')"
            )
        )

    command.upgrade(cfg, '0025')
    insp = inspect(engine)
    assert 'bill_shares' not in insp.get_table_names()
    cols = {col['name'] for col in insp.get_columns('bills')}
    assert {'user_id', 'account_id', 'category_id'} <= cols
    assert not {'owner_id', 'split_mode'} & cols
    with engine.connect() as c:
        rows = c.execute(
            text(
                'SELECT id, user_id, name, amount_cents, account_id, category_id,'
                ' description_hint FROM bills ORDER BY id'
            )
        )
        assert [tuple(r) for r in rows] == [
            (1, 1, 'Internet', 5400, 1, 11, 'hint'),
            (2, 1, 'Power', 20000, 1, 11, 'hint'),
            (3, 2, 'Internet', 3600, 2, 12, 'hint'),
        ]
        payments = c.execute(text('SELECT id, bill_id, user_id FROM bill_payments ORDER BY id'))
        assert [tuple(r) for r in payments] == [(1, 1, 1), (2, 3, 2)]
        # Kim's payment is gone; the money it moved is not. Its transaction lets
        # go of the payment's key, which the next payment may be given.
        txns = c.execute(
            text('SELECT id, amount_cents, bill_payment_id, dedup_hash FROM transactions')
        )
        assert sorted(tuple(r) for r in txns) == [
            (1, -5400, 1, 'bill-payment:1'),
            (2, -3600, 2, 'bill-payment:2'),
            (3, -3000, None, 'bill-left:3'),
        ]
        keys = c.execute(text('SELECT user_id, key FROM notification_log ORDER BY user_id'))
        assert [tuple(r) for r in keys] == [(1, '1:2026-09-10'), (2, '3:2026-09-10')]

    command.downgrade(cfg, '0024')
    with engine.connect() as c:
        shares_back = c.execute(
            text('SELECT bill_id, user_id, account_id, category_id FROM bill_shares ORDER BY 1')
        )
        assert [tuple(r) for r in shares_back] == [(1, 1, 1, 11), (2, 1, 1, 11), (3, 2, 2, 12)]
        owners = c.execute(text('SELECT id, owner_id, split_mode FROM bills ORDER BY id'))
        assert [tuple(r) for r in owners] == [(1, 1, 'equal'), (2, 1, 'equal'), (3, 2, 'equal')]
    command.upgrade(cfg, 'head')
    engine.dispose()


def test_a_bill_whose_owner_holds_no_share_still_moves(tmp_path: Path) -> None:
    """0025 with a bill only someone else held a share of: no crash, and the
    whole amount is still covered between them."""
    url = f'sqlite:///{tmp_path / "fiscus.db"}'
    cfg = alembic_config(url)
    command.upgrade(cfg, '0024')
    engine = create_engine(url)
    with engine.begin() as c:
        for uid, name in ((1, 'alex'), (2, 'sam')):
            c.execute(
                text(
                    'INSERT INTO users (id, username, display_name, is_admin, is_active,'
                    ' user_handle, password_enabled, data_version, created_at)'
                    " VALUES (:id, :n, :n, 0, 1, :h, 0, 0, '2026-01-01')"
                ),
                {'id': uid, 'n': name, 'h': bytes([uid]) * 64},
            )
        c.execute(
            text(
                'INSERT INTO bills (id, owner_id, name, amount_cents, frequency, next_due,'
                ' split_mode, match_tolerance_pct, match_window_days, description_hint,'
                " active, created_at) VALUES (1, 1, 'Water', 10001, 'monthly', '2026-09-10',"
                " 'equal', 5, 3, '', 1, '2026-01-01')"
            )
        )
        c.execute(text('INSERT INTO bill_shares (bill_id, user_id, split_value) VALUES (1, 2, 0)'))

    command.upgrade(cfg, '0025')
    with engine.connect() as c:
        rows = c.execute(text('SELECT user_id, amount_cents FROM bills ORDER BY id')).all()
        assert [r.user_id for r in rows] == [1, 2]
        assert sum(r.amount_cents for r in rows) == 10001
    engine.dispose()


def test_the_password_columns_go_and_everything_owned_stays(tmp_path: Path) -> None:
    """0026 rebuilds users without the password columns (D52). Every owned
    table points at it with ON DELETE CASCADE, so nothing may go with it."""
    url = f'sqlite:///{tmp_path / "fiscus.db"}'
    cfg = alembic_config(url)
    command.upgrade(cfg, '0025')
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(
            text(
                'INSERT INTO users (id, username, display_name, is_admin, is_active,'
                ' user_handle, password_enabled, password_hash, data_version, created_at)'
                " VALUES (1, 'alex', 'Alex', 1, 1, :h, 1, 'x', 7, '2026-01-01')"
            ),
            {'h': bytes([1]) * 64},
        )
        c.execute(
            text(
                'INSERT INTO webauthn_credentials (user_id, credential_id, public_key,'
                ' sign_count, transports, name, backed_up, created_at) VALUES (1, :cid, :pk,'
                " 0, '[]', 'Phone', 0, '2026-01-01')"
            ),
            {'cid': b'c' * 16, 'pk': b'k' * 16},
        )
        c.execute(
            text(
                'INSERT INTO accounts (id, user_id, name, type, opening_balance_cents,'
                " is_set_aside, archived, created_at) VALUES (1, 1, 'Everyday', 'transaction',"
                " 100, 0, 0, '2026-01-01')"
            )
        )
        c.execute(
            text(
                'INSERT INTO bills (id, user_id, name, amount_cents, frequency, next_due,'
                ' match_tolerance_pct, match_window_days, description_hint, active,'
                " created_at, account_id) VALUES (1, 1, 'Power', 12140, 'monthly',"
                " '2026-09-15', 5, 3, '', 1, '2026-01-01', 1)"
            )
        )

    command.upgrade(cfg, '0026')
    cols = {col['name'] for col in inspect(engine).get_columns('users')}
    assert not {'password_enabled', 'password_hash'} & cols
    with engine.connect() as c:
        user = c.execute(text('SELECT username, is_admin, data_version FROM users')).one()
        assert tuple(user) == ('alex', 1, 7)
        for table in ('webauthn_credentials', 'accounts', 'bills'):
            assert c.execute(text(f'SELECT count(*) FROM {table}')).scalar_one() == 1  # noqa: S608
        assert c.execute(text('PRAGMA foreign_key_check')).all() == []

    command.downgrade(cfg, '0025')
    with engine.connect() as c:
        back = c.execute(text('SELECT password_enabled, password_hash FROM users')).one()
        assert tuple(back) == (0, None)
        assert c.execute(text('SELECT count(*) FROM bills')).scalar_one() == 1
    command.upgrade(cfg, 'head')
    engine.dispose()


def test_the_users_rebuild_refuses_with_foreign_keys_on(tmp_path: Path) -> None:
    """With foreign keys on, dropping users to rebuild it would cascade into
    every owned table: 0026 refuses instead."""
    url = f'sqlite:///{tmp_path / "fiscus.db"}'
    command.upgrade(alembic_config(url), '0025')
    path = MIGRATIONS_DIR / 'versions' / '0026_no_password_columns.py'
    spec = importlib.util.spec_from_file_location('m0026', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine(url)

    @event.listens_for(engine, 'connect')
    def _on(dbapi_conn: Any, _record: Any) -> None:
        dbapi_conn.execute('PRAGMA foreign_keys=ON')

    with (
        engine.connect() as conn,
        Operations.context(MigrationContext.configure(conn)),
        pytest.raises(RuntimeError, match='foreign keys on'),
    ):
        module.upgrade()
    cols = {col['name'] for col in inspect(engine).get_columns('users')}
    assert 'password_hash' in cols
    engine.dispose()


def test_import_mappings_go(tmp_path: Path) -> None:
    """0027 drops import_mappings, unread since D37, and only that."""
    url = f'sqlite:///{tmp_path / "fiscus.db"}'
    cfg = alembic_config(url)
    command.upgrade(cfg, '0026')
    engine = create_engine(url)
    before = set(inspect(engine).get_table_names())
    assert 'import_mappings' in before
    command.upgrade(cfg, '0027')
    assert set(inspect(engine).get_table_names()) == before - {'import_mappings'}
    command.downgrade(cfg, '0026')
    assert set(inspect(engine).get_table_names()) == before
    command.upgrade(cfg, 'head')
    engine.dispose()


def test_square_goes_and_its_transactions_stay(tmp_path: Path) -> None:
    """0028 (D55) drops Square's connection and invoices, the rules and the bill
    matching columns. What Square synced, and payments matched to it, stay."""
    url = f'sqlite:///{tmp_path / "fiscus.db"}'
    cfg = alembic_config(url)
    command.upgrade(cfg, '0027')
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(
            text(
                'INSERT INTO users (id, username, display_name, is_admin, is_active,'
                " user_handle, data_version, created_at) VALUES (1, 'alex', 'Alex', 1, 1, :h,"
                " 0, '2026-01-01')"
            ),
            {'h': bytes([1]) * 64},
        )
        c.execute(
            text(
                'INSERT INTO accounts (id, user_id, name, type, opening_balance_cents,'
                " is_set_aside, archived, created_at) VALUES (1, 1, 'Square', 'transaction',"
                " 0, 0, 0, '2026-01-01')"
            )
        )
        c.execute(
            text(
                'INSERT INTO categories (id, user_id, name, is_business, is_deductible,'
                " is_income, is_transfer, budget_position) VALUES (1, 1, 'Sales', 1, 0, 1, 0, 0)"
            )
        )
        c.execute(
            text(
                'INSERT INTO square_connections (user_id, enabled, access_token, account_id,'
                " start_date, last_result, last_error) VALUES (1, 1, 'EAAA-secret', 1,"
                " '2026-01-01', '', '')"
            )
        )
        c.execute(
            text(
                'INSERT INTO square_invoices (user_id, square_id, number, customer, due_date,'
                " amount_due_cents, status) VALUES (1, 'inv', '7', 'Kim', '2026-11-01', 5000,"
                " 'UNPAID')"
            )
        )
        c.execute(
            text(
                'INSERT INTO rules (user_id, pattern, is_regex, category_id, priority,'
                " created_at) VALUES (1, 'SQUARE', 0, 1, 100, '2026-01-01')"
            )
        )
        c.execute(
            text(
                'INSERT INTO bills (id, user_id, name, amount_cents, frequency, next_due,'
                ' match_tolerance_pct, match_window_days, description_hint, active,'
                " created_at, account_id) VALUES (1, 1, 'Fees', 2000, 'monthly', '2026-09-15',"
                " 5, 3, 'square', 1, '2026-01-01', 1)"
            )
        )
        c.execute(
            text(
                'INSERT INTO transactions (id, user_id, account_id, date, amount_cents,'
                " description, dedup_hash, created_at) VALUES (1, 1, 1, '2026-09-15', -2000,"
                " 'SQUARE FEES', 'sq:1', '2026-09-15')"
            )
        )
        c.execute(
            text(
                'INSERT INTO bill_payments (bill_id, user_id, due_date, paid_on,'
                " transaction_id, created_at) VALUES (1, 1, '2026-09-15', '2026-09-15', 1,"
                " '2026-09-15')"
            )
        )

    command.upgrade(cfg, '0028')
    insp = inspect(engine)
    assert not {'square_connections', 'square_invoices', 'rules'} & set(insp.get_table_names())
    cols = {col['name'] for col in insp.get_columns('bills')}
    assert not {'match_tolerance_pct', 'match_window_days', 'description_hint'} & cols
    with engine.connect() as c:
        assert c.execute(text('SELECT name, amount_cents FROM bills')).one() == ('Fees', 2000)
        assert c.execute(text('SELECT transaction_id FROM bill_payments')).scalar_one() == 1
        assert c.execute(text('SELECT amount_cents FROM transactions')).scalar_one() == -2000
        assert c.execute(text('PRAGMA foreign_key_check')).all() == []

    command.downgrade(cfg, '0027')
    insp = inspect(engine)
    assert {'square_connections', 'square_invoices', 'rules'} <= set(insp.get_table_names())
    with engine.connect() as c:
        back = c.execute(
            text('SELECT match_tolerance_pct, match_window_days, description_hint FROM bills')
        ).one()
        assert tuple(back) == (5, 3, '')
        assert c.execute(text('SELECT count(*) FROM bill_payments')).scalar_one() == 1
    command.upgrade(cfg, 'head')
    engine.dispose()


def test_a_bill_payment_keeps_what_was_paid(tmp_path: Path) -> None:
    """0029 (D57) gives each payment its amount: its own transaction, else the
    synced one it was matched to, else what its bill says."""
    url = f'sqlite:///{tmp_path / "fiscus.db"}'
    cfg = alembic_config(url)
    command.upgrade(cfg, '0028')
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(
            text(
                'INSERT INTO users (id, username, display_name, is_admin, is_active,'
                " user_handle, data_version, created_at) VALUES (1, 'alex', 'Alex', 1, 1, :h,"
                " 0, '2026-01-01')"
            ),
            {'h': bytes([1]) * 64},
        )
        c.execute(
            text(
                'INSERT INTO accounts (id, user_id, name, type, opening_balance_cents,'
                " is_set_aside, archived, created_at) VALUES (1, 1, 'Everyday', 'transaction',"
                " 0, 0, 0, '2026-01-01')"
            )
        )
        c.execute(
            text(
                'INSERT INTO bills (id, user_id, name, amount_cents, frequency, next_due,'
                " active, created_at, account_id) VALUES (1, 1, 'Power', 35000, 'monthly',"
                " '2026-07-01', 1, '2026-01-01', 1)"
            )
        )
        for pid, due in ((1, '2026-07-01'), (2, '2026-08-01'), (3, '2026-09-01')):
            c.execute(
                text(
                    'INSERT INTO bill_payments (id, bill_id, user_id, due_date, paid_on,'
                    " created_at) VALUES (:id, 1, 1, :due, :due, '2026-01-01')"
                ),
                {'id': pid, 'due': due},
            )
        # Marked paid by hand (D42), and matched to a synced payment.
        c.execute(
            text(
                'INSERT INTO transactions (id, user_id, account_id, date, amount_cents,'
                ' description, dedup_hash, created_at, bill_payment_id) VALUES (1, 1, 1,'
                " '2026-07-01', -30000, 'Power', 'bill-payment:1', '2026-07-01', 1)"
            )
        )
        c.execute(
            text(
                'INSERT INTO transactions (id, user_id, account_id, date, amount_cents,'
                " description, dedup_hash, created_at) VALUES (2, 1, 1, '2026-08-02', -31250,"
                " 'ORIGIN ENERGY', 'sq:2', '2026-08-02')"
            )
        )
        c.execute(text('UPDATE bill_payments SET transaction_id = 2 WHERE id = 2'))

    command.upgrade(cfg, '0029')
    with engine.connect() as c:
        paid: dict[int, int] = dict(
            c.execute(text('SELECT id, amount_cents FROM bill_payments')).all()
        )
    assert paid == {1: 30000, 2: 31250, 3: 35000}
    command.downgrade(cfg, '0028')
    cols = {col['name'] for col in inspect(engine).get_columns('bill_payments')}
    assert 'amount_cents' not in cols
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM transactions')).scalar_one() == 2
    engine.dispose()


def test_a_monthly_line_holding_a_yearly_bill_starts_saving(tmp_path: Path) -> None:
    """0030 (D63) starts a monthly line saving from this budget month when it
    holds a running bill paid less often, by its person's start day."""
    url = f'sqlite:///{tmp_path / "fiscus.db"}'
    cfg = alembic_config(url)
    command.upgrade(cfg, '0029')
    engine = create_engine(url)
    with engine.begin() as c:
        for uid, name in ((1, 'alex'), (2, 'sam')):
            c.execute(
                text(
                    'INSERT INTO users (id, username, display_name, is_admin, is_active,'
                    ' user_handle, data_version, created_at) VALUES (:id, :n, :n, 1, 1, :h,'
                    " 0, '2026-01-01')"
                ),
                {'id': uid, 'n': name, 'h': bytes([uid]) * 64},
            )
        # Alex's budget month starts on the 15th; Sam has no settings (the 1st).
        c.execute(
            text(
                'INSERT INTO user_settings (user_id, low_balance_cents, forecast_months,'
                ' ntfy_enabled, ntfy_url, ntfy_topic, ntfy_token, ntfy_bill_days,'
                ' ntfy_bills_due, ntfy_overdue, ntfy_low_balance, ntfy_hour,'
                " budget_start_day) VALUES (1, 0, 6, 0, '', '', '', 3, 1, 1, 1, 8, 15)"
            )
        )
        # id, user, name, income, cycle, saved since, bill frequency, active, end
        lines: list[tuple[Any, ...]] = [
            (1, 1, 'Phone', 0, 1, None, 'yearly', 1, None),
            (2, 1, 'Groceries', 0, 1, None, 'monthly', 1, None),
            (3, 1, 'Rego', 0, 12, '2026-09-15', 'yearly', 1, None),
            (4, 1, 'Gym', 0, 1, None, 'yearly', 0, None),
            (5, 1, 'Old cover', 0, 1, None, 'yearly', 1, '2020-01-01'),
            (6, 1, 'Bonus', 1, 1, None, 'yearly', 1, None),
            (7, 2, 'Water', 0, 1, None, 'quarterly', 1, None),
        ]
        for cid, uid, name, income, cycle, since, freq, active, end in lines:
            c.execute(
                text(
                    'INSERT INTO categories (id, user_id, name, is_business, is_deductible,'
                    ' is_income, is_transfer) VALUES (:id, :u, :n, 0, 0, :inc, 0)'
                ),
                {'id': cid, 'u': uid, 'n': name, 'inc': income},
            )
            c.execute(
                text(
                    'INSERT INTO budgets (id, user_id, category_id, monthly_cents,'
                    ' use_in_forecast, cycle_months, saved_since, saved_opening_cents)'
                    ' VALUES (:id, :u, :id, 5000, 0, :cy, :since, 0)'
                ),
                {'id': cid, 'u': uid, 'cy': cycle, 'since': since},
            )
            c.execute(
                text(
                    'INSERT INTO bills (id, user_id, name, amount_cents, frequency, next_due,'
                    ' end_date, active, created_at, category_id) VALUES (:id, :u, :n, 17000,'
                    " :f, '2027-03-15', :end, :a, '2026-01-01', :id)"
                ),
                {'id': cid, 'u': uid, 'n': name, 'f': freq, 'end': end, 'a': active},
            )

    def starts(day: int) -> str:
        tz = ZoneInfo(os.environ.get('FISCUS_TIMEZONE') or 'Australia/Perth')
        return recurrence.month_period(datetime.now(tz).date(), day)[0].isoformat()

    before = (starts(15), starts(1))
    command.upgrade(cfg, '0030')
    after = (starts(15), starts(1))
    with engine.connect() as c:
        since = dict(c.execute(text('SELECT id, saved_since FROM budgets')).all())
    assert (since[1], since[7]) in (before, after)
    assert since[3] == '2026-09-15'
    assert [since[i] for i in (2, 4, 5, 6)] == [None] * 4
    command.downgrade(cfg, '0029')
    with engine.connect() as c:
        since = dict(c.execute(text('SELECT id, saved_since FROM budgets')).all())
    assert since == {1: None, 2: None, 3: '2026-09-15', 4: None, 5: None, 6: None, 7: None}
    engine.dispose()


def test_a_budget_keeps_only_what_is_on_top_of_its_bills(tmp_path: Path) -> None:
    """0031 (D64) stores each line's estimate: what it had less its recurring
    bills, never below nothing. A saved-up line carries forward what it held
    first, and a spending category holding a bill gets a line."""
    url = f'sqlite:///{tmp_path / "fiscus.db"}'
    cfg = alembic_config(url)
    command.upgrade(cfg, '0030')
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(
            text(
                'INSERT INTO users (id, username, display_name, is_admin, is_active,'
                " user_handle, data_version, created_at) VALUES (1, 'alex', 'Alex', 1, 1, :h,"
                " 0, '2026-01-01')"
            ),
            {'h': bytes([1]) * 64},
        )
        # id: name, income, transfer
        cats = {
            1: ('Phone', 0, 0),
            2: ('Groceries', 0, 0),
            3: ('Rego', 0, 0),
            4: ('Housing', 0, 0),
            5: ('Utilities', 0, 0),
            6: ('Insurance', 0, 0),
            7: ('Salary', 1, 0),
            8: ('Transfers', 0, 1),
            9: ('Gym', 0, 0),
        }
        for cid, (name, income, transfer) in cats.items():
            c.execute(
                text(
                    'INSERT INTO categories (id, user_id, name, is_business, is_deductible,'
                    ' is_income, is_transfer) VALUES (:id, 1, :n, 0, 0, :i, :t)'
                ),
                {'id': cid, 'n': name, 'i': income, 't': transfer},
            )
        # category: monthly, total, cycle, saved since
        lines = {
            1: (3900, 3900, 1, None),
            2: (60000, 60000, 1, None),
            3: (7500, 90000, 12, '2026-01-01'),
            4: (20000, 20000, 1, None),
            7: (500000, 500000, 1, None),
        }
        for cid, (monthly, total, cycle, since) in lines.items():
            c.execute(
                text(
                    'INSERT INTO budgets (user_id, category_id, monthly_cents, total_cents,'
                    ' use_in_forecast, cycle_months, saved_since, saved_opening_cents)'
                    ' VALUES (1, :c, :m, :t, 0, :cy, :s, 0)'
                ),
                {'c': cid, 'm': monthly, 't': total, 'cy': cycle, 's': since},
            )
        # category, amount, frequency, active
        bills = [
            (1, 3900, 'monthly', 1),
            (1, 17000, 'yearly', 1),
            (3, 90000, 'yearly', 1),
            (4, 30000, 'quarterly', 1),
            (5, 12140, 'monthly', 1),
            (6, 185000, 'yearly', 1),
            (8, 50000, 'monthly', 1),
            (9, 6000, 'monthly', 0),
        ]
        for n, (cid, amount, freq, active) in enumerate(bills, start=1):
            c.execute(
                text(
                    'INSERT INTO bills (id, user_id, name, amount_cents, frequency, next_due,'
                    ' active, created_at, category_id) VALUES (:id, 1, :n, :a, :f,'
                    " '2027-03-15', :act, '2026-01-01', :c)"
                ),
                {'id': n, 'n': f'Bill {n}', 'a': amount, 'f': freq, 'act': active, 'c': cid},
            )
        # Last March's rego, paid.
        c.execute(
            text(
                'INSERT INTO bill_payments (id, bill_id, user_id, due_date, paid_on,'
                " created_at, amount_cents) VALUES (1, 3, 1, '2026-03-15', '2026-03-15',"
                " '2026-03-15', 90000)"
            )
        )

    def start() -> date:
        tz = ZoneInfo(os.environ.get('FISCUS_TIMEZONE') or 'Australia/Perth')
        return recurrence.month_period(datetime.now(tz).date(), 1)[0]

    first = start()
    command.upgrade(cfg, '0031')
    if start() != first:  # a new budget month began while it ran
        first = start()
    with engine.connect() as c:
        got = {
            r[0]: tuple(r[1:])
            for r in c.execute(
                text(
                    'SELECT category_id, monthly_cents, total_cents, cycle_months, saved_since,'
                    ' saved_opening_cents FROM budgets'
                )
            )
        }
    # Rego saved 75 a month from January and paid 900 in March: carried forward.
    months = (first.year - 2026) * 12 + first.month - 1
    held = 7500 * months - 90000
    since = first.isoformat()
    assert got == {
        1: (0, 0, 1, None, 0),  # 39 is below its 53.17 of bills: it rises to them
        2: (60000, 60000, 1, None, 0),
        3: (0, 0, 12, since, held),
        4: (10000, 10000, 1, None, 0),  # 100 a month of rates, 100 on top
        5: (0, 0, 1, None, 0),
        6: (0, 0, 1, since, 0),  # a yearly bill: saved up (D63)
        7: (500000, 500000, 1, None, 0),
    }
    command.downgrade(cfg, '0030')
    with engine.connect() as c:
        back: dict[int, int] = dict(
            c.execute(text('SELECT category_id, monthly_cents FROM budgets')).all()
        )
    assert back == {1: 5317, 2: 60000, 3: 7500, 4: 20000, 5: 12140, 6: 15417, 7: 500000}
    engine.dispose()


def test_bills_start_not_varying_and_keep_their_payments(tmp_path: Path) -> None:
    """0032 (D66) adds whether a bill varies, off for every bill; going back
    drops it and leaves the payments that point at bills."""
    url = f'sqlite:///{tmp_path / "fiscus.db"}'
    cfg = alembic_config(url)
    command.upgrade(cfg, '0031')
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(
            text(
                'INSERT INTO users (id, username, display_name, is_admin, is_active,'
                " user_handle, data_version, created_at) VALUES (1, 'alex', 'Alex', 1, 1, :h,"
                " 0, '2026-01-01')"
            ),
            {'h': bytes([1]) * 64},
        )
        c.execute(
            text(
                'INSERT INTO bills (id, user_id, name, amount_cents, frequency, next_due,'
                " active, created_at) VALUES (1, 1, 'Power', 30000, 'quarterly',"
                " '2026-07-01', 1, '2026-01-01')"
            )
        )
        c.execute(
            text(
                'INSERT INTO bill_payments (id, bill_id, user_id, due_date, paid_on,'
                " amount_cents, created_at) VALUES (1, 1, 1, '2026-07-01', '2026-07-01',"
                " 31250, '2026-07-01')"
            )
        )
    command.upgrade(cfg, '0032')
    with engine.connect() as c:
        assert c.execute(text('SELECT varies, amount_cents FROM bills')).one() == (0, 30000)
    command.downgrade(cfg, '0031')
    cols = {col['name'] for col in inspect(engine).get_columns('bills')}
    assert 'varies' not in cols
    with engine.connect() as c:
        assert c.execute(text('SELECT amount_cents FROM bill_payments')).scalar_one() == 31250
    engine.dispose()


def test_loans_start_without_a_first_repayment_and_keep_their_rates(tmp_path: Path) -> None:
    """0034 (D71) adds a loan's first repayment, empty for every loan; going
    back drops it and leaves the rates that point at loans."""
    url = f'sqlite:///{tmp_path / "fiscus.db"}'
    cfg = alembic_config(url)
    command.upgrade(cfg, '0033')
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(
            text(
                'INSERT INTO users (id, username, display_name, is_admin, is_active,'
                " user_handle, data_version, created_at) VALUES (1, 'alex', 'Alex', 1, 1, :h,"
                " 0, '2026-01-01')"
            ),
            {'h': bytes([1]) * 64},
        )
        c.execute(
            text(
                'INSERT INTO loans (id, user_id, name, principal_cents, start_date,'
                ' term_months, repayment_frequency, balance_cents, balance_date)'
                " VALUES (1, 1, 'Home', 62377490, '2026-10-26', 360, 'weekly', 62377490,"
                " '2026-10-26')"
            )
        )
        c.execute(
            text(
                'INSERT INTO loan_rates (id, user_id, loan_id, effective_date, rate)'
                " VALUES (1, 1, 1, '2026-10-26', '6.59')"
            )
        )
    command.upgrade(cfg, '0034')
    with engine.connect() as c:
        assert c.execute(text('SELECT first_repayment_date FROM loans')).scalar_one() is None
    command.downgrade(cfg, '0033')
    cols = {col['name'] for col in inspect(engine).get_columns('loans')}
    assert 'first_repayment_date' not in cols
    with engine.connect() as c:
        assert c.execute(text('SELECT rate FROM loan_rates')).scalar_one() == '6.59'
    engine.dispose()


def test_transactions_rebuilt_for_goal_moves_keep_their_links(tmp_path: Path) -> None:
    """0035 (D73) links a transaction to a goal move, cascading; the rebuild
    keeps a bill payment matched to its transaction, and going back drops the
    goal moves' transactions only."""
    url = f'sqlite:///{tmp_path / "fiscus.db"}'
    cfg = alembic_config(url)
    command.upgrade(cfg, '0034')
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(
            text(
                'INSERT INTO users (id, username, display_name, is_admin, is_active,'
                " user_handle, data_version, created_at) VALUES (1, 'alex', 'Alex', 1, 1, :h,"
                " 0, '2026-01-01')"
            ),
            {'h': bytes([1]) * 64},
        )
        c.execute(
            text(
                'INSERT INTO accounts (id, user_id, name, type, opening_balance_cents,'
                " is_set_aside, archived, created_at) VALUES (1, 1, 'Everyday', 'transaction',"
                " 0, 0, 0, '2026-01-01')"
            )
        )
        c.execute(
            text(
                'INSERT INTO transactions (id, user_id, account_id, date, amount_cents,'
                " description, dedup_hash, created_at) VALUES (1, 1, 1, '2026-09-15', -12140,"
                " 'ORIGIN ENERGY', 'h', '2026-09-15')"
            )
        )
        c.execute(
            text(
                'INSERT INTO bills (id, user_id, name, amount_cents, frequency, next_due,'
                " active, varies, created_at) VALUES (1, 1, 'Power', 12140, 'monthly',"
                " '2026-09-15', 1, 0, '2026-01-01')"
            )
        )
        c.execute(
            text(
                'INSERT INTO bill_payments (id, bill_id, user_id, due_date, paid_on,'
                " amount_cents, transaction_id, created_at) VALUES (1, 1, 1, '2026-09-15',"
                " '2026-09-15', 12140, 1, '2026-09-15')"
            )
        )
    command.upgrade(cfg, '0035')
    fks = {fk['referred_table']: fk for fk in inspect(engine).get_foreign_keys('transactions')}
    assert fks['goal_moves']['options'] == {'ondelete': 'CASCADE'}
    with engine.begin() as c:
        assert c.execute(text('SELECT transaction_id FROM bill_payments')).scalar_one() == 1
        assert c.execute(text('SELECT goal_move_id FROM transactions')).scalar_one() is None
        c.execute(
            text(
                'INSERT INTO goals (id, user_id, name, account_id, keep_aside, archived,'
                " is_tax, created_at) VALUES (1, 1, 'Japan', 1, 1, 0, 0, '2026-01-01')"
            )
        )
        c.execute(
            text(
                'INSERT INTO goal_moves (id, user_id, goal_id, date, amount_cents, note,'
                " created_at) VALUES (1, 1, 1, '2026-10-01', -30000, '', '2026-10-01')"
            )
        )
        c.execute(
            text(
                'INSERT INTO transactions (id, user_id, account_id, date, amount_cents,'
                ' description, dedup_hash, created_at, goal_move_id) VALUES'
                " (2, 1, 1, '2026-10-01', -30000, 'Out of Japan', 'goal-move:1', '2026-10-01', 1)"
            )
        )
    command.downgrade(cfg, '0034')
    cols = {col['name'] for col in inspect(engine).get_columns('transactions')}
    assert 'goal_move_id' not in cols
    with engine.connect() as c:
        assert c.execute(text('SELECT id FROM transactions')).scalars().all() == [1]
        assert c.execute(text('SELECT transaction_id FROM bill_payments')).scalar_one() == 1
        assert c.execute(text('SELECT amount_cents FROM goal_moves')).scalar_one() == -30000
    engine.dispose()
