"""Forecast, scenarios and planned transfers."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from fiscus.clock import utcnow_naive
from fiscus.domain.forecast import SCENARIO_KINDS, ForecastResult, trend
from fiscus.forms import (
    FormError,
    choice,
    ends_after,
    int_or,
    money,
    opt_date,
    rate,
    req_date,
    text,
)
from fiscus.models import (
    FREQUENCIES,
    Account,
    Loan,
    PaySchedule,
    RecurringTransfer,
    Scenario,
    ScenarioAdjustment,
    User,
)
from fiscus.services import forecasting, goals, ledger
from fiscus.services.access import get_owned, maybe_owned, owned
from fiscus.services.users import settings_for
from fiscus.web import ctx, current_user, get_db, redirect, render

router = APIRouter()


def chart_payload(
    base: ForecastResult,
    names: dict[int, str],
    scenario: ForecastResult | None,
    scenario_name: str,
    free: list[int] | None = None,
    lines: dict[str, bool] | None = None,
) -> str:
    # Drawn only when it differs from all accounts.
    if free == base.combined:
        free = None
    data: dict[str, Any] = {
        'dates': [d.isoformat() for d in base.dates],
        'combined': base.combined,
        'trend': base.trend.line,
        'free': free,
        'free_trend': trend(free).line if free else None,
        # The account behind a low-balance warning is drawn even when the rest
        # are hidden, so the warning shows on the chart.
        'accounts': [
            {
                'key': f'account:{a}',
                'name': names.get(a, str(a)),
                'values': v,
                'low': bool(base.low_balance and base.low_balance['account_id'] == a),
            }
            for a, v in base.accounts.items()
        ],
        'low': base.low_balance['date'].isoformat() if base.low_balance else None,
        # Lines turned off or on in the legend, kept until turned back (D50).
        'lines': lines or {},
    }
    if scenario is not None:
        data['scenario'] = {
            'name': scenario_name,
            'combined': scenario.combined,
            'trend': scenario.trend.line,
        }
    # Goes in a data attribute; Jinja's autoescape quotes it.
    return json.dumps(data, separators=(',', ':'))


@router.get('/forecast')
def forecast_page(
    request: Request,
    months: int = 0,
    scenario_id: int = 0,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    c = ctx(request)
    prefs = settings_for(db, user)
    months = months or prefs.forecast_months
    months = max(1, min(24, months))
    base = forecasting.base(db, c.clock, user, months)
    sc = maybe_owned(db, Scenario, scenario_id, user)
    sc_result = forecasting.scenario(db, c.clock, user, sc, months) if sc else None
    accts = ledger.accounts(db, user)
    names = {a.id: a.name for a in accts}
    # Free to spend, day by day, as the budget page has it today (D51): the
    # accounts it counts, less what goals keep aside today (the same amount
    # off every day, D46) and what is still budgeted for the rest of each
    # budget month.
    counted = [a.id for a in accts if not a.is_set_aside and a.id in base.accounts]
    goals_held = goals.held_outside(db, user, [a for a in accts if not a.is_set_aside])
    to_spend = forecasting.still_to_spend(db, user, c.clock.today(), len(base.dates) - 1)
    free = [
        sum(base.accounts[a][i] for a in counted) - goals_held - to_spend[i]
        for i in range(len(base.dates))
    ]
    month_end = base.month_end
    sc_month_end = {m['date']: m['combined'] for m in sc_result.month_end} if sc_result else {}
    return render(
        request,
        'forecast/index.html',
        {
            'base': base,
            'months': months,
            'names': names,
            'month_end': month_end,
            'sc': sc,
            'sc_result': sc_result,
            'sc_month_end': sc_month_end,
            'scenarios': db.execute(owned(Scenario, user).order_by(Scenario.name)).scalars().all(),
            'goals_held': goals_held,
            'to_spend': to_spend[0],
            'free_on': dict(zip(base.dates, free, strict=True)) if free != base.combined else {},
            'chart': chart_payload(
                base,
                names,
                sc_result,
                sc.name if sc else '',
                free,
                forecasting.chart_lines(db, user),
            ),
            'estimates': forecasting.estimates(db, user, c.clock.today()),
            'transfers': db.execute(owned(RecurringTransfer, user)).scalars().all(),
            'accounts': accts,
            'freqs': FREQUENCIES,
            'prefs': prefs,
        },
    )


@router.post('/forecast/lines')
def save_chart_line(
    key: str = Form(...),
    shown: bool = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    """A line turned off or on in the chart's legend (D50). The chart's script
    sends it as the person clicks, so it answers with nothing."""
    if key.startswith('account:'):
        acct = get_owned(db, Account, int_or(key.removeprefix('account:')), user)
        key = f'account:{acct.id}'
    elif key not in forecasting.CHART_LINES:
        raise HTTPException(status_code=400)
    forecasting.set_chart_line(db, user, key, shown)
    return Response(status_code=204)


@router.post('/transfers')
def add_transfer(
    request: Request,
    name: str = Form(...),
    from_account_id: int = Form(...),
    to_account_id: int = Form(...),
    amount: str = Form(...),
    frequency: str = Form(...),
    anchor_date: str = Form(...),
    end_date: str = Form(''),
    is_tax_set_aside: bool = Form(False),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    src = get_owned(db, Account, from_account_id, user)
    dst = get_owned(db, Account, to_account_id, user)
    try:
        if src.id == dst.id:
            raise FormError('Choose two different accounts')
        first = req_date(anchor_date, 'First transfer')
        db.add(
            RecurringTransfer(
                user_id=user.id,
                name=text(name, 'Name', 80),
                from_account_id=src.id,
                to_account_id=dst.id,
                amount_cents=money(amount, 'Amount', allow_negative=False),
                frequency=choice(frequency, FREQUENCIES, 'Frequency'),
                anchor_date=first,
                end_date=ends_after(first, opt_date(end_date, 'End date')),
                is_tax_set_aside=is_tax_set_aside,
            )
        )
    except FormError as exc:
        return redirect('/forecast', flash=str(exc), request=request)
    return redirect('/forecast', flash='Planned transfer saved', request=request)


@router.post('/transfers/{transfer_id}/delete')
def delete_transfer(
    transfer_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    db.delete(get_owned(db, RecurringTransfer, transfer_id, user))
    return redirect('/forecast', flash='Planned transfer removed', request=request)


@router.get('/scenarios')
def scenarios(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    return render(
        request,
        'forecast/scenarios.html',
        {
            'scenarios': db.execute(owned(Scenario, user).order_by(Scenario.name)).scalars().all(),
        },
    )


@router.post('/scenarios')
def create_scenario(
    request: Request,
    name: str = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    try:
        sc = Scenario(
            user_id=user.id,
            name=text(name, 'Name', 80),
            created_at=utcnow_naive(ctx(request).clock),
        )
    except FormError as exc:
        return redirect('/scenarios', flash=str(exc), request=request)
    db.add(sc)
    db.flush()
    return redirect(f'/scenarios/{sc.id}', request=request)


@router.get('/scenarios/{scenario_id}')
def show_scenario(
    scenario_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    sc = get_owned(db, Scenario, scenario_id, user)
    return render(
        request,
        'forecast/scenario.html',
        {
            'sc': sc,
            'kinds': SCENARIO_KINDS,
            'accounts': ledger.accounts(db, user),
            'loans': db.execute(owned(Loan, user)).scalars().all(),
            'pays': db.execute(owned(PaySchedule, user)).scalars().all(),
            'freqs': FREQUENCIES,
            'names': {a.id: a.name for a in ledger.accounts(db, user, include_archived=True)},
        },
    )


@router.post('/scenarios/{scenario_id}/delete')
def delete_scenario(
    scenario_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    db.delete(get_owned(db, Scenario, scenario_id, user))
    return redirect('/scenarios', flash='Scenario deleted', request=request)


@router.post('/scenarios/{scenario_id}/adjustments')
async def add_adjustment(
    scenario_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    sc = get_owned(db, Scenario, scenario_id, user)
    form = await request.form()

    def f(key: str) -> str:
        v = form.get(key)
        return v.strip() if isinstance(v, str) else ''

    kind = f('kind')
    try:
        params: dict[str, Any]
        if kind == 'one_off':
            acct = get_owned(db, Account, int_or(f('account_id')), user)
            amount = money(f('amount'), 'Amount')
            if f('direction') != 'in':
                amount = -abs(amount)
            params = {
                'date': req_date(f('date'), 'Date').isoformat(),
                'account_id': acct.id,
                'amount_cents': amount,
                'label': f('label')[:80] or 'One-off',
            }
        elif kind == 'income_change':
            ps = maybe_owned(db, PaySchedule, int_or(f('pay_schedule_id')), user)
            params = {
                'from_date': req_date(f('from_date'), 'From').isoformat(),
                'pay_schedule_id': ps.id if ps else None,
                'new_net_cents': money(f('new_net'), 'New net pay', allow_negative=False)
                if f('new_net')
                else None,
                'delta_cents': money(f('delta'), 'Change') if f('delta') else 0,
            }
        elif kind == 'rate_change':
            loan = get_owned(db, Loan, int_or(f('loan_id')), user)
            params = {
                'loan_id': loan.id,
                'from_date': req_date(f('from_date'), 'From').isoformat(),
                'rate': rate(f('rate'), 'Rate'),
            }
        elif kind in ('new_bill', 'new_income'):
            acct = get_owned(db, Account, int_or(f('account_id')), user)
            start = req_date(f('start_date'), 'Start')
            end = ends_after(start, opt_date(f('end_date'), 'End'))
            params = {
                'name': f('name')[:80] or ('New bill' if kind == 'new_bill' else 'New income'),
                'account_id': acct.id,
                'amount_cents': money(f('amount'), 'Amount', allow_negative=False),
                'frequency': choice(f('frequency'), FREQUENCIES, 'Frequency'),
                'start_date': start.isoformat(),
                'end_date': end.isoformat() if end else None,
            }
        elif kind == 'extra_repayment':
            loan = get_owned(db, Loan, int_or(f('loan_id')), user)
            from_acct = maybe_owned(db, Account, int_or(f('account_id')), user)
            params = {
                'loan_id': loan.id,
                'date': req_date(f('date'), 'Date').isoformat(),
                'amount_cents': money(f('amount'), 'Amount', allow_negative=False),
                'frequency': choice(f('frequency') or 'one_off', FREQUENCIES, 'Frequency'),
                'account_id': from_acct.id if from_acct else None,
            }
        else:
            raise FormError('Choose what kind of change to make')
    except FormError as exc:
        return redirect(f'/scenarios/{sc.id}', flash=str(exc), request=request)
    db.add(ScenarioAdjustment(user_id=user.id, scenario_id=sc.id, kind=kind, params=params))
    return redirect(f'/scenarios/{sc.id}', flash='Change added', request=request)


@router.post('/scenarios/{scenario_id}/adjustments/{adj_id}/delete')
def delete_adjustment(
    scenario_id: int,
    adj_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    sc = get_owned(db, Scenario, scenario_id, user)
    adj = get_owned(db, ScenarioAdjustment, adj_id, user)
    if adj.scenario_id == sc.id:
        db.delete(adj)
    return redirect(f'/scenarios/{sc.id}', flash='Change removed', request=request)
