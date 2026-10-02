"""Pure domain logic: recurrence, splits, CSV dedup, rules, bill matching, money."""

from __future__ import annotations

from datetime import date

import pytest

from fiscus.domain import csvimport, recurrence, splits
from fiscus.domain.matching import Candidate, OpenOccurrence, assign, matches
from fiscus.domain.rules import Matcher, RuleError, RuleSpec, validate
from fiscus.money import MoneyError, fmt, parse_cents

# --------------------------------------------------------------------- money


@pytest.mark.parametrize(
    ('raw', 'cents'),
    [
        ('12', 1200),
        ('12.3', 1230),
        ('-45.50', -4550),
        ('$1,234.56', 123456),
        ('(20.00)', -2000),
        ('20.00-', -2000),
        ('+3.005', 301),
        ('0.005', 1),
    ],
)
def test_parse_cents(raw: str, cents: int) -> None:
    assert parse_cents(raw) == cents


def test_parse_cents_rejects_junk() -> None:
    for raw in ('', 'abc', '1e999', 'NaN'):
        with pytest.raises(MoneyError):
            parse_cents(raw)
    with pytest.raises(MoneyError):
        parse_cents('-1', allow_negative=False)


def test_fmt() -> None:
    assert fmt(123456) == '$1,234.56'
    assert fmt(-5) == '-$0.05'
    assert fmt(100, sign=True) == '+$1.00'


# ---------------------------------------------------------------- recurrence


def test_monthly_clamps_to_month_end_and_returns() -> None:
    got = list(
        recurrence.occurrences(date(2026, 1, 31), 'monthly', date(2026, 1, 1), date(2026, 5, 31))
    )
    assert got == [
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
        date(2026, 4, 30),
        date(2026, 5, 31),
    ]


def test_leap_year_february() -> None:
    got = list(
        recurrence.occurrences(date(2027, 12, 31), 'monthly', date(2028, 2, 1), date(2028, 2, 29))
    )
    assert got == [date(2028, 2, 29)]


def test_fortnightly_from_anchor_in_the_past() -> None:
    got = list(
        recurrence.occurrences(
            date(2026, 9, 3), 'fortnightly', date(2026, 10, 1), date(2026, 10, 31)
        )
    )
    assert got == [date(2026, 10, 1), date(2026, 10, 15), date(2026, 10, 29)]


def test_weekly_never_before_anchor() -> None:
    got = list(
        recurrence.occurrences(date(2026, 10, 10), 'weekly', date(2026, 10, 1), date(2026, 10, 20))
    )
    assert got == [date(2026, 10, 10), date(2026, 10, 17)]


def test_quarterly_and_yearly() -> None:
    q = list(
        recurrence.occurrences(date(2026, 2, 15), 'quarterly', date(2026, 1, 1), date(2026, 12, 31))
    )
    assert q == [date(2026, 2, 15), date(2026, 5, 15), date(2026, 8, 15), date(2026, 11, 15)]
    y = list(
        recurrence.occurrences(date(2024, 2, 29), 'yearly', date(2025, 1, 1), date(2028, 12, 31))
    )
    assert y == [date(2025, 2, 28), date(2026, 2, 28), date(2027, 2, 28), date(2028, 2, 29)]


def test_end_date_and_one_off() -> None:
    got = list(
        recurrence.occurrences(
            date(2026, 1, 1),
            'monthly',
            date(2026, 1, 1),
            date(2026, 12, 31),
            until=date(2026, 3, 15),
        )
    )
    assert got == [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)]
    assert list(
        recurrence.occurrences(date(2026, 5, 5), 'one_off', date(2026, 1, 1), date(2026, 12, 31))
    ) == [date(2026, 5, 5)]
    assert (
        list(
            recurrence.occurrences(
                date(2026, 5, 5), 'one_off', date(2026, 6, 1), date(2026, 12, 31)
            )
        )
        == []
    )


def test_monthly_equivalent() -> None:
    assert recurrence.monthly_equivalent(100_00, 'weekly') == 433_33  # 100 x 52 / 12
    assert recurrence.monthly_equivalent(120_00, 'quarterly') == 40_00
    assert recurrence.monthly_equivalent(1200_00, 'yearly') == 100_00
    assert recurrence.monthly_equivalent(500_00, 'one_off') == 0


# -------------------------------------------------------------------- splits


def S(uid: int, value: int = 0) -> splits.ShareSpec:
    return splits.ShareSpec(uid, value)


def test_equal_split_gives_rounding_to_owner() -> None:
    # 100.01 / 3 leaves 2 cents over: the owner takes the first, then the next in order.
    assert splits.compute(100_01, 'equal', [S(1), S(2), S(3)], owner_id=2) == {
        2: 33_34,
        1: 33_34,
        3: 33_33,
    }


def test_percent_split_sums_exactly() -> None:
    out = splits.compute(333_33, 'percent', [S(1, 3333), S(2, 6667)], owner_id=1)
    assert sum(out.values()) == 333_33
    assert out == {1: 111_10, 2: 222_23}


def test_fixed_split_must_add_up() -> None:
    assert splits.compute(150_00, 'fixed', [S(1, 100_00), S(2, 50_00)], 1) == {1: 100_00, 2: 50_00}
    with pytest.raises(splits.SplitError):
        splits.compute(150_00, 'fixed', [S(1, 100_00), S(2, 40_00)], 1)


def test_percent_must_total_100_and_owner_must_hold_a_share() -> None:
    with pytest.raises(splits.SplitError):
        splits.compute(100_00, 'percent', [S(1, 5000), S(2, 4000)], 1)
    with pytest.raises(splits.SplitError):
        splits.compute(100_00, 'equal', [S(2)], 1)
    with pytest.raises(splits.SplitError):
        splits.compute(100_00, 'equal', [S(1), S(1)], 1)


# ------------------------------------------------------------------- csv/dedup

ANZ = """01/09/2026,"-4.50","EFTPOS COFFEE CO"
01/09/2026,"-4.50","EFTPOS COFFEE CO"
02/09/2026,"2500.00","PAY/SALARY ACME"
03/09/2026,"-120.35","ORIGIN ENERGY BPAY"
"""


def test_anz_is_detected_and_parsed() -> None:
    rows = csvimport.read_rows(ANZ)
    assert csvimport.is_anz(rows)
    parsed = csvimport.parse(rows, csvimport.ANZ)
    assert not parsed.errors
    assert parsed.rows[2].amount_cents == 2500_00
    assert parsed.rows[3].date == date(2026, 9, 3)


def test_headered_file_is_not_anz() -> None:
    rows = csvimport.read_rows('Date,Amount,Description\n01/09/2026,-4.50,Coffee\n')
    assert not csvimport.is_anz(rows)


def test_identical_rows_in_one_file_are_both_kept() -> None:
    rows = csvimport.parse(csvimport.read_rows(ANZ), csvimport.ANZ).rows
    hashes = csvimport.dedup_hashes(7, rows)
    assert len(set(hashes)) == 4


def test_overlapping_exports_produce_the_same_hashes() -> None:
    first = csvimport.parse(csvimport.read_rows(ANZ), csvimport.ANZ).rows
    overlap = csvimport.parse(
        csvimport.read_rows(ANZ + '04/09/2026,"-9.99","NETFLIX"\n'), csvimport.ANZ
    ).rows
    a = set(csvimport.dedup_hashes(7, first))
    b = csvimport.dedup_hashes(7, overlap)
    assert len([h for h in b if h not in a]) == 1


def test_hash_depends_on_account() -> None:
    rows = csvimport.parse(csvimport.read_rows(ANZ), csvimport.ANZ).rows
    assert set(csvimport.dedup_hashes(1, rows)).isdisjoint(csvimport.dedup_hashes(2, rows))


def test_mapping_with_debit_and_credit_columns() -> None:
    text = (
        'Date,Details,Debit,Credit\n2026-09-01,Coffee,4.50,\n2026-09-02,Pay,,2500.00\nbad,row,,\n'
    )
    m = csvimport.Mapping(
        has_header=True,
        date_col=0,
        date_format='YYYY-MM-DD',
        amount_col=None,
        debit_col=2,
        credit_col=3,
        description_cols=(1,),
    )
    parsed = csvimport.parse(csvimport.read_rows(text), m)
    assert [r.amount_cents for r in parsed.rows] == [-450, 2500_00]
    assert len(parsed.errors) == 1


def test_csv_formula_injection_guard() -> None:
    assert csvimport.csv_safe('=HYPERLINK("x")') == '\'=HYPERLINK("x")'
    assert csvimport.csv_safe('-12.00') == "'-12.00"
    assert csvimport.csv_safe('Coffee') == 'Coffee'


# --------------------------------------------------------------------- rules


def test_rules_priority_and_regex() -> None:
    m = Matcher(
        [
            RuleSpec(1, 'woolworths', False, 10, 100),
            RuleSpec(2, r'^WOOL.*PETROL', True, 20, 200),
        ]
    )
    assert m.category_for('WOOLWORTHS PETROL 123') == 20
    assert m.category_for('Woolworths Metro') == 10
    assert m.category_for('Coles') is None


def test_bad_regex_is_refused() -> None:
    with pytest.raises(RuleError):
        validate('([', True)
    with pytest.raises(RuleError):
        validate('   ', False)


# ------------------------------------------------------------------ matching


def occ(
    bill_id: int = 1,
    due: date = date(2026, 9, 15),
    expected: int = 100_00,
    account: int | None = 1,
    hint: str = '',
) -> OpenOccurrence:
    return OpenOccurrence(bill_id, due, expected, account, 5, 3, hint)


def txn(i: int, d: date, amount: int, desc: str = 'BPAY', account: int = 1) -> Candidate:
    return Candidate(i, account, d, amount, desc)


def test_match_within_tolerance_and_window() -> None:
    assert matches(occ(), txn(1, date(2026, 9, 17), -104_00))
    assert not matches(occ(), txn(1, date(2026, 9, 19), -100_00))  # 4 days late
    assert not matches(occ(), txn(1, date(2026, 9, 15), -106_00))  # over 5 %
    assert not matches(occ(), txn(1, date(2026, 9, 15), 100_00))  # money in
    assert not matches(occ(), txn(1, date(2026, 9, 15), -100_00, account=2))


def test_hint_must_appear() -> None:
    assert matches(occ(hint='origin'), txn(1, date(2026, 9, 15), -100_00, 'ORIGIN ENERGY'))
    assert not matches(occ(hint='origin'), txn(1, date(2026, 9, 15), -100_00, 'AGL'))


def test_assignment_is_one_to_one_and_prefers_nearest() -> None:
    occs = [occ(due=date(2026, 9, 1)), occ(due=date(2026, 9, 3))]
    pairs = assign(occs, [txn(1, date(2026, 9, 3), -100_00), txn(2, date(2026, 9, 4), -100_00)])
    assert [(o.due_date, t.transaction_id) for o, t in pairs] == [
        (date(2026, 9, 3), 1),
        (date(2026, 9, 1), 2),
    ]
