"""Schema.

Money columns are integer cents. Interest rates are decimal strings ("6.24"
meaning 6.24 % p.a.) so nothing is ever a float. Datetimes are naive UTC.
Every table holding one person's finances has `user_id`, and every read goes
through `services.access.owned` so that column is always in the WHERE clause.
"""

from __future__ import annotations

import datetime as dt
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fiscus.db import Base

ACCOUNT_TYPES = ('transaction', 'offset', 'savings', 'credit_card')
FREQUENCIES = ('weekly', 'fortnightly', 'monthly', 'bimonthly', 'quarterly', 'yearly', 'one_off')
PAY_FREQUENCIES = ('weekly', 'fortnightly', 'monthly', 'quarterly')
SPLIT_MODES = ('equal', 'percent', 'fixed')


class Owned:
    """Marker: rows belong to one user and changing them invalidates their forecast."""

    user_id: Mapped[int]


# --------------------------------------------------------------------- identity


class User(Base):
    __tablename__ = 'users'

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # WebAuthn user handle. Random, never the database id.
    user_handle: Mapped[bytes] = mapped_column(LargeBinary(64), unique=True)
    password_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    data_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime)

    credentials: Mapped[list[WebAuthnCredential]] = relationship(
        back_populates='user', cascade='all, delete-orphan'
    )


class WebAuthnCredential(Base):
    __tablename__ = 'webauthn_credentials'

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    credential_id: Mapped[bytes] = mapped_column(LargeBinary(1024), unique=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary(2048))
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    transports: Mapped[list[str]] = mapped_column(JSON, default=list)
    name: Mapped[str] = mapped_column(String(80))
    backed_up: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship(back_populates='credentials')


class AuthSession(Base):
    __tablename__ = 'sessions'

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime)
    method: Mapped[str] = mapped_column(String(20))
    ip: Mapped[str] = mapped_column(String(64), default='')
    user_agent: Mapped[str] = mapped_column(String(255), default='')


class SetupLink(Base):
    """One-time, time-limited link to register a passkey.

    purpose: bootstrap (first admin, no user yet), onboard (new user) or
    recovery (existing user who lost their passkeys).
    """

    __tablename__ = 'setup_links'

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    purpose: Mapped[str] = mapped_column(String(20))
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey('users.id', ondelete='CASCADE'), nullable=True, index=True
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey('users.id', ondelete='SET NULL'), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuthChallenge(Base):
    """A WebAuthn challenge, single use, five minutes."""

    __tablename__ = 'auth_challenges'

    id: Mapped[int] = mapped_column(primary_key=True)
    handle: Mapped[str] = mapped_column(String(64), unique=True)
    purpose: Mapped[str] = mapped_column(String(20))
    challenge: Mapped[bytes] = mapped_column(LargeBinary(64))
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey('users.id', ondelete='CASCADE'), nullable=True
    )
    setup_link_id: Mapped[int | None] = mapped_column(
        ForeignKey('setup_links.id', ondelete='CASCADE'), nullable=True
    )
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class AuditLog(Base):
    """Append-only. A trigger in the migration refuses UPDATE and DELETE."""

    __tablename__ = 'audit_log'

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, index=True)
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subject_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64))
    ip: Mapped[str] = mapped_column(String(64), default='')
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AppState(Base):
    __tablename__ = 'app_state'

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class UserSettings(Base, Owned):
    __tablename__ = 'user_settings'

    user_id: Mapped[int] = mapped_column(
        ForeignKey('users.id', ondelete='CASCADE'), primary_key=True
    )
    low_balance_cents: Mapped[int] = mapped_column(Integer, default=0)
    forecast_months: Mapped[int] = mapped_column(Integer, default=12)
    spending_account_id: Mapped[int | None] = mapped_column(
        ForeignKey('accounts.id', ondelete='SET NULL'), nullable=True
    )
    large_bill_cents: Mapped[int] = mapped_column(Integer, default=50000)
    ntfy_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    ntfy_url: Mapped[str] = mapped_column(String(255), default='')
    ntfy_topic: Mapped[str] = mapped_column(String(120), default='')
    ntfy_token: Mapped[str] = mapped_column(String(255), default='')
    ntfy_bill_days: Mapped[int] = mapped_column(Integer, default=3)
    ntfy_bills_due: Mapped[bool] = mapped_column(Boolean, default=True)
    ntfy_overdue: Mapped[bool] = mapped_column(Boolean, default=True)
    ntfy_low_balance: Mapped[bool] = mapped_column(Boolean, default=True)
    ntfy_hour: Mapped[int] = mapped_column(Integer, default=8)
    # The budget month runs from this day to the day before it next month.
    budget_start_day: Mapped[int] = mapped_column(Integer, default=1)


class SquareConnection(Base, Owned):
    """A person's Square seller account, synced into one of their accounts.

    Off until they save an access token. The token is stored like the ntfy
    token: in the database, never shown again, and in backups.
    """

    __tablename__ = 'square_connections'

    user_id: Mapped[int] = mapped_column(
        ForeignKey('users.id', ondelete='CASCADE'), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    access_token: Mapped[str] = mapped_column(String(255), default='')
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey('accounts.id', ondelete='SET NULL'), nullable=True
    )
    income_category_id: Mapped[int | None] = mapped_column(
        ForeignKey('categories.id', ondelete='SET NULL'), nullable=True
    )
    fee_category_id: Mapped[int | None] = mapped_column(
        ForeignKey('categories.id', ondelete='SET NULL'), nullable=True
    )
    payout_category_id: Mapped[int | None] = mapped_column(
        ForeignKey('categories.id', ondelete='SET NULL'), nullable=True
    )
    start_date: Mapped[date] = mapped_column(Date)
    # Everything created before this (UTC) has been fetched; the next sync
    # starts a week earlier, as Square can complete a payment late.
    synced_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_result: Mapped[str] = mapped_column(String(255), default='')
    last_error: Mapped[str] = mapped_column(String(255), default='')


class SquareInvoice(Base, Owned):
    """An unpaid Square invoice as of the last sync. Replaced on every sync."""

    __tablename__ = 'square_invoices'

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    square_id: Mapped[str] = mapped_column(String(120))
    number: Mapped[str] = mapped_column(String(120), default='')
    customer: Mapped[str] = mapped_column(String(255), default='')
    due_date: Mapped[date] = mapped_column(Date)
    amount_due_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default='')


# ---------------------------------------------------------------- accounts etc.


class Account(Base, Owned):
    __tablename__ = 'accounts'

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    name: Mapped[str] = mapped_column(String(80))
    type: Mapped[str] = mapped_column(String(20))
    opening_balance_cents: Mapped[int] = mapped_column(Integer, default=0)
    is_set_aside: Mapped[bool] = mapped_column(Boolean, default=False)
    loan_id: Mapped[int | None] = mapped_column(
        ForeignKey('loans.id', ondelete='SET NULL'), nullable=True
    )
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    # The day the balance was last typed in by hand.
    balance_updated_on: Mapped[date | None] = mapped_column(Date, nullable=True)


class ImportMapping(Base, Owned):
    """Column mapping for a non-ANZ CSV, saved per account."""

    __tablename__ = 'import_mappings'

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey('accounts.id', ondelete='CASCADE'), unique=True
    )
    mapping: Mapped[dict[str, Any]] = mapped_column(JSON)


class Import(Base, Owned):
    __tablename__ = 'imports'

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey('accounts.id', ondelete='CASCADE'))
    file_name: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(255), default='')
    status: Mapped[str] = mapped_column(String(20), default='pending')
    format: Mapped[str] = mapped_column(String(20), default='')
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Category(Base, Owned):
    __tablename__ = 'categories'
    __table_args__ = (UniqueConstraint('user_id', 'name'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    name: Mapped[str] = mapped_column(String(80))
    is_business: Mapped[bool] = mapped_column(Boolean, default=False)
    is_deductible: Mapped[bool] = mapped_column(Boolean, default=False)
    is_income: Mapped[bool] = mapped_column(Boolean, default=False)
    # Moves between the user's own accounts: never spending, never income.
    is_transfer: Mapped[bool] = mapped_column(Boolean, default=False)


class Transaction(Base, Owned):
    __tablename__ = 'transactions'
    __table_args__ = (
        UniqueConstraint('account_id', 'dedup_hash'),
        Index('ix_transactions_user_date', 'user_id', 'date'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'))
    account_id: Mapped[int] = mapped_column(
        ForeignKey('accounts.id', ondelete='CASCADE'), index=True
    )
    date: Mapped[date] = mapped_column(Date)
    amount_cents: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(500))
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey('categories.id', ondelete='SET NULL'), nullable=True, index=True
    )
    import_id: Mapped[int | None] = mapped_column(
        ForeignKey('imports.id', ondelete='CASCADE'), nullable=True, index=True
    )
    dedup_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime)


class Rule(Base, Owned):
    __tablename__ = 'rules'

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    pattern: Mapped[str] = mapped_column(String(200))
    is_regex: Mapped[bool] = mapped_column(Boolean, default=False)
    category_id: Mapped[int] = mapped_column(ForeignKey('categories.id', ondelete='CASCADE'))
    priority: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime)


# ------------------------------------------------------------------------ bills


class Bill(Base):
    """A bill has an owner and shares. The owner always holds a share row."""

    __tablename__ = 'bills'

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    name: Mapped[str] = mapped_column(String(120))
    amount_cents: Mapped[int] = mapped_column(Integer)
    frequency: Mapped[str] = mapped_column(String(20))
    next_due: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    split_mode: Mapped[str] = mapped_column(String(10), default='equal')
    match_tolerance_pct: Mapped[int] = mapped_column(Integer, default=5)
    match_window_days: Mapped[int] = mapped_column(Integer, default=3)
    description_hint: Mapped[str] = mapped_column(String(120), default='')
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)

    shares: Mapped[list[BillShare]] = relationship(
        back_populates='bill', cascade='all, delete-orphan', order_by='BillShare.id'
    )


class BillShare(Base, Owned):
    __tablename__ = 'bill_shares'
    __table_args__ = (UniqueConstraint('bill_id', 'user_id'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey('bills.id', ondelete='CASCADE'), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    # Basis points for percent, cents for fixed, unused for equal.
    split_value: Mapped[int] = mapped_column(Integer, default=0)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey('accounts.id', ondelete='SET NULL'), nullable=True
    )
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey('categories.id', ondelete='SET NULL'), nullable=True
    )

    bill: Mapped[Bill] = relationship(back_populates='shares')


class BillPayment(Base, Owned):
    """One user's occurrence of a bill, marked paid (matched or by hand)."""

    __tablename__ = 'bill_payments'
    __table_args__ = (UniqueConstraint('bill_id', 'user_id', 'due_date'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey('bills.id', ondelete='CASCADE'), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    due_date: Mapped[date] = mapped_column(Date)
    paid_on: Mapped[date] = mapped_column(Date)
    transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey('transactions.id', ondelete='CASCADE'), nullable=True, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime)


# ----------------------------------------------------------------------- income


class PaySchedule(Base, Owned):
    __tablename__ = 'pay_schedules'

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    name: Mapped[str] = mapped_column(String(80))
    frequency: Mapped[str] = mapped_column(String(20))
    anchor_date: Mapped[date] = mapped_column(Date)
    net_cents: Mapped[int] = mapped_column(Integer)
    gross_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tax_withheld_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey('accounts.id', ondelete='SET NULL'), nullable=True
    )
    # Casual pay: expect the recent average of deposits whose description
    # contains match_text; net_cents is the guess until any arrive.
    varies: Mapped[bool] = mapped_column(Boolean, default=False)
    match_text: Mapped[str] = mapped_column(String(120), default='')


class Payslip(Base, Owned):
    __tablename__ = 'payslips'
    # One payslip per payday of a schedule. Unlinked payslips are one-offs.
    __table_args__ = (UniqueConstraint('schedule_id', 'payday'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    date: Mapped[date] = mapped_column(Date)
    gross_cents: Mapped[int] = mapped_column(Integer)
    tax_withheld_cents: Mapped[int] = mapped_column(Integer)
    net_cents: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(120), default='')
    # Where the net lands. A payslip dated after today is forecast as income.
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey('accounts.id', ondelete='SET NULL'), nullable=True
    )
    # The schedule payday this payslip records; it then stands in for that
    # payday's expected pay. Both null for a one-off (bonus, back pay).
    schedule_id: Mapped[int | None] = mapped_column(
        ForeignKey('pay_schedules.id', ondelete='SET NULL'), nullable=True, index=True
    )
    payday: Mapped[dt.date | None] = mapped_column(Date, nullable=True)


# ------------------------------------------------------------------------ loans


class Loan(Base, Owned):
    __tablename__ = 'loans'

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    name: Mapped[str] = mapped_column(String(80))
    principal_cents: Mapped[int] = mapped_column(Integer)
    start_date: Mapped[date] = mapped_column(Date)
    term_months: Mapped[int] = mapped_column(Integer)
    repayment_frequency: Mapped[str] = mapped_column(String(20), default='monthly')
    # What is owed today, as the bank says. Projections start here.
    balance_cents: Mapped[int] = mapped_column(Integer)
    balance_date: Mapped[date] = mapped_column(Date)
    repayment_override_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    repayment_account_id: Mapped[int | None] = mapped_column(
        ForeignKey('accounts.id', ondelete='SET NULL', use_alter=True), nullable=True
    )

    rates: Mapped[list[LoanRate]] = relationship(
        back_populates='loan', cascade='all, delete-orphan', order_by='LoanRate.effective_date'
    )


class LoanRate(Base, Owned):
    __tablename__ = 'loan_rates'
    __table_args__ = (UniqueConstraint('loan_id', 'effective_date'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    loan_id: Mapped[int] = mapped_column(ForeignKey('loans.id', ondelete='CASCADE'), index=True)
    effective_date: Mapped[date] = mapped_column(Date)
    rate: Mapped[str] = mapped_column(String(12))

    loan: Mapped[Loan] = relationship(back_populates='rates')


# ------------------------------------------------------------------ planning


class RecurringTransfer(Base, Owned):
    """A planned move between the user's own accounts, e.g. tax set-aside."""

    __tablename__ = 'recurring_transfers'

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    name: Mapped[str] = mapped_column(String(80))
    from_account_id: Mapped[int] = mapped_column(ForeignKey('accounts.id', ondelete='CASCADE'))
    to_account_id: Mapped[int] = mapped_column(ForeignKey('accounts.id', ondelete='CASCADE'))
    amount_cents: Mapped[int] = mapped_column(Integer)
    frequency: Mapped[str] = mapped_column(String(20))
    anchor_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_tax_set_aside: Mapped[bool] = mapped_column(Boolean, default=False)


class Budget(Base, Owned):
    __tablename__ = 'budgets'
    __table_args__ = (UniqueConstraint('user_id', 'category_id'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey('categories.id', ondelete='CASCADE'))
    # Set aside on top of this person's bills in the category. The budget shown
    # is the bills' monthly equivalent plus this.
    monthly_cents: Mapped[int] = mapped_column(Integer)
    # Use this amount instead of the rolling average in the forecast.
    use_in_forecast: Mapped[bool] = mapped_column(Boolean, default=False)
    # Spent every few months (rego, insurance): the monthly share is saved up
    # from saved_since, starting from saved_opening_cents, and spending comes
    # out of what is saved. 1 is an ordinary monthly budget.
    cycle_months: Mapped[int] = mapped_column(Integer, default=1)
    saved_since: Mapped[date | None] = mapped_column(Date, nullable=True)
    saved_opening_cents: Mapped[int] = mapped_column(Integer, default=0)


class BudgetSpend(Base, Owned):
    """Money taken off a category's budget by hand."""

    __tablename__ = 'budget_spends'
    __table_args__ = (Index('ix_budget_spends_user_date', 'user_id', 'date'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'))
    category_id: Mapped[int] = mapped_column(ForeignKey('categories.id', ondelete='CASCADE'))
    date: Mapped[date] = mapped_column(Date)
    amount_cents: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(120), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime)


class Goal(Base, Owned):
    """Money set aside for something (a holiday, an emergency fund), usually
    inside one of the person's accounts. What it holds is its moves added up."""

    __tablename__ = 'goals'

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    name: Mapped[str] = mapped_column(String(80))
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey('accounts.id', ondelete='SET NULL'), nullable=True
    )
    # Both optional: a goal without them is a bucket with no end.
    target_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Its money is not free to spend: the budget leaves it out of the accounts.
    keep_aside: Mapped[bool] = mapped_column(Boolean, default=True)
    # Done: kept for its history, no longer holding money aside.
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    # Holds the person's tax money: counted as set aside for tax. One per person.
    is_tax: Mapped[bool] = mapped_column(Boolean, default=False)
    # Saved from this budget line: putting money aside counts as spent on it.
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey('categories.id', ondelete='SET NULL'), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime)

    moves: Mapped[list[GoalMove]] = relationship(
        back_populates='goal', cascade='all, delete-orphan', order_by='GoalMove.date'
    )


class GoalMove(Base, Owned):
    """Money put into a goal (positive) or taken out (negative), by hand."""

    __tablename__ = 'goal_moves'
    __table_args__ = (Index('ix_goal_moves_user_date', 'user_id', 'date'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'))
    goal_id: Mapped[int] = mapped_column(ForeignKey('goals.id', ondelete='CASCADE'), index=True)
    date: Mapped[date] = mapped_column(Date)
    amount_cents: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(120), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime)
    # The budget spend recorded with it, when put aside from a budget line.
    # Removing either removes both (`goals.remove_move`, `goals.remove_spend`).
    spend_id: Mapped[int | None] = mapped_column(
        ForeignKey('budget_spends.id', ondelete='SET NULL'), nullable=True
    )

    goal: Mapped[Goal] = relationship(back_populates='moves')


class TaxDeduction(Base, Owned):
    """Total deductions a person claims for one financial year, typed in."""

    __tablename__ = 'tax_deductions'
    __table_args__ = (UniqueConstraint('user_id', 'year'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'))
    # Financial year label, "2026-27".
    year: Mapped[str] = mapped_column(String(7))
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)


class Scenario(Base, Owned):
    __tablename__ = 'scenarios'

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    name: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime)

    adjustments: Mapped[list[ScenarioAdjustment]] = relationship(
        back_populates='scenario', cascade='all, delete-orphan', order_by='ScenarioAdjustment.id'
    )


class ScenarioAdjustment(Base, Owned):
    __tablename__ = 'scenario_adjustments'

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    scenario_id: Mapped[int] = mapped_column(
        ForeignKey('scenarios.id', ondelete='CASCADE'), index=True
    )
    kind: Mapped[str] = mapped_column(String(30))
    params: Mapped[dict[str, Any]] = mapped_column(JSON)

    scenario: Mapped[Scenario] = relationship(back_populates='adjustments')


class ForecastCache(Base):
    __tablename__ = 'forecast_cache'

    user_id: Mapped[int] = mapped_column(
        ForeignKey('users.id', ondelete='CASCADE'), primary_key=True
    )
    cache_key: Mapped[str] = mapped_column(String(120))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class NotificationLog(Base):
    __tablename__ = 'notification_log'
    __table_args__ = (UniqueConstraint('user_id', 'kind', 'key'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    key: Mapped[str] = mapped_column(String(120))
    sent_at: Mapped[datetime] = mapped_column(DateTime)
