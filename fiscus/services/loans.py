"""Loan summaries (repayment, offset savings and payoff with and without the
offset) and repayments marked paid, which come off the repayment account
(D76)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from fiscus.clock import Clock, utcnow_naive
from fiscus.domain import loan as loan_math
from fiscus.domain.loan import LoanSpec, repayments
from fiscus.domain.tax import fy_bounds
from fiscus.models import Account, Loan, LoanRepayment, User
from fiscus.services import bills, ledger
from fiscus.services.access import owned

# Not marked paid this far back, a repayment still has to come off its
# account, as an unpaid bill does; older ones count as made.
DUE_CARRY_DAYS = bills.OVERDUE_CARRY_DAYS


def loan_spec(db: Session, loan: Loan) -> LoanSpec:
    offsets = (
        db.execute(
            select(Account.id).where(
                Account.loan_id == loan.id,
                Account.user_id == loan.user_id,
                Account.archived.is_(False),
            )
        )
        .scalars()
        .all()
    )
    rates = [(r.effective_date, Decimal(r.rate)) for r in loan.rates]
    if not rates:
        rates = [(loan.start_date, Decimal(0))]
    return LoanSpec(
        id=loan.id,
        name=loan.name,
        principal_cents=loan.principal_cents,
        start_date=loan.start_date,
        term_months=loan.term_months,
        frequency=loan.repayment_frequency,
        balance_cents=loan.balance_cents,
        balance_date=loan.balance_date,
        rates=rates,
        repayment_override_cents=loan.repayment_override_cents,
        first_repayment=loan.first_repayment_date,
        account_id=loan.repayment_account_id,
        offset_account_ids=tuple(offsets),
    )


@dataclass(frozen=True)
class LoanSummary:
    loan: Loan
    rate: str
    repayment_cents: int
    offset_ids: tuple[int, ...]
    offset_balance_cents: int
    saved_month_cents: int
    saved_year_cents: int
    # The first day of each that is counted, when the offset's balance was
    # first typed in after the month or year began.
    saved_month_from: date | None
    saved_year_from: date | None
    with_offset: loan_math.Payoff
    without_offset: loan_math.Payoff
    next_repayment: date | None = None
    # False until settlement: there is no interest, so nothing saved yet.
    started: bool = True

    @property
    def lifetime_saving_cents(self) -> int:
        return self.without_offset.total_interest - self.with_offset.total_interest

    @property
    def months_sooner(self) -> int | None:
        a, b = self.with_offset.paid_off, self.without_offset.paid_off
        if a is None or b is None:
            return None
        return (b.year - a.year) * 12 + (b.month - a.month)


def summarise(db: Session, user: User, loan: Loan, today: date) -> LoanSummary:
    spec = loan_spec(db, loan)
    balances = ledger.balances(db, user, today)
    offset_now = sum(max(0, balances.get(a, 0)) for a in spec.offset_account_ids)

    month_start = today.replace(day=1)
    fy_start, _ = fy_bounds(today)

    def saved_since(start: date) -> tuple[int, date | None]:
        start = max(start, loan.start_date)
        if start > today:
            return 0, None
        daily, known = ledger.balance_history(db, user, list(spec.offset_account_ids), start, today)
        saved = loan_math.interest_saved(loan.balance_cents, spec.rates, daily)
        return saved, (known if known > start else None)

    # The current repayment, after any rate changes up to today.
    sim = loan_math.LoanSim(spec, opening_offset_cents=offset_now)
    day = spec.balance_date
    while day < today:
        day += timedelta(days=1)
        sim.repayments_due(day)
        sim.end_of_day(day, offset_now)
    month_saved, month_from = saved_since(month_start)
    year_saved, year_from = saved_since(fy_start)
    projected_from = max(today, spec.balance_date)
    with_offset = loan_math.project_payoff(spec, projected_from, lambda _d: offset_now)
    without = loan_math.project_payoff(spec, projected_from, lambda _d: 0)
    return LoanSummary(
        loan=loan,
        rate=str(loan_math.rate_on(spec.rates, today)),
        repayment_cents=sim.payment,
        offset_ids=spec.offset_account_ids,
        offset_balance_cents=offset_now,
        saved_month_cents=month_saved,
        saved_year_cents=year_saved,
        saved_month_from=month_from,
        saved_year_from=year_from,
        with_offset=with_offset,
        without_offset=without,
        next_repayment=spec.next_repayment(today) if loan.balance_cents > 0 else None,
        started=loan.start_date <= today,
    )


@dataclass(frozen=True)
class Repayment:
    """A scheduled repayment, and whether it has come off its account (D76)."""

    loan: Loan
    day: date
    cents: int  # what was paid once marked (D57's way); what the loan asks till then
    # paid: marked paid. made: counted as made with nothing to mark, as a
    # balance typed in after its day has it, its loan's repayments are not
    # tracked in an account, or it is older than DUE_CARRY_DAYS. due: fallen
    # due and not off its account yet. upcoming: still to come.
    status: str
    marked: LoanRepayment | None = None

    @property
    def made(self) -> bool:
        return self.status in ('paid', 'made')


def tracked_in(db: Session, user: User, loan: Loan) -> Account | None:
    """The open account the loan's repayments come from; None when they are
    not tracked in one."""
    return next((a for a in ledger.accounts(db, user) if a.id == loan.repayment_account_id), None)


def scheduled(db: Session, user: User, start: date, end: date, today: date) -> list[Repayment]:
    """Every loan's repayments from `start` to `end`, by loan name then day,
    each with whether it has come off its account (D76). Stepped with today's
    offset held steady, as on the calendar."""
    loans = db.execute(owned(Loan, user).order_by(Loan.name, Loan.id)).scalars().all()
    if not loans:
        return []
    balances = ledger.balances(db, user, today)
    typed = ledger.typed_days(db, user, today)
    open_ids = {a.id for a in ledger.accounts(db, user)}
    marked = {
        (m.loan_id, m.due_date): m
        for m in db.execute(
            owned(LoanRepayment, user).where(
                LoanRepayment.due_date >= start, LoanRepayment.due_date <= end
            )
        ).scalars()
    }
    oldest = today - timedelta(days=DUE_CARRY_DAYS)
    out: list[Repayment] = []
    for loan in loans:
        spec = loan_spec(db, loan)
        offset = sum(max(0, balances.get(a, 0)) for a in spec.offset_account_ids)
        acct = loan.repayment_account_id if loan.repayment_account_id in open_ids else None
        for day, cents in repayments(spec, start, end, offset):
            m = marked.get((loan.id, day))
            if m is not None:
                out.append(Repayment(loan, day, m.amount_cents, 'paid', m))
            elif day > today:
                out.append(Repayment(loan, day, cents, 'upcoming'))
            elif acct is None or day < oldest or (acct in typed and day < typed[acct]):
                # A balance typed in after its day has it (D45).
                out.append(Repayment(loan, day, cents, 'made'))
            else:
                out.append(Repayment(loan, day, cents, 'due'))
    return out


def due(db: Session, user: User, today: date) -> list[Repayment]:
    """Repayments fallen due that have not come off their account: the
    forecast takes each off tomorrow, as an unpaid bill (D76)."""
    return [
        r
        for r in scheduled(db, user, today - timedelta(days=DUE_CARRY_DAYS), today, today)
        if r.status == 'due'
    ]


def mark_paid(db: Session, clock: Clock, user: User, loan: Loan, day: date) -> LoanRepayment | None:
    """Mark the loan's repayment of `day`, today or before, paid: it comes off
    the repayment account dated its day, at what the loan asked (D76). None
    when no repayment falls on `day` by today."""
    today = clock.today()
    existing = db.execute(
        owned(LoanRepayment, user).where(
            LoanRepayment.loan_id == loan.id, LoanRepayment.due_date == day
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    found = [r for r in scheduled(db, user, day, day, today) if r.loan.id == loan.id]
    if not found or day > today:
        return None
    row = LoanRepayment(
        user_id=user.id,
        loan_id=loan.id,
        due_date=day,
        amount_cents=found[0].cents,
        created_at=utcnow_naive(clock),
    )
    db.add(row)
    db.flush()
    ledger.follow(
        db,
        clock,
        user,
        row,
        account_id=loan.repayment_account_id,
        day=day,
        cents=-row.amount_cents,
        description=f'{loan.name} repayment',
    )
    return row


def unmark(db: Session, user: User, loan: Loan, day: date) -> bool:
    """Undo a repayment marked paid: its transaction goes with it."""
    row = db.execute(
        owned(LoanRepayment, user).where(
            LoanRepayment.loan_id == loan.id, LoanRepayment.due_date == day
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True


@dataclass(frozen=True)
class BudgetLoan:
    """A home loan's repayments in a budget month: always in the budget, set
    by the loan, and spent as each comes off its account (D76)."""

    loan: Loan
    repayments: tuple[Repayment, ...]
    # Fallen due before the budget month and still not off its account: shown
    # on the month under way to be marked paid, outside its figures.
    earlier: tuple[Repayment, ...] = ()
    account: Account | None = None  # where they come off; None if not tracked

    @property
    def budget_cents(self) -> int:
        return sum(r.cents for r in self.repayments)

    @property
    def paid_cents(self) -> int:
        return sum(r.cents for r in self.repayments if r.made)

    @property
    def count(self) -> int:
        return len(self.repayments)

    @property
    def paid_count(self) -> int:
        return sum(1 for r in self.repayments if r.made)

    @property
    def remaining_cents(self) -> int:
        return self.budget_cents - self.paid_cents

    @property
    def due(self) -> list[Repayment]:
        """To mark paid, oldest first."""
        return [r for r in (*self.earlier, *self.repayments) if r.status == 'due']

    @property
    def marked(self) -> list[Repayment]:
        return [r for r in self.repayments if r.status == 'paid']


def budget_lines(db: Session, user: User, start: date, end: date, today: date) -> list[BudgetLoan]:
    found = scheduled(db, user, start, end, today)
    earlier = [r for r in due(db, user, today) if r.day < start] if start <= today <= end else []
    accts = {a.id: a for a in ledger.accounts(db, user)}
    loans: dict[int, Loan] = {}
    for r in (*found, *earlier):
        loans.setdefault(r.loan.id, r.loan)
    return [
        BudgetLoan(
            loan,
            tuple(r for r in found if r.loan.id == loan.id),
            tuple(r for r in earlier if r.loan.id == loan.id),
            accts.get(loan.repayment_account_id) if loan.repayment_account_id else None,
        )
        for loan in sorted(loans.values(), key=lambda x: (x.name, x.id))
    ]
