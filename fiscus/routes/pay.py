"""Pay schedules and payslips."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from fiscus.domain import recurrence
from fiscus.domain.tax import fy_bounds
from fiscus.forms import FormError, choice, money, opt_date, opt_money, req_date, text
from fiscus.models import PAY_FREQUENCIES, Account, PaySchedule, Payslip, User
from fiscus.services import ledger
from fiscus.services.access import get_owned, maybe_owned, owned
from fiscus.web import ctx, current_user, get_db, redirect, render

router = APIRouter()


@router.get('/pay')
def pay_page(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    c = ctx(request)
    today = c.clock.today()
    schedules = db.execute(owned(PaySchedule, user).order_by(PaySchedule.name)).scalars().all()
    nxt = {s.id: recurrence.next_on_or_after(s.anchor_date, s.frequency, today) for s in schedules}
    for s in schedules:
        d = nxt[s.id]
        if d is not None and s.end_date and d > s.end_date:
            nxt[s.id] = None
    fy_start, _ = fy_bounds(today)
    slips = db.execute(owned(Payslip, user).order_by(Payslip.date.desc()).limit(60)).scalars().all()
    ytd = [p for p in slips if p.date >= fy_start]
    return render(
        request,
        'pay/index.html',
        {
            'schedules': schedules,
            'next': nxt,
            'slips': slips,
            'freqs': PAY_FREQUENCIES,
            'accounts': ledger.accounts(db, user),
            'ytd': {
                'gross': sum(p.gross_cents for p in ytd),
                'tax': sum(p.tax_withheld_cents for p in ytd),
                'net': sum(p.net_cents for p in ytd),
            },
            'edit': maybe_owned(
                db, PaySchedule, int(request.query_params.get('edit', '0') or 0), user
            ),
        },
    )


@router.post('/pay/schedules')
@router.post('/pay/schedules/{schedule_id}')
def save_schedule(
    request: Request,
    schedule_id: int | None = None,
    name: str = Form(...),
    frequency: str = Form(...),
    anchor_date: str = Form(...),
    net: str = Form(...),
    gross: str = Form(''),
    tax_withheld: str = Form(''),
    end_date: str = Form(''),
    account_id: int = Form(0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    ps = get_owned(db, PaySchedule, schedule_id, user) if schedule_id else None
    try:
        values = dict(
            name=text(name, 'Name', 80),
            frequency=choice(frequency, PAY_FREQUENCIES, 'Frequency'),
            anchor_date=req_date(anchor_date, 'A payday'),
            net_cents=money(net, 'Net pay', allow_negative=False),
            gross_cents=opt_money(gross, 'Gross pay'),
            tax_withheld_cents=opt_money(tax_withheld, 'Tax withheld'),
            end_date=opt_date(end_date, 'End date'),
        )
    except FormError as exc:
        return redirect('/pay', flash=str(exc), request=request)
    acct = maybe_owned(db, Account, account_id, user)
    if ps is None:
        ps = PaySchedule(user_id=user.id, **values)
        db.add(ps)
    else:
        for k, v in values.items():
            setattr(ps, k, v)
    ps.account_id = acct.id if acct else None
    return redirect('/pay', flash='Pay schedule saved', request=request)


@router.post('/pay/schedules/{schedule_id}/delete')
def delete_schedule(
    schedule_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    db.delete(get_owned(db, PaySchedule, schedule_id, user))
    return redirect('/pay', flash='Pay schedule deleted', request=request)


@router.post('/payslips')
def add_payslip(
    request: Request,
    date: str = Form(...),
    gross: str = Form(...),
    tax_withheld: str = Form(...),
    net: str = Form(...),
    source: str = Form(''),
    account_id: int = Form(0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    try:
        slip = Payslip(
            user_id=user.id,
            date=req_date(date, 'Date'),
            gross_cents=money(gross, 'Gross', allow_negative=False),
            tax_withheld_cents=money(tax_withheld, 'Tax withheld', allow_negative=False),
            net_cents=money(net, 'Net', allow_negative=False),
            source=source.strip()[:120],
        )
    except FormError as exc:
        return redirect('/pay', flash=str(exc), request=request)
    if slip.tax_withheld_cents > slip.gross_cents:
        return redirect('/pay', flash='Tax withheld is more than gross pay', request=request)
    acct = maybe_owned(db, Account, account_id, user)
    slip.account_id = acct.id if acct else None
    db.add(slip)
    if slip.date > ctx(request).clock.today():
        return redirect('/pay', flash='Payslip saved. It is in your forecast.', request=request)
    return redirect('/pay', flash='Payslip saved', request=request)


@router.post('/payslips/{slip_id}/delete')
def delete_payslip(
    slip_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    db.delete(get_owned(db, Payslip, slip_id, user))
    return redirect('/pay', flash='Payslip deleted', request=request)
