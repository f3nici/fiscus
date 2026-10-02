"""Square payments, refunds, payouts and invoices as ledger rows.

The Square balance is an account like any other. A completed payment is money
in, with its processing fee as a separate row so income stays gross for tax.
A refund is money out, less any fee Square returns. A payout is the balance
moving to the bank, so it is money out here and a deposit in the bank's own
import; categorise both as a transfer.

Each row carries a stable key from Square's ids, so a sync can be repeated
over overlapping windows without duplicates. Amounts are Square's own integer
minor units, which for AUD are cents. Pure: takes parsed JSON, returns values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

CURRENCY = 'AUD'
OPEN_INVOICE = {'UNPAID', 'SCHEDULED', 'PARTIALLY_PAID', 'PAYMENT_PENDING'}


@dataclass(frozen=True)
class Row:
    key: str
    kind: str  # income | fee | refund | payout
    date: date
    amount_cents: int
    description: str


@dataclass(frozen=True)
class OpenInvoice:
    square_id: str
    number: str
    customer: str
    due: date
    amount_due_cents: int
    status: str


def local_date(stamp: str, tz: ZoneInfo) -> date:
    """An RFC 3339 timestamp as a date where the household is."""
    return datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone(tz).date()


def _money(obj: Any) -> int | None:
    """Cents, or None when missing or not in AUD."""
    if not isinstance(obj, dict) or obj.get('currency') != CURRENCY:
        return None
    amount = obj.get('amount')
    return amount if isinstance(amount, int) else None


def _fees(obj: dict[str, Any]) -> int:
    return sum(_money(f.get('amount_money')) or 0 for f in obj.get('processing_fee') or [])


def customer_name(recipient: Any) -> str:
    if not isinstance(recipient, dict):
        return ''
    person = ' '.join(
        p for p in (recipient.get('given_name'), recipient.get('family_name')) if p
    ).strip()
    return str(recipient.get('company_name') or person or recipient.get('email_address') or '')


def invoice_labels(invoices: list[dict[str, Any]]) -> dict[str, str]:
    """Order id to "Invoice inv-100 · Customer", to name the payments against them."""
    out: dict[str, str] = {}
    for inv in invoices:
        order = inv.get('order_id')
        if not order:
            continue
        label = f'Invoice {inv.get("invoice_number") or inv.get("id", "")}'.strip()
        who = customer_name(inv.get('primary_recipient'))
        out[order] = f'{label} · {who}' if who else label
    return out


def payment_rows(payment: dict[str, Any], tz: ZoneInfo, labels: dict[str, str]) -> list[Row]:
    if payment.get('status') != 'COMPLETED':
        return []
    total = _money(payment.get('total_money'))
    if total is None or not payment.get('id') or not payment.get('created_at'):
        return []
    pid = payment['id']
    when = local_date(payment['created_at'], tz)
    what = labels.get(payment.get('order_id') or '') or (payment.get('note') or '').strip()
    receipt = payment.get('receipt_number') or pid[:8]
    desc = f'Square payment {receipt}' + (f' · {what}' if what else '')
    rows = [Row(f'payment:{pid}', 'income', when, total, desc[:500])]
    fee = _fees(payment)
    if fee:
        rows.append(Row(f'fee:{pid}', 'fee', when, -fee, f'Square fee {receipt}'))
    return rows


def refund_rows(refund: dict[str, Any], tz: ZoneInfo) -> list[Row]:
    if refund.get('status') != 'COMPLETED':
        return []
    amount = _money(refund.get('amount_money'))
    if amount is None or not refund.get('id') or not refund.get('created_at'):
        return []
    rid = refund['id']
    when = local_date(refund['created_at'], tz)
    rows = [Row(f'refund:{rid}', 'refund', when, -amount, f'Square refund {rid[:8]}')]
    # Square reports a returned fee as a negative fee on the refund.
    fee = _fees(refund)
    if fee:
        rows.append(Row(f'refund-fee:{rid}', 'fee', when, -fee, f'Square fee {rid[:8]}'))
    return rows


def payout_rows(payout: dict[str, Any], tz: ZoneInfo) -> list[Row]:
    if payout.get('status') != 'PAID':
        return []
    amount = _money(payout.get('amount_money'))
    if amount is None or not payout.get('id'):
        return []
    if payout.get('arrival_date'):
        when = date.fromisoformat(payout['arrival_date'])
    elif payout.get('created_at'):
        when = local_date(payout['created_at'], tz)
    else:
        return []
    # A negative payout is Square taking money back from the bank.
    return [Row(f'payout:{payout["id"]}', 'payout', when, -amount, 'Square payout to bank')]


def open_invoices(invoices: list[dict[str, Any]]) -> list[OpenInvoice]:
    """Invoices still to be paid, one line per payment request with money owing."""
    out: list[OpenInvoice] = []
    for inv in invoices:
        if inv.get('status') not in OPEN_INVOICE:
            continue
        number = str(inv.get('invoice_number') or inv.get('id', ''))
        who = customer_name(inv.get('primary_recipient'))
        for i, req in enumerate(inv.get('payment_requests') or []):
            due = req.get('due_date')
            owed = _money(req.get('computed_amount_money'))
            paid = _money(req.get('total_completed_amount_money')) or 0
            if not due or owed is None or owed - paid <= 0:
                continue
            out.append(
                OpenInvoice(
                    square_id=f'{inv.get("id", "")}:{i}',
                    number=number,
                    customer=who,
                    due=date.fromisoformat(due),
                    amount_due_cents=owed - paid,
                    status=str(inv.get('status')),
                )
            )
    return out
