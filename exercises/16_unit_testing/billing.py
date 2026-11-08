"""Billing core: invoice totals, payment charging, receipts, due dates and CSV export.

This is production code under test. Do not edit it: the exercise is the test
suite you write in main.py.
"""

from __future__ import annotations

import csv
import logging
import os
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")
DEFAULT_NET_DAYS = 30
EXPORT_FIELDS = (
    "invoice_id",
    "customer_email",
    "issued_on",
    "currency",
    "subtotal",
    "discount",
    "tax",
    "total",
)


class BillingError(Exception):
    """Base class for all billing failures."""


class InvalidInvoice(BillingError, ValueError):
    """An invoice, line item or input violates a business rule."""


class TransientGatewayError(BillingError):
    """The gateway failed in a way that is safe to retry (timeout, 503)."""


class PaymentDeclined(BillingError):
    """The issuer refused the charge. Never retried."""


class PaymentFailed(BillingError):
    """Every attempt failed with a transient error."""


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def round_money(amount: Decimal) -> Decimal:
    """Round to whole cents, ties away from zero (finance's rule)."""
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def discount_rate(subtotal: Decimal) -> Decimal:
    """Volume discount: 10% from 1000, 5% from 500, 2% from 100; boundaries inclusive."""
    if subtotal >= Decimal("1000"):
        return Decimal("0.10")
    if subtotal >= Decimal("500"):
        return Decimal("0.05")
    if subtotal >= Decimal("100"):
        return Decimal("0.02")
    return ZERO


@dataclass(frozen=True, slots=True)
class LineItem:
    """One priced line. The unit price may carry more than two decimal places."""

    sku: str
    unit_price: Decimal
    quantity: int

    def __post_init__(self) -> None:
        if not self.sku:
            raise InvalidInvoice("sku must be non-empty")
        if self.unit_price < ZERO:
            raise InvalidInvoice(f"unit price for {self.sku} must not be negative")
        if self.quantity < 1:
            raise InvalidInvoice(f"quantity for {self.sku} must be >= 1, got {self.quantity}")

    @property
    def line_total(self) -> Decimal:
        return round_money(self.unit_price * self.quantity)


@dataclass(frozen=True, slots=True)
class Invoice:
    invoice_id: str
    customer_email: str
    lines: tuple[LineItem, ...]
    issued_on: date
    tax_rate: Decimal = ZERO
    currency: str = "USD"

    def __post_init__(self) -> None:
        if not self.lines:
            raise InvalidInvoice(f"invoice {self.invoice_id} has no line items")
        if not ZERO <= self.tax_rate < ONE:
            raise InvalidInvoice(f"tax rate must be in [0, 1), got {self.tax_rate}")


@dataclass(frozen=True, slots=True)
class Totals:
    subtotal: Decimal
    discount: Decimal
    tax: Decimal
    total: Decimal


def invoice_totals(invoice: Invoice) -> Totals:
    """Discount comes from the subtotal tier; tax is charged on the discounted amount."""
    subtotal = sum((line.line_total for line in invoice.lines), ZERO)
    discount = round_money(subtotal * discount_rate(subtotal))
    taxable = subtotal - discount
    tax = round_money(taxable * invoice.tax_rate)
    return Totals(subtotal=subtotal, discount=discount, tax=tax, total=taxable + tax)


def due_date(issued_on: date, net_days: int = DEFAULT_NET_DAYS) -> date:
    """Due date for an invoice issued on `issued_on` with `net_days` payment terms."""
    if net_days < 0:
        raise ValueError(f"net_days must be >= 0, got {net_days}")
    return issued_on + timedelta(days=net_days)


def issue_due_date(net_days: int = DEFAULT_NET_DAYS) -> date:
    """Due date for an invoice issued today, where 'today' is the UTC calendar date."""
    return due_date(utcnow().date(), net_days)


def is_overdue(invoice_due: date) -> bool:
    """True only strictly after the due date; an invoice is not overdue on its due day."""
    return utcnow().date() > invoice_due


class Gateway(Protocol):
    """Payment gateway port. Returns a transaction id on success."""

    def charge(self, *, amount: Decimal, currency: str, token: str, idempotency_key: str) -> str: ...


class Notifier(Protocol):
    """Outbound notification port (email, SMS, ...)."""

    def send(self, *, to: str, subject: str, body: str) -> None: ...


@dataclass(slots=True)
class PaymentService:
    """Charges invoices through an injected gateway, retrying transient failures.

    `max_attempts` counts the first try. Between attempts the service calls
    time.sleep(backoff_delay(n)), where n is the 1-based number of the attempt
    that just failed.
    """

    gateway: Gateway
    notifier: Notifier
    max_attempts: int = 4
    base_delay: float = 1.0
    max_delay: float = 8.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {self.max_attempts}")
        if self.base_delay < 0 or self.max_delay < 0:
            raise ValueError("delays must not be negative")

    def backoff_delay(self, attempt: int) -> float:
        """Seconds to wait after failed attempt `attempt`: base * 2**(attempt-1), capped at max_delay."""
        return min(self.base_delay * 2 ** (attempt - 1), self.max_delay)

    def charge(self, invoice: Invoice, token: str) -> str:
        """Charge the invoice total and return the gateway transaction id.

        Raises InvalidInvoice for a non-positive total (before any gateway call),
        PaymentDeclined immediately on decline, and PaymentFailed when transient
        errors exhaust all attempts. A receipt is sent after success; receipt
        failures are logged and never raised.
        """
        totals = invoice_totals(invoice)
        if totals.total <= ZERO:
            raise InvalidInvoice(f"invoice {invoice.invoice_id} total must be positive, got {totals.total}")
        for attempt in range(1, self.max_attempts + 1):
            try:
                txn_id = self.gateway.charge(
                    amount=totals.total,
                    currency=invoice.currency,
                    token=token,
                    idempotency_key=invoice.invoice_id,
                )
            except PaymentDeclined:
                logger.error("payment declined for invoice %s", invoice.invoice_id)
                raise
            except TransientGatewayError as exc:
                logger.warning(
                    "transient gateway error for invoice %s (attempt %d of %d): %s",
                    invoice.invoice_id,
                    attempt,
                    self.max_attempts,
                    exc,
                )
                if attempt == self.max_attempts:
                    logger.error("giving up on invoice %s after %d attempts", invoice.invoice_id, attempt)
                    raise PaymentFailed(
                        f"invoice {invoice.invoice_id}: gateway unavailable after {attempt} attempts"
                    ) from exc
                time.sleep(self.backoff_delay(attempt))
            else:
                self._send_receipt(invoice, totals, txn_id)
                return txn_id
        raise AssertionError("unreachable: the loop always returns or raises")

    def _send_receipt(self, invoice: Invoice, totals: Totals, txn_id: str) -> None:
        body = (
            f"Invoice {invoice.invoice_id}\n"
            f"Total: {totals.total} {invoice.currency}\n"
            f"Transaction: {txn_id}\n"
        )
        try:
            self.notifier.send(
                to=invoice.customer_email,
                subject=f"Receipt for invoice {invoice.invoice_id}",
                body=body,
            )
        except Exception:
            # The money has moved; a failed email must not turn a settled charge into an error.
            logger.exception(
                "receipt delivery failed for invoice %s (txn %s)", invoice.invoice_id, txn_id
            )


def export_invoices_csv(invoices: Iterable[Invoice], path: Path) -> int:
    """Write one CSV row per invoice and return the row count.

    Money is written as fixed two-decimal strings. Rows are assembled in a temp
    file next to `path` and moved into place with os.replace, so a failure part
    way through leaves any previous export untouched and no temp file behind.
    """
    path = Path(path)
    rows = 0
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=EXPORT_FIELDS)
            writer.writeheader()
            for invoice in invoices:
                totals = invoice_totals(invoice)
                writer.writerow(
                    {
                        "invoice_id": invoice.invoice_id,
                        "customer_email": invoice.customer_email,
                        "issued_on": invoice.issued_on.isoformat(),
                        "currency": invoice.currency,
                        "subtotal": _money(totals.subtotal),
                        "discount": _money(totals.discount),
                        "tax": _money(totals.tax),
                        "total": _money(totals.total),
                    }
                )
                rows += 1
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return rows


def _money(amount: Decimal) -> str:
    return format(amount, ".2f")
