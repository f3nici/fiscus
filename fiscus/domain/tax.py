"""Australian resident income tax estimate.

Rates come from config/tax/<year>.yaml, one file per financial year (1 July to
30 June). Everything here is an estimate and every screen says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import yaml

from fiscus.money import round_div

CONFIG_DIR = Path(__file__).resolve().parents[2] / 'config' / 'tax'


class TaxConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaxYear:
    label: str
    start: date
    end: date
    brackets: tuple[tuple[int, Decimal], ...]  # (over, rate), dollars
    medicare_rate: Decimal
    medicare_lower: int
    medicare_upper: int
    medicare_phase_in: Decimal
    lito_max: int
    lito_full_to: int
    lito_t1_to: int
    lito_t1_rate: Decimal
    lito_t2_to: int
    lito_t2_rate: Decimal
    verified: str


def fy_label(day: date) -> str:
    start = day.year if day.month >= 7 else day.year - 1
    return f'{start}-{str(start + 1)[-2:]}'


def fy_bounds(day: date) -> tuple[date, date]:
    start = day.year if day.month >= 7 else day.year - 1
    return date(start, 7, 1), date(start + 1, 6, 30)


def _dec(v: Any) -> Decimal:
    return Decimal(str(v))


def load_year(label: str, config_dir: Path | None = None) -> TaxYear:
    path = (config_dir or CONFIG_DIR) / f'{label}.yaml'
    if not path.exists():
        raise TaxConfigError(f'No tax table for {label}. Add config/tax/{label}.yaml')
    raw = yaml.safe_load(path.read_text())
    try:
        brackets = tuple(sorted((int(b['over']), _dec(b['rate'])) for b in raw['brackets']))
        m, lito = raw['medicare'], raw['lito']
        return TaxYear(
            label=str(raw['financial_year']),
            start=_as_date(raw['start']),
            end=_as_date(raw['end']),
            brackets=brackets,
            medicare_rate=_dec(m['rate']),
            medicare_lower=int(m['lower']),
            medicare_upper=int(m['upper']),
            medicare_phase_in=_dec(m['phase_in_rate']),
            lito_max=int(lito['max']),
            lito_full_to=int(lito['full_to']),
            lito_t1_to=int(lito['first_taper_to']),
            lito_t1_rate=_dec(lito['first_taper_rate']),
            lito_t2_to=int(lito['second_taper_to']),
            lito_t2_rate=_dec(lito['second_taper_rate']),
            verified=str(raw.get('verified', '')),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TaxConfigError(f'{path.name} is malformed: {exc}') from exc


def _as_date(v: Any) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def _cents(dollars: Decimal) -> int:
    return int((dollars * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def income_tax(year: TaxYear, taxable_cents: int) -> int:
    """Tax on taxable income before offsets and levy. ATO uses whole dollars."""
    income = Decimal(max(0, taxable_cents) // 100)
    tax = Decimal(0)
    for i, (over, rate) in enumerate(year.brackets):
        upper = Decimal(year.brackets[i + 1][0]) if i + 1 < len(year.brackets) else None
        if income <= over:
            break
        top = income if upper is None else min(income, upper)
        tax += (top - Decimal(over)) * rate
    return _cents(tax)


def lito(year: TaxYear, taxable_cents: int) -> int:
    income = Decimal(max(0, taxable_cents) // 100)
    offset = Decimal(year.lito_max)
    if income > year.lito_full_to:
        offset -= (min(income, Decimal(year.lito_t1_to)) - year.lito_full_to) * year.lito_t1_rate
    if income > year.lito_t1_to:
        offset -= (min(income, Decimal(year.lito_t2_to)) - year.lito_t1_to) * year.lito_t2_rate
    return max(0, _cents(offset))


def medicare(year: TaxYear, taxable_cents: int) -> int:
    income = Decimal(max(0, taxable_cents) // 100)
    if income <= year.medicare_lower:
        return 0
    full = income * year.medicare_rate
    if income <= year.medicare_upper:
        return _cents(min(full, (income - year.medicare_lower) * year.medicare_phase_in))
    return _cents(full)


@dataclass(frozen=True)
class TaxEstimate:
    taxable_cents: int
    income_tax_cents: int
    medicare_cents: int
    total_cents: int


def estimate(year: TaxYear, taxable_cents: int) -> TaxEstimate:
    """Income tax plus Medicare. The low income tax offset is left out: on a part
    year's income it showed an offset the full year would not get (D31)."""
    it = income_tax(year, taxable_cents)
    mc = medicare(year, taxable_cents)
    return TaxEstimate(taxable_cents, it, mc, it + mc)


@dataclass(frozen=True)
class Position:
    income_cents: int
    withheld_cents: int
    deductions_cents: int
    total: TaxEstimate

    @property
    def owing_cents(self) -> int:
        """Estimated tax still to pay: total on taxable income less what was withheld."""
        return self.total.total_cents - self.withheld_cents


def position(year: TaxYear, income: int, withheld: int, deductions: int) -> Position:
    taxable = max(0, income - deductions)
    return Position(income, withheld, deductions, estimate(year, taxable))


def share_of(year: Position, income: int, withheld: int) -> Position:
    """The part of a whole year's position that `income` so far accounts for:
    that share of the deductions and of the year's tax.

    Tax worked on the income so far alone would give it the whole tax-free
    threshold and the low brackets, and show early in the year a refund the
    full year takes back.
    """
    if year.income_cents <= 0:
        return Position(income, withheld, 0, TaxEstimate(0, 0, 0, 0))

    def part(cents: int) -> int:
        return round_div(cents * income, year.income_cents)

    t = year.total
    it, mc = part(t.income_tax_cents), part(t.medicare_cents)
    return Position(
        income,
        withheld,
        part(year.deductions_cents),
        TaxEstimate(part(t.taxable_cents), it, mc, it + mc),
    )


@dataclass(frozen=True)
class PayFigures:
    """What a schedule says about a pay beyond its net."""

    gross_cents: int | None
    withheld_cents: int | None
    usual_net_cents: int  # the net those figures go with
    varies: bool = False


def gross_and_tax(
    net: int, figures: PayFigures, slips: tuple[int, int, int] | None
) -> tuple[int, int, bool]:
    """Gross and tax withheld for an expected pay of `net`, and whether
    anything said so.

    The schedule's own gross and tax first (scaled to the net when the pay
    varies); a gross alone means gross less net was withheld, a tax alone that
    gross is net plus it. Then in proportion to that job's own payslips
    (gross, withheld, net). With nothing to go on, nothing was withheld: the
    safe guess for a tax estimate, and right for contract income.
    """
    g, w = figures.gross_cents, figures.withheld_cents
    if figures.varies and figures.usual_net_cents:
        if g is not None:
            g = round_div(g * net, figures.usual_net_cents)
        if w is not None:
            w = round_div(w * net, figures.usual_net_cents)
    if g is not None:
        return g, (w if w is not None else max(0, g - net)), True
    if w is not None:
        return net + w, w, True
    if slips is not None and slips[2] > 0:
        gross, withheld, slip_net = slips
        g = round_div(net * gross, slip_net)
        return g, (round_div(g * withheld, gross) if gross else 0), True
    return net, 0, False
