"""Upgrading a database that holds data, for the migrations that move it."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, inspect, text

from fiscus.db import alembic_config


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
    command.upgrade(cfg, 'head')
    with engine.connect() as c:
        budget_rows = c.execute(text('SELECT category_id, monthly_cents FROM budgets'))
        left = {r[0]: r[1] for r in budget_rows}
    assert left == {10: 8667, 11: 25000, 12: 80000}
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
        # Kim's payment is gone; the money it moved is not.
        txns = c.execute(text('SELECT id, amount_cents, bill_payment_id FROM transactions'))
        assert sorted(tuple(r) for r in txns) == [(1, -5400, 1), (2, -3600, 2), (3, -3000, None)]
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
