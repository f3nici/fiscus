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

    command.upgrade(cfg, 'head')

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
    engine.dispose()
