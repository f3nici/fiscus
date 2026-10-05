"""Pay schedules and payslips."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy.orm import Session

from fiscus.domain import payslip as payslip_pdf
from fiscus.domain import recurrence
from fiscus.domain.csvimport import MAX_BYTES, ImportError_
from fiscus.domain.tax import fy_bounds
from fiscus.forms import FormError, choice, money, opt_date, opt_money, req_date, text
from fiscus.models import PAY_FREQUENCIES, Account, PaySchedule, Payslip, User
from fiscus.money import fmt_plain
from fiscus.services import ledger, pay
from fiscus.services.access import get_owned, maybe_owned, owned
from fiscus.web import ctx, current_user, get_db, redirect, render

router = APIRouter()


@router.get('/pay')
def pay_page(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    return _page(request, user, db)


def _page(
    request: Request, user: User, db: Session, read: dict[str, Any] | None = None
) -> Response:
    c = ctx(request)
    today = c.clock.today()
    expected = pay.schedules(db, user, today)
    schedules = [ps for ps, _net in expected]
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
            'expected': {ps.id: net for ps, net in expected},
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
            'read': read,
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
    varies: str = Form(''),
    # No longer on the form (D25 hid transactions); saving from it clears any old text.
    match_text: str = Form(''),
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
            varies=varies == 'on',
            match_text=' '.join(match_text.split())[:120],
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


def _plain(cents: int | None) -> str:
    return '' if cents is None else fmt_plain(cents)


@router.post('/payslips/read')
async def read_payslip(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Fill in the payslip form from a PDF. Nothing is saved until the person adds it."""
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        return redirect('/pay', flash='That file is larger than 10 MB', request=request)
    try:
        slip = await run_in_threadpool(payslip_pdf.read, data)
    except ImportError_ as exc:
        return redirect('/pay', flash=str(exc), request=request)
    notes = slip.notes()
    when = slip.pay_date or slip.period_end
    if when is not None and slip.net_cents is not None:
        dupe = db.execute(
            owned(Payslip, user).where(Payslip.date == when, Payslip.net_cents == slip.net_cents)
        ).first()
        if dupe is not None:
            notes.append('You already have a payslip for this date and net pay.')
    return _page(
        request,
        user,
        db,
        {
            'date': when.isoformat() if when else '',
            'source': slip.employer,
            'gross': _plain(slip.gross_cents),
            'tax_withheld': _plain(slip.tax_withheld_cents),
            'net': _plain(slip.net_cents),
            'file_name': (file.filename or 'the payslip')[:120],
            'notes': notes,
        },
    )


@router.post('/payslips/{slip_id}/delete')
def delete_payslip(
    slip_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    db.delete(get_owned(db, Payslip, slip_id, user))
    return redirect('/pay', flash='Payslip deleted', request=request)
