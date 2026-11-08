"""Test suite for billing.py: parametrized boundaries, autospec doubles, patched clock and sleep, caplog, tmp_path, Hypothesis."""

from __future__ import annotations

import csv
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import call, create_autospec, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import billing
from billing import (
    EXPORT_FIELDS,
    Gateway,
    InvalidInvoice,
    Invoice,
    LineItem,
    Notifier,
    PaymentDeclined,
    PaymentFailed,
    PaymentService,
    TransientGatewayError,
    discount_rate,
    due_date,
    export_invoices_csv,
    invoice_totals,
    is_overdue,
    issue_due_date,
    utcnow,
)

D = Decimal


def make_line(price: str = "10.00", qty: int = 1, sku: str = "SKU-1") -> LineItem:
    return LineItem(sku=sku, unit_price=D(price), quantity=qty)


def make_invoice(
    lines: list[LineItem] | None = None,
    *,
    invoice_id: str = "INV-1",
    tax_rate: str = "0",
    issued_on: date = date(2026, 1, 15),
) -> Invoice:
    return Invoice(
        invoice_id=invoice_id,
        customer_email="ada@example.com",
        lines=tuple(lines if lines is not None else [make_line()]),
        issued_on=issued_on,
        tax_rate=D(tax_rate),
    )


def pricey_invoice(invoice_id: str = "INV-1") -> Invoice:
    """Subtotal 600 -> 5% off -> 570 taxed at 10%: discount and tax both matter."""
    return make_invoice([make_line("600.00", 1)], tax_rate="0.10", invoice_id=invoice_id)


@pytest.fixture
def gateway() -> Gateway:
    fake = create_autospec(Gateway, instance=True)
    fake.charge.return_value = "txn_123"
    return fake


@pytest.fixture
def notifier() -> Notifier:
    return create_autospec(Notifier, instance=True)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record backoff sleeps instead of blocking. monkeypatch restores time.sleep."""
    recorded: list[float] = []
    monkeypatch.setattr(billing.time, "sleep", recorded.append)
    return recorded


@pytest.fixture
def set_clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[datetime], None]:
    def _set(now: datetime) -> None:
        monkeypatch.setattr(billing, "utcnow", lambda: now)

    return _set


@pytest.fixture
def service(gateway: Gateway, notifier: Notifier, sleeps: list[float]) -> PaymentService:
    return PaymentService(gateway=gateway, notifier=notifier)


class TestPart1LineTotalsAndInvoiceTotals:
    @pytest.mark.parametrize(
        ("price", "qty", "expected"),
        [("0.125", 1, "0.13"), ("0.135", 1, "0.14"), ("0.333", 3, "1.00"), ("19.99", 3, "59.97")],
        ids=["half-up-down", "half-up-up", "round-after-multiply", "plain"],
    )
    def test_line_total_rounds_half_up_to_cents(self, price: str, qty: int, expected: str) -> None:
        assert make_line(price, qty).line_total == D(expected), f"{price} x {qty} should round to {expected}"

    @pytest.mark.parametrize("qty", [0, -2])
    def test_zero_or_negative_quantity_is_rejected(self, qty: int) -> None:
        with pytest.raises(InvalidInvoice, match="quantity"):
            make_line(qty=qty)

    def test_negative_unit_price_is_rejected(self) -> None:
        with pytest.raises(InvalidInvoice, match="negative"):
            make_line(price="-0.01")

    def test_zero_price_line_is_allowed(self) -> None:
        assert make_line(price="0").line_total == D("0.00")

    def test_totals_without_discount_or_tax(self) -> None:
        totals = invoice_totals(make_invoice([make_line("40.00", 2)]))
        assert totals.subtotal == D("80.00")
        assert totals.discount == D("0.00")
        assert totals.tax == D("0.00")
        assert totals.total == D("80.00")

    def test_tax_applies_without_discount(self) -> None:
        totals = invoice_totals(make_invoice([make_line("80.00")], tax_rate="0.10"))
        assert totals.tax == D("8.00")
        assert totals.total == D("88.00")

    def test_discount_reduces_the_taxable_base(self) -> None:
        totals = invoice_totals(pricey_invoice())
        observed = (totals.subtotal, totals.discount, totals.tax, totals.total)
        assert observed == (D("600.00"), D("30.00"), D("57.00"), D("627.00")), (
            "expected subtotal 600, 5% discount 30, tax 10% of 570 = 57, total 627"
        )

    @pytest.mark.parametrize("rate", ["-0.01", "1", "1.5"])
    def test_tax_rate_outside_zero_to_one_is_rejected(self, rate: str) -> None:
        with pytest.raises(InvalidInvoice, match="tax rate"):
            make_invoice(tax_rate=rate)

    def test_invoice_without_lines_is_rejected(self) -> None:
        with pytest.raises(InvalidInvoice, match="no line items"):
            make_invoice([])

    @settings(derandomize=True, max_examples=200, deadline=None)
    @given(
        cart=st.lists(
            st.tuples(
                st.decimals(min_value=D("0"), max_value=D("400"), places=2),
                st.integers(min_value=1, max_value=20),
            ),
            min_size=1,
            max_size=8,
        ),
        tax_pct=st.integers(min_value=0, max_value=99),
    )
    def test_totals_invariants_hold_for_any_cart(self, cart, tax_pct: int) -> None:
        lines = tuple(LineItem(f"SKU-{i}", price, qty) for i, (price, qty) in enumerate(cart))
        invoice = Invoice("INV-P", "x@example.com", lines, date(2026, 1, 1), tax_rate=D(tax_pct) / 100)
        totals = invoice_totals(invoice)
        assert totals.total == totals.subtotal - totals.discount + totals.tax
        assert totals.total >= 0
        if totals.subtotal < 100:
            assert totals.discount == 0, "no discount below the 100 tier"


class TestPart2VolumeDiscountTiers:
    @pytest.mark.parametrize(
        ("subtotal", "rate"),
        [
            ("0", "0"),
            ("99.99", "0"),
            ("100", "0.02"),
            ("499.99", "0.02"),
            ("500", "0.05"),
            ("999.99", "0.05"),
            ("1000", "0.10"),
            ("5000", "0.10"),
        ],
    )
    def test_rate_for_subtotal(self, subtotal: str, rate: str) -> None:
        assert discount_rate(D(subtotal)) == D(rate), f"subtotal {subtotal} should get rate {rate}"

    def test_discount_applies_at_exact_threshold(self) -> None:
        totals = invoice_totals(make_invoice([make_line("500.00")]))
        assert totals.discount == D("25.00")
        assert totals.total == D("475.00")


class TestPart3ClockAndDueDates:
    def test_utcnow_is_timezone_aware_utc(self) -> None:
        assert utcnow().utcoffset() == timedelta(0), "utcnow() must return an aware UTC datetime"

    def test_utcnow_tracks_real_time(self) -> None:
        before = datetime.now(UTC)
        now = utcnow()
        after = datetime.now(UTC)
        assert before <= now <= after

    def test_due_date_adds_net_days(self) -> None:
        assert due_date(date(2026, 1, 31), net_days=30) == date(2026, 3, 2)

    def test_due_date_defaults_to_thirty_days(self) -> None:
        assert due_date(date(2026, 1, 1)) == date(2026, 1, 31)

    def test_negative_net_days_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="net_days"):
            due_date(date(2026, 1, 1), net_days=-1)

    def test_issue_due_date_uses_utc_calendar_day(self, set_clock: Callable[[datetime], None]) -> None:
        set_clock(datetime(2026, 12, 31, 23, 59, tzinfo=UTC))
        assert issue_due_date(net_days=1) == date(2027, 1, 1)

    @pytest.mark.parametrize(
        ("now", "overdue"),
        [
            (datetime(2026, 3, 2, 23, 59, tzinfo=UTC), False),
            (datetime(2026, 3, 3, 0, 0, tzinfo=UTC), True),
        ],
        ids=["due-day-not-overdue", "day-after-overdue"],
    )
    def test_overdue_only_after_the_due_day(
        self, set_clock: Callable[[datetime], None], now: datetime, overdue: bool
    ) -> None:
        set_clock(now)
        assert is_overdue(date(2026, 3, 2)) is overdue


class TestPart4ChargeAndRetries:
    def test_charge_sends_total_currency_and_idempotency_key(
        self, service: PaymentService, gateway: Gateway
    ) -> None:
        txn = service.charge(pricey_invoice(), token="tok_visa")
        assert txn == "txn_123"
        gateway.charge.assert_called_once_with(
            amount=D("627.00"), currency="USD", token="tok_visa", idempotency_key="INV-1"
        )

    def test_declined_payment_is_not_retried(
        self, service: PaymentService, gateway: Gateway, sleeps: list[float], notifier: Notifier
    ) -> None:
        gateway.charge.side_effect = PaymentDeclined("card declined")
        with pytest.raises(PaymentDeclined, match="card declined"):
            service.charge(make_invoice(), token="tok")
        assert gateway.charge.call_count == 1
        assert sleeps == []
        notifier.send.assert_not_called()

    def test_transient_error_then_success_sleeps_once(
        self, service: PaymentService, gateway: Gateway, sleeps: list[float]
    ) -> None:
        gateway.charge.side_effect = [TransientGatewayError("503"), "txn_9"]
        assert service.charge(make_invoice(), token="tok") == "txn_9"
        assert gateway.charge.call_count == 2
        assert sleeps == [1.0]

    def test_exhausted_retries_raise_payment_failed_with_backoff_schedule(
        self, service: PaymentService, gateway: Gateway, sleeps: list[float]
    ) -> None:
        gateway.charge.side_effect = TransientGatewayError("timeout")
        with pytest.raises(PaymentFailed, match="after 4 attempts") as info:
            service.charge(make_invoice(), token="tok")
        assert gateway.charge.call_count == 4
        assert sleeps == [1.0, 2.0, 4.0], "no sleep after the final attempt"
        assert isinstance(info.value.__cause__, TransientGatewayError)

    def test_backoff_calls_time_sleep_with_each_delay(self, gateway: Gateway, notifier: Notifier) -> None:
        service = PaymentService(gateway=gateway, notifier=notifier, base_delay=0.5, max_delay=60)
        gateway.charge.side_effect = [TransientGatewayError("a"), TransientGatewayError("b"), "txn"]
        with patch.object(billing.time, "sleep") as sleep_mock:
            service.charge(make_invoice(), token="tok")
        assert sleep_mock.call_args_list == [call(0.5), call(1.0)]

    @pytest.mark.parametrize(
        ("attempt", "delay"),
        [(1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0), (5, 8.0), (9, 8.0)],
    )
    def test_backoff_doubles_then_caps(self, service: PaymentService, attempt: int, delay: float) -> None:
        assert service.backoff_delay(attempt) == delay

    def test_single_attempt_policy_never_retries(
        self, gateway: Gateway, notifier: Notifier, sleeps: list[float]
    ) -> None:
        service = PaymentService(gateway=gateway, notifier=notifier, max_attempts=1)
        gateway.charge.side_effect = TransientGatewayError("503")
        with pytest.raises(PaymentFailed):
            service.charge(make_invoice(), token="tok")
        assert gateway.charge.call_count == 1
        assert sleeps == []

    @pytest.mark.parametrize("kwargs", [{"max_attempts": 0}, {"base_delay": -1.0}], ids=["zero-attempts", "negative-delay"])
    def test_invalid_retry_policy_is_rejected(self, gateway: Gateway, notifier: Notifier, kwargs: dict) -> None:
        with pytest.raises(ValueError):
            PaymentService(gateway=gateway, notifier=notifier, **kwargs)

    def test_unexpected_gateway_error_is_not_retried(
        self, service: PaymentService, gateway: Gateway, sleeps: list[float]
    ) -> None:
        gateway.charge.side_effect = RuntimeError("bug in adapter")
        with pytest.raises(RuntimeError, match="bug in adapter"):
            service.charge(make_invoice(), token="tok")
        assert gateway.charge.call_count == 1
        assert sleeps == []

    def test_zero_total_invoice_never_reaches_gateway(self, service: PaymentService, gateway: Gateway) -> None:
        with pytest.raises(InvalidInvoice, match="must be positive"):
            service.charge(make_invoice([make_line("0")]), token="tok")
        gateway.charge.assert_not_called()


class TestPart5ReceiptsAndLogging:
    def test_receipt_goes_to_customer_after_success(self, service: PaymentService, notifier: Notifier) -> None:
        service.charge(pricey_invoice(), token="tok")
        notifier.send.assert_called_once()
        kwargs = notifier.send.call_args.kwargs
        assert kwargs["to"] == "ada@example.com"
        assert kwargs["subject"] == "Receipt for invoice INV-1"
        assert "Total: 627.00 USD" in kwargs["body"]
        assert "txn_123" in kwargs["body"]

    def test_no_receipt_when_charge_is_declined(
        self, service: PaymentService, gateway: Gateway, notifier: Notifier
    ) -> None:
        gateway.charge.side_effect = PaymentDeclined("nope")
        with pytest.raises(PaymentDeclined):
            service.charge(make_invoice(), token="tok")
        notifier.send.assert_not_called()

    def test_receipt_failure_does_not_fail_a_settled_charge(
        self, service: PaymentService, notifier: Notifier, caplog: pytest.LogCaptureFixture
    ) -> None:
        notifier.send.side_effect = ConnectionError("smtp unavailable")
        with caplog.at_level(logging.ERROR, logger="billing"):
            txn = service.charge(make_invoice(), token="tok")
        assert txn == "txn_123"
        failures = [
            r for r in caplog.records if r.levelno == logging.ERROR and "receipt delivery failed" in r.getMessage()
        ]
        assert len(failures) == 1, "receipt failure must be logged once at ERROR"
        assert failures[0].exc_info is not None, "traceback must be attached"

    def test_retry_warning_names_the_attempt(
        self, service: PaymentService, gateway: Gateway, sleeps: list[float], caplog: pytest.LogCaptureFixture
    ) -> None:
        gateway.charge.side_effect = [TransientGatewayError("503"), "txn_9"]
        with caplog.at_level(logging.WARNING, logger="billing"):
            service.charge(make_invoice(invoice_id="INV-9"), token="tok")
        messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(messages) == 1
        assert "INV-9" in messages[0]
        assert "attempt 1 of 4" in messages[0]

    def test_decline_is_logged_at_error(
        self, service: PaymentService, gateway: Gateway, caplog: pytest.LogCaptureFixture
    ) -> None:
        gateway.charge.side_effect = PaymentDeclined("nope")
        with caplog.at_level(logging.ERROR, logger="billing"), pytest.raises(PaymentDeclined):
            service.charge(make_invoice(), token="tok")
        assert any("declined" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)


class TestPart6CsvExport:
    def test_writes_header_and_one_row_per_invoice(self, tmp_path) -> None:
        path = tmp_path / "invoices.csv"
        second = make_invoice([make_line("5")], invoice_id="INV-2", issued_on=date(2026, 2, 1))
        assert export_invoices_csv([pricey_invoice("INV-1"), second], path) == 2
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            assert reader.fieldnames == list(EXPORT_FIELDS)
            records = list(reader)
        assert records[0]["invoice_id"] == "INV-1"
        assert records[0]["subtotal"] == "600.00"
        assert records[0]["discount"] == "30.00"
        assert records[0]["tax"] == "57.00"
        assert records[0]["total"] == "627.00"
        assert records[0]["issued_on"] == "2026-01-15"
        assert records[1]["invoice_id"] == "INV-2"
        assert records[1]["total"] == "5.00"
        assert records[1]["issued_on"] == "2026-02-01"

    def test_empty_export_writes_header_only(self, tmp_path) -> None:
        path = tmp_path / "empty.csv"
        assert export_invoices_csv([], path) == 0
        assert path.read_text(encoding="utf-8").splitlines() == [",".join(EXPORT_FIELDS)]

    def test_failure_mid_export_keeps_previous_file_and_leaves_no_temp_files(self, tmp_path) -> None:
        path = tmp_path / "invoices.csv"
        path.write_text("previous export\n", encoding="utf-8")

        def invoices() -> Iterator[Invoice]:
            yield pricey_invoice()
            raise RuntimeError("database connection lost")

        with pytest.raises(RuntimeError, match="connection lost"):
            export_invoices_csv(invoices(), path)
        assert path.read_text(encoding="utf-8") == "previous export\n"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["invoices.csv"]

    def test_overwrite_replaces_previous_export(self, tmp_path) -> None:
        path = tmp_path / "invoices.csv"
        export_invoices_csv([pricey_invoice()], path)
        export_invoices_csv([], path)
        assert path.read_text(encoding="utf-8").splitlines() == [",".join(EXPORT_FIELDS)]
