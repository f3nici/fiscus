"""Loan maths, the forecast engine and tax, against hand-calculated cases."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from fiscus.domain import loan as L
from fiscus.domain import tax
from fiscus.domain.forecast import Flow, ForecastInput, apply_scenario, run

# ---------------------------------------------------------------------- loan


def test_standard_repayment() -> None:
    # $500,000 at 6 % over 30 years monthly: the textbook $2,997.75.
    assert L.repayment(500_000_00, Decimal('6'), 360, 12) == 2_997_75
    # Zero rate is principal over periods.
    assert L.repayment(120_000_00, Decimal('0'), 120, 12) == 1_000_00
    # $300,000 at 5.5 % over 25 years fortnightly: r = 0.055 / 26, n = 650.
    assert L.repayment(300_000_00, Decimal('5.5'), 650, 26) == 849_79


def spec(**kw: object) -> L.LoanSpec:
    base: dict[str, object] = dict(
        id=1,
        name='Home',
        principal_cents=500_000_00,
        start_date=date(2026, 1, 15),
        term_months=360,
        frequency='monthly',
        balance_cents=500_000_00,
        balance_date=date(2026, 1, 15),
        rates=[(date(2026, 1, 15), Decimal('6'))],
        account_id=10,
        offset_account_ids=(11,),
    )
    base.update(kw)
    return L.LoanSpec(**base)  # type: ignore[arg-type]


def test_interest_is_daily_on_balance_less_offset_and_charged_monthly() -> None:
    s = spec()
    sim = L.LoanSim(s)
    day = s.start_date
    charged_on = []
    # 31 days from 16 Jan to 15 Feb with a constant $100,000 offset.
    while day < date(2026, 2, 15):
        day += timedelta(days=1)
        sim.repayments_due(day)
        before = sim.interest_charged
        sim.end_of_day(day, 100_000_00)
        if sim.interest_charged != before:
            charged_on.append(day)
    # 30 days on $400,000, then the 15 Feb repayment lands before that day's
    # interest: 400,000 x 6 % / 365 x 30 + 397,002.25 x 6 % / 365 = $2,037.86
    assert sim.interest_charged == 2_037_86
    assert charged_on == [date(2026, 2, 15)]


def test_offset_larger_than_loan_means_no_interest() -> None:
    sim = L.LoanSim(spec(balance_cents=50_000_00))
    for i in range(1, 40):
        sim.end_of_day(date(2026, 1, 15) + timedelta(days=i), 80_000_00)
    assert sim.interest_charged == 0


def test_repayment_on_anniversary_reduces_balance() -> None:
    sim = L.LoanSim(spec())
    assert sim.repayments_due(date(2026, 2, 14)) == []
    assert sim.repayments_due(date(2026, 2, 15)) == [(2_997_75, 10)]
    assert sim.balance == 500_000_00 - 2_997_75


def test_rate_change_reprices_over_the_remaining_term() -> None:
    s = spec(rates=[(date(2026, 1, 15), Decimal('6')), (date(2026, 3, 1), Decimal('7'))])
    sim = L.LoanSim(s)
    sim.repayments_due(date(2026, 3, 1))
    remaining = s.periods_between(date(2026, 3, 1))
    # 15 Mar 2026 to 15 Jan 2056 inclusive.
    assert remaining == 359
    assert sim.payment == L.repayment(sim.balance, Decimal('7'), 359, 12)
    assert sim.payment > 2_997_75


def test_payoff_without_offset_runs_the_full_term_and_offset_shortens_it() -> None:
    s = spec()
    without = L.project_payoff(s, s.start_date, lambda _d: 0)
    with_offset = L.project_payoff(s, s.start_date, lambda _d: 100_000_00)
    assert without.paid_off is not None and with_offset.paid_off is not None
    # Daily accrual makes the last payment land within a month of the 30-year term.
    assert abs((without.paid_off - date(2056, 1, 15)).days) <= 31
    assert with_offset.paid_off < without.paid_off - timedelta(days=365 * 7)
    assert with_offset.total_interest < without.total_interest


def test_interest_saved_hand_calculated() -> None:
    days = {date(2026, 9, d): 50_000_00 for d in range(1, 31)}
    # $50,000 x 6 % / 365 x 30 = $246.58
    assert L.interest_saved(500_000_00, [(date(2026, 1, 1), Decimal('6'))], days) == 246_58


# ------------------------------------------------------------------ forecast


def base_input(**kw: object) -> ForecastInput:
    inp = ForecastInput(
        today=date(2026, 10, 1),
        days=30,
        balances={1: 1_000_00, 2: 5_000_00},
        account_names={1: 'Everyday', 2: 'Savings'},
        account_types={1: 'transaction', 2: 'savings'},
    )
    for k, v in kw.items():
        setattr(inp, k, v)
    return inp


def test_forecast_hand_calculated() -> None:
    inp = base_input(
        flows=[
            Flow(date(2026, 10, 2), 1, 2_000_00, 'pay', 'Salary'),
            Flow(date(2026, 10, 5), 1, -1_500_00, 'bill', 'Rent'),
            Flow(date(2026, 10, 5), 2, 100_00, 'transfer', 'Save'),
            Flow(date(2026, 10, 5), 1, -100_00, 'transfer', 'Save'),
        ],
        daily={1: Decimal(-10_00)},
    )
    r = run(inp)
    assert r.dates[0] == date(2026, 10, 1) and len(r.dates) == 31
    assert r.accounts[1][0] == 1_000_00
    # 2 Oct: 1000 + 2000 - 10 = 2990
    assert r.accounts[1][1] == 2_990_00
    # 5 Oct: 2990 - 10 - 10 - 1500 - 100 - 10 = 1360
    assert r.accounts[1][4] == 1_360_00
    assert r.accounts[2][4] == 5_100_00
    # 31 Oct: 1000 + 2000 - 1600 - 300 = 1100
    assert r.accounts[1][-1] == 1_100_00
    assert r.combined[-1] == 1_100_00 + 5_100_00
    assert [m['label'] for m in r.markers] == ['Salary', 'Rent']
    assert r.month_end[0]['date'] == date(2026, 10, 31)


def test_fractional_daily_spending_carries() -> None:
    r = run(base_input(daily={1: Decimal('-33.333')}))
    # 30 days at 33.333 cents is 999.99 cents: 1000 after rounding the carry.
    assert 1_000_00 - r.accounts[1][-1] in (999, 1000)


def test_low_balance_warning_finds_first_day() -> None:
    inp = base_input(
        flows=[Flow(date(2026, 10, 10), 1, -1_200_00, 'bill', 'Car rego')], low_balance_cents=0
    )
    r = run(inp)
    assert r.low_balance is not None
    assert r.low_balance['date'] == date(2026, 10, 10)
    assert r.low_balance['account_id'] == 1
    assert r.low_balance['balance'] == -200_00


def test_credit_card_is_not_a_low_balance_warning() -> None:
    inp = base_input(
        balances={1: 1_000_00, 3: -500_00},
        account_names={1: 'E', 3: 'Card'},
        account_types={1: 'transaction', 3: 'credit_card'},
    )
    assert run(inp).low_balance is None


def test_offset_interest_in_the_forecast() -> None:
    loan = spec(
        balance_cents=400_000_00,
        balance_date=date(2026, 10, 1),
        start_date=date(2026, 1, 15),
        offset_account_ids=(2,),
        account_id=1,
    )
    r = run(base_input(loans=[loan], days=45))
    # Repayment out of account 1 on 15 Oct.
    idx = (date(2026, 10, 15) - date(2026, 10, 1)).days
    assert r.accounts[1][idx] == 1_000_00 - L.LoanSim(loan).payment
    # Only 2..15 Oct accrues in the forecast, charged on 15 Oct: 13 days on
    # (400,000 - 5,000 offset), then the repayment lands before the 15th's
    # interest: 13 x 395,000 x 6 % / 365 + (397,002.25 - 5,000) x 6 % / 365 = $908.55
    assert r.loans[1][idx] == 400_000_00 - L.LoanSim(loan).payment + 908_55


def test_scenarios_do_not_touch_the_base_input() -> None:
    inp = base_input(flows=[Flow(date(2026, 10, 2), 1, 2_000_00, 'pay', 'Salary', 1)])
    before = run(inp)
    changed = apply_scenario(
        inp,
        [
            (
                'one_off',
                {'date': '2026-10-03', 'account_id': 1, 'amount_cents': -500_00, 'label': 'Tyres'},
            ),
            (
                'income_change',
                {'from_date': '2026-10-01', 'pay_schedule_id': 1, 'new_net_cents': 2_500_00},
            ),
            (
                'new_bill',
                {
                    'name': 'Gym',
                    'amount_cents': 20_00,
                    'frequency': 'weekly',
                    'start_date': '2026-10-05',
                    'account_id': 1,
                },
            ),
        ],
    )
    after = run(changed)
    assert run(inp).combined == before.combined  # base unchanged
    assert len(inp.flows) == 1
    # +500 pay, -500 tyres, -20 x 4 gym (5, 12, 19, 26 Oct)
    assert after.combined[-1] - before.combined[-1] == 500_00 - 500_00 - 80_00


def test_scenario_rate_change_and_extra_repayment() -> None:
    loan = spec(
        balance_cents=400_000_00,
        balance_date=date(2026, 10, 1),
        offset_account_ids=(),
        account_id=1,
    )
    inp = base_input(loans=[loan], days=60)
    base = run(inp)
    higher = run(
        apply_scenario(
            inp, [('rate_change', {'loan_id': 1, 'from_date': '2026-10-01', 'rate': '7'})]
        )
    )
    assert higher.loans[1][-1] > base.loans[1][-1]
    extra = run(
        apply_scenario(
            inp,
            [
                (
                    'extra_repayment',
                    {
                        'loan_id': 1,
                        'date': '2026-10-20',
                        'amount_cents': 10_000_00,
                        'frequency': 'one_off',
                        'account_id': 2,
                    },
                )
            ],
        )
    )
    assert extra.loans[1][-1] < base.loans[1][-1] - 9_000_00
    assert extra.accounts[2][-1] == base.accounts[2][-1] - 10_000_00


# ------------------------------------------------------------------------ tax


@pytest.fixture
def fy() -> tax.TaxYear:
    return tax.load_year('2026-27')


def test_fy_bounds() -> None:
    assert tax.fy_label(date(2026, 10, 1)) == '2026-27'
    assert tax.fy_label(date(2027, 6, 30)) == '2026-27'
    assert tax.fy_label(date(2027, 7, 1)) == '2027-28'
    assert tax.fy_bounds(date(2026, 10, 1)) == (date(2026, 7, 1), date(2027, 6, 30))


@pytest.mark.parametrize(
    ('income', 'expected'),
    [
        (18_200, 0),
        (45_000, 4_020_00),  # (45,000 - 18,200) x 15 %
        (100_000, 20_520_00),  # 4,020 + 55,000 x 30 %
        (135_000, 31_020_00),
        (190_000, 51_370_00),  # 31,020 + 55,000 x 37 %
        (250_000, 78_370_00),  # 51,370 + 60,000 x 45 %
    ],
)
def test_income_tax_2026_27(fy: tax.TaxYear, income: int, expected: int) -> None:
    assert tax.income_tax(fy, income * 100) == expected


def test_lito_and_medicare(fy: tax.TaxYear) -> None:
    assert tax.lito(fy, 30_000_00) == 700_00
    assert tax.lito(fy, 40_000_00) == 575_00  # 700 - 2,500 x 5 %
    assert tax.lito(fy, 50_000_00) == 250_00  # 325 - 5,000 x 1.5 %
    assert tax.lito(fy, 70_000_00) == 0
    assert tax.medicare(fy, 20_000_00) == 0
    assert tax.medicare(fy, 30_000_00) == 277_80  # (30,000 - 27,222) x 10 %
    assert tax.medicare(fy, 100_000_00) == 2_000_00


def test_position_owing_on_business_profit(fy: tax.TaxYear) -> None:
    # Salary 80,000 with tax withheld as if it were the only income, plus 20,000 profit.
    salary_only = tax.estimate(fy, 80_000_00)
    pos = tax.position(fy, 80_000_00, salary_only.total_cents, 30_000_00, 10_000_00)
    assert pos.business_profit_cents == 20_000_00
    # The extra 20,000 is taxed at 30 % + 2 % Medicare = 6,400.
    assert pos.owing_cents == 6_400_00


def test_missing_year_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(tax.TaxConfigError):
        tax.load_year('2031-32', tmp_path)
