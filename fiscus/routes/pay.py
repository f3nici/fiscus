"""Pay schedules and payslips.

A payslip records a payday of a schedule or is a one-off. Recent paydays
without one are asked for; a PDF, or several, fill in the figures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date
from datetime import timedelta

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy.orm import Session

from fiscus.domain import payslip as payslip_pdf
from fiscus.domain import recurrence
from fiscus.domain.pay import ASK_DAYS, LINK_DAYS, MATCH_DAYS, OpenPayday, Unsure, match_paydays
from fiscus.domain.tax import fy_bounds
from fiscus.domain.uploads import MAX_BYTES, ImportError_
from fiscus.forms import (
    FormError,
    choice,
    ends_after,
    int_or,
    money,
    opt_date,
    req_date,
    text,
)
from fiscus.models import PAY_FREQUENCIES, Account, PaySchedule, Payslip, User
from fiscus.money import fmt, fmt_plain
from fiscus.services import ledger, pay
from fiscus.services.access import get_owned, maybe_owned, owned
from fiscus.web import ctx, current_user, get_db, redirect, render

router = APIRouter()
MAX_FILES = 40
# Paydays offered for a payslip run this far ahead (a payslip often comes first).
AHEAD_DAYS = 14


@dataclass
class SlipForm:
    """The payslip form's values, as strings, with where they came from."""

    date: str = ''
    source: str = ''
    gross: str = ''
    tax_withheld: str = ''
    net: str = ''
    account_id: int = 0
    payday: str = 'auto'  # 'auto', '' for a one-off, or 'schedule_id:YYYY-MM-DD'
    file_name: str = ''
    notes: list[str] = field(default_factory=list)
    error: str = ''
    duplicate: bool = False


def _plain(cents: int | None) -> str:
    return '' if cents is None else fmt_plain(cents)


def _options(
    db: Session, user: User, today: Date, except_slip: int | None = None
) -> list[OpenPayday]:
    # Back to the start of the financial year, so an older pay can be chosen
    # even though only the last eight weeks are asked for.
    start = min(today - timedelta(days=ASK_DAYS), fy_bounds(today)[0])
    return pay.open_paydays(db, user, start, today + timedelta(days=AHEAD_DAYS), except_slip)


def _with(options: list[OpenPayday], extra: list[OpenPayday]) -> list[OpenPayday]:
    keys = {o.key for o in options}
    out = options + [o for o in extra if o.key not in keys]
    out.sort(key=lambda o: (o.payday, o.name))
    return out


def _parse_payday(db: Session, user: User, value: str) -> tuple[PaySchedule, Date] | None:
    sid, _, day = value.partition(':')
    try:
        when = Date.fromisoformat(day)
        ps = maybe_owned(db, PaySchedule, int(sid), user)
    except ValueError:
        return None
    if ps is None or not pay.is_payday(ps, when):
        return None
    return ps, when


def _prefill(db: Session, user: User, value: str, today: Date) -> SlipForm | None:
    """The form for a payday still to put in: the schedule's expected figures."""
    found = _parse_payday(db, user, value)
    if found is None:
        return None
    ps, when = found
    net = pay.net_per_pay(db, user, ps, today)
    scaled = not ps.varies or not ps.net_cents
    return SlipForm(
        date=when.isoformat(),
        source=ps.name,
        gross=_plain(ps.gross_cents) if scaled else '',
        tax_withheld=_plain(ps.tax_withheld_cents) if scaled else '',
        net=_plain(net),
        account_id=ps.account_id or 0,
        payday=value,
        notes=['These are the expected figures. Change them to what the payslip says.'],
    )


@router.get('/pay')
def pay_page(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Response:
    q = request.query_params
    today = ctx(request).clock.today()
    slip = maybe_owned(db, Payslip, int_or(q.get('slip')), user)
    form: SlipForm | None = None
    if slip is not None:
        form = SlipForm(
            date=slip.date.isoformat(),
            source=slip.source,
            gross=fmt_plain(slip.gross_cents),
            tax_withheld=fmt_plain(slip.tax_withheld_cents),
            net=fmt_plain(slip.net_cents),
            account_id=slip.account_id or 0,
            payday=(
                f'{slip.schedule_id}:{slip.payday.isoformat()}'
                if slip.schedule_id and slip.payday
                else ''
            ),
        )
    elif q.get('payday'):
        form = _prefill(db, user, q.get('payday', ''), today)
    return _page(request, user, db, form, slip)


def _page(
    request: Request,
    user: User,
    db: Session,
    form: SlipForm | None = None,
    slip: Payslip | None = None,
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
    options = _options(db, user, today, slip.id if slip else None)
    if form is not None and form.payday not in ('', 'auto'):
        found = _parse_payday(db, user, form.payday)
        if found is not None:
            options = _with(options, [OpenPayday(found[0].id, found[1], found[0].name)])
    return render(
        request,
        'pay/index.html',
        {
            'schedules': schedules,
            'expected': {ps.id: net for ps, net in expected},
            'next': nxt,
            'slips': slips,
            'names': {s.id: s.name for s in schedules},
            'freqs': PAY_FREQUENCIES,
            'accounts': ledger.accounts(db, user),
            'ytd': {
                'gross': sum(p.gross_cents for p in ytd),
                'tax': sum(p.tax_withheld_cents for p in ytd),
                'net': sum(p.net_cents for p in ytd),
            },
            'edit': maybe_owned(db, PaySchedule, int_or(request.query_params.get('edit')), user),
            'to_put_in': pay.to_put_in(db, user, today),
            'form': form or SlipForm(date=today.isoformat()),
            'slip': slip,
            'options': options,
        },
    )


def _opt_positive(value: str, label: str) -> int | None:
    return money(value, label, allow_negative=False) if value.strip() else None


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
        anchor = req_date(anchor_date, 'A payday')
        values = dict(
            name=text(name, 'Name', 80),
            frequency=choice(frequency, PAY_FREQUENCIES, 'Frequency'),
            anchor_date=anchor,
            net_cents=money(net, 'Net pay', allow_negative=False),
            gross_cents=_opt_positive(gross, 'Gross pay'),
            tax_withheld_cents=_opt_positive(tax_withheld, 'Tax withheld'),
            end_date=ends_after(anchor, opt_date(end_date, 'End date')),
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


def _link(
    db: Session,
    user: User,
    value: str,
    when: Date,
    source: str,
    except_slip: int | None,
    taken: set[str] | None = None,
) -> tuple[PaySchedule, Date] | None:
    """The payday a payslip records: chosen, matched by date, or none (a one-off)."""
    taken = taken or set()
    if value == '':
        return None
    if value == 'auto':
        near = [
            o
            for o in pay.open_paydays(
                db,
                user,
                when - timedelta(days=MATCH_DAYS),
                when + timedelta(days=MATCH_DAYS),
                except_slip,
            )
            if o.key not in taken
        ]
        hit = match_paydays([(when, source)], near)[0]
        if hit is None:
            return None
        if isinstance(hit, Unsure):
            raise FormError(_unsure(hit))
        value = hit.key
    found = _parse_payday(db, user, value)
    if found is None:
        raise FormError('That payday is not one of your pay schedules')
    ps, day = found
    if abs((when - day).days) > LINK_DAYS:
        raise FormError(f'The pay date is more than {LINK_DAYS} days from that payday')
    clash = db.execute(
        owned(Payslip, user).where(Payslip.schedule_id == ps.id, Payslip.payday == day)
    ).scalar_one_or_none()
    if clash is not None and clash.id != except_slip:
        raise FormError(
            f'{ps.name} on {day.strftime("%d %b")} already has a payslip'
            f' ({clash.source or "no employer"}, net {fmt(clash.net_cents)}).'
            ' If that one is for another job, edit it and choose its pay first.'
        )
    if value in taken:
        raise FormError(f'{ps.name} on {day.strftime("%d %b")} already has a payslip')
    return ps, day


def _unsure(hit: Unsure) -> str:
    names = sorted({o.name for o in hit.choices})
    jobs = ', '.join(names[:-1]) + ' and ' + names[-1]
    every = 'all' if len(names) > 2 else 'both'
    return f'{jobs} are {every} paid around then. Choose which pay this is.'


def _fill(
    db: Session,
    user: User,
    slip: Payslip,
    form: SlipForm,
    taken: set[str] | None = None,
) -> None:
    """Validate the form into the payslip. Raises FormError."""
    when = req_date(form.date, 'Date')
    gross = money(form.gross, 'Gross', allow_negative=False)
    withheld = money(form.tax_withheld, 'Tax withheld', allow_negative=False)
    net = money(form.net, 'Net', allow_negative=False)
    if withheld > gross:
        raise FormError('Tax withheld is more than gross pay')
    source = form.source.strip()[:120]
    link = _link(db, user, form.payday, when, source, slip.id, taken)
    slip.date = when
    slip.gross_cents, slip.tax_withheld_cents, slip.net_cents = gross, withheld, net
    slip.source = source
    acct = maybe_owned(db, Account, form.account_id, user)
    slip.account_id = acct.id if acct else None
    slip.schedule_id = link[0].id if link else None
    slip.payday = link[1] if link else None


def _saved_flash(slip: Payslip, today: Date, names: dict[int, str], moved: str = '') -> str:
    what = (
        f'Payslip saved for {names.get(slip.schedule_id, "your pay")} on '
        f'{slip.payday.strftime("%d %b")}'
        if slip.schedule_id and slip.payday
        else 'Payslip saved as a one-off'
    )
    if slip.date > today:
        return what + moved + '. It is in your forecast.'
    return what + moved


def _names(db: Session, user: User) -> dict[int, str]:
    return {s.id: s.name for s in db.execute(owned(PaySchedule, user)).scalars()}


@router.post('/pay/schedules/{schedule_id}/no-pay')
def no_pay(
    schedule_id: int,
    request: Request,
    payday: str = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Nothing was paid on this payday (no shifts, unpaid leave): a payslip of zero."""
    ps = get_owned(db, PaySchedule, schedule_id, user)
    try:
        day = req_date(payday, 'Payday')
        link = _link(db, user, f'{ps.id}:{day.isoformat()}', day, ps.name, None)
    except FormError as exc:
        return redirect('/pay', flash=str(exc), request=request)
    assert link is not None
    db.add(
        Payslip(
            user_id=user.id,
            date=day,
            gross_cents=0,
            tax_withheld_cents=0,
            net_cents=0,
            source=ps.name,
            schedule_id=ps.id,
            payday=day,
        )
    )
    return redirect('/pay', flash=f'{ps.name} on {day.strftime("%d %b")}: no pay', request=request)


@dataclass
class ReadRow:
    index: int
    file_name: str
    form: SlipForm
    include: bool


async def _read_files(files: list[UploadFile]) -> list[tuple[str, payslip_pdf.ReadPayslip | str]]:
    out: list[tuple[str, payslip_pdf.ReadPayslip | str]] = []
    for f in files:
        name = (f.filename or 'payslip.pdf')[:120]
        data = await f.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            out.append((name, 'That file is larger than 10 MB'))
            continue
        try:
            out.append((name, await run_in_threadpool(payslip_pdf.read, data)))
        except ImportError_ as exc:
            out.append((name, str(exc)))
    return out


def _form_from(
    db: Session,
    user: User,
    name: str,
    slip: payslip_pdf.ReadPayslip,
    hit: OpenPayday | Unsure | None,
) -> SlipForm:
    when = slip.pay_date or slip.period_end
    notes = slip.notes()
    duplicate = (
        when is not None
        and slip.net_cents is not None
        and db.execute(
            owned(Payslip, user).where(Payslip.date == when, Payslip.net_cents == slip.net_cents)
        ).first()
        is not None
    )
    if duplicate:
        notes.append('You already have a payslip for this date and net pay.')
    elif isinstance(hit, Unsure):
        notes.append(_unsure(hit))
    elif hit is None:
        notes.append(
            'No payday of your schedules matched, so it is a one-off unless you choose one.'
        )
    return SlipForm(
        date=when.isoformat() if when else '',
        source=slip.employer,
        gross=_plain(slip.gross_cents),
        tax_withheld=_plain(slip.tax_withheld_cents),
        net=_plain(slip.net_cents),
        payday=hit.key if isinstance(hit, OpenPayday) else 'auto' if hit else '',
        file_name=name,
        notes=notes,
        duplicate=duplicate,
    )


@router.post('/payslips/read')
async def read_payslips(
    request: Request,
    file: list[UploadFile] = File(...),
    payday: str = Form(''),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Fill in payslips from PDFs. Nothing is saved until the person adds them."""
    if len(file) > MAX_FILES:
        return redirect(
            '/pay', flash=f'Upload up to {MAX_FILES} payslips at a time', request=request
        )
    read = await _read_files(file)
    today = ctx(request).clock.today()
    good = [r for _n, r in read if not isinstance(r, str)]
    if len(read) == 1:
        name, result = read[0]
        if isinstance(result, str):
            return redirect('/pay', flash=result, request=request)
        # Read for a payday the person chose to put in, else matched by date.
        context = _parse_payday(db, user, payday) if payday else None
        hit: OpenPayday | Unsure | None = (
            OpenPayday(context[0].id, context[1], context[0].name)
            if context is not None
            else _match(db, user, [result])[0]
        )
        form = _form_from(db, user, name, result, hit)
        if isinstance(hit, OpenPayday):
            linked = maybe_owned(db, PaySchedule, hit.schedule_id, user)
            form.account_id = (linked.account_id or 0) if linked else 0
        return _page(request, user, db, form)
    hits = iter(_match(db, user, good))
    rows: list[ReadRow] = []
    errors: list[tuple[str, str]] = []
    for name, result in read:
        if isinstance(result, str):
            errors.append((name, result))
            continue
        form = _form_from(db, user, name, result, next(hits))
        rows.append(ReadRow(len(rows), name, form, include=not form.duplicate))
    rows.sort(key=lambda r: r.form.date or '9999')
    for i, r in enumerate(rows):
        r.index = i
    return _review(request, user, db, rows, errors, today)


def _match(
    db: Session, user: User, slips: list[payslip_pdf.ReadPayslip]
) -> list[OpenPayday | Unsure | None]:
    dated = [(s.pay_date or s.period_end, s.employer) for s in slips]
    known = [d for d, _e in dated if d is not None]
    if not known:
        return [None] * len(slips)
    pad = timedelta(days=MATCH_DAYS)
    open_ = pay.open_paydays(db, user, min(known) - pad, max(known) + pad)
    hits = match_paydays([(d, e) for d, e in dated if d is not None], open_)
    it = iter(hits)
    return [next(it) if d is not None else None for d, _e in dated]


def _iso(value: str) -> Date | None:
    try:
        return Date.fromisoformat(value)
    except ValueError:
        return None


def _review(
    request: Request,
    user: User,
    db: Session,
    rows: list[ReadRow],
    errors: list[tuple[str, str]],
    today: Date,
    saved: str = '',
) -> Response:
    dates = [d for d in (_iso(r.form.date) for r in rows) if d is not None]
    lo = min([*dates, today]) - timedelta(days=LINK_DAYS)
    hi = max([*dates, today]) + timedelta(days=AHEAD_DAYS)
    options = pay.open_paydays(db, user, lo, hi)
    return render(
        request,
        'pay/review.html',
        {
            'rows': rows,
            'errors': errors,
            'options': options,
            'accounts': ledger.accounts(db, user),
            'saved': saved,
        },
    )


@router.post('/payslips/bulk')
async def save_bulk(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Add the reviewed payslips that are ticked. Each is checked like a single one."""
    data = await request.form()

    def values(key: str) -> list[str]:
        return [str(v) for v in data.getlist(key)]

    dates, sources = values('date'), values('source')
    grosses, taxes, nets, paydays = (
        values('gross'),
        values('tax_withheld'),
        values('net'),
        values('payday'),
    )
    names = values('file_name')
    include = set(values('include'))
    account_id = int_or(data.get('account_id'))
    n = len(dates)
    if not all(len(v) == n for v in (sources, grosses, taxes, nets, paydays, names)):
        return redirect(
            '/pay', flash='The review form was incomplete. Upload again.', request=request
        )
    taken: set[str] = set()
    saved = 0
    rows: list[ReadRow] = []
    clock = ctx(request).clock
    today = clock.today()
    before = ledger.balances(db, user, today)
    for i in range(n):
        form = SlipForm(
            dates[i], sources[i], grosses[i], taxes[i], nets[i], account_id, paydays[i], names[i]
        )
        if str(i) not in include:
            continue
        slip = Payslip(user_id=user.id)
        try:
            _fill(db, user, slip, form, taken)
        except FormError as exc:
            form.error = str(exc)
            rows.append(ReadRow(len(rows), names[i], form, include=True))
            continue
        if slip.schedule_id and slip.payday:
            taken.add(f'{slip.schedule_id}:{slip.payday.isoformat()}')
        db.add(slip)
        db.flush()
        pay.follow(db, clock, user, slip)
        saved += 1
    msg = (
        f'{saved} payslip{"" if saved == 1 else "s"} saved{ledger.now_at(db, user, before, today)}'
    )
    if rows:
        # What was valid is saved; the rest is shown again to fix.
        return _review(request, user, db, rows, [], today, msg)
    return redirect('/pay', flash=msg, request=request)


# After /payslips/read and /payslips/bulk: those literal paths sit beside {slip_id}.
@router.post('/payslips')
@router.post('/payslips/{slip_id}')
def save_payslip(
    request: Request,
    slip_id: int | None = None,
    date: str = Form(...),
    gross: str = Form(...),
    tax_withheld: str = Form(...),
    net: str = Form(...),
    source: str = Form(''),
    account_id: int = Form(0),
    payday: str = Form('auto'),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    existing = get_owned(db, Payslip, slip_id, user) if slip_id else None
    clock = ctx(request).clock
    today = clock.today()
    before = ledger.balances(db, user, today)
    slip = existing or Payslip(user_id=user.id)
    form = SlipForm(date, source, gross, tax_withheld, net, account_id, payday)
    try:
        _fill(db, user, slip, form)
    except FormError as exc:
        form.error = str(exc)
        return _page(request, user, db, form, existing)
    if existing is None:
        db.add(slip)
    db.flush()
    pay.follow(db, clock, user, slip)
    flash = _saved_flash(slip, today, _names(db, user), ledger.now_at(db, user, before, today))
    return redirect('/pay', flash=flash, request=request)


@router.post('/payslips/{slip_id}/delete')
def delete_payslip(
    slip_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    today = ctx(request).clock.today()
    before = ledger.balances(db, user, today)
    db.delete(get_owned(db, Payslip, slip_id, user))
    flash = f'Payslip deleted{ledger.now_at(db, user, before, today)}'
    return redirect('/pay', flash=flash, request=request)
