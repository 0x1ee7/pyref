# 16 · Unit testing: write the suite

## Scenario

`billing.py` is the production module of a small billing service. It computes invoice totals (Decimal, half-up cents, volume tiers, tax on the discounted amount), charges invoices through an injected payment gateway with retries and exponential backoff, sends receipts through an injected notifier, computes due dates from a module clock, and exports invoices to CSV atomically.

This exercise is inverted: the production code is provided and **you write the tests**. Your deliverable is `main.py`, a pytest suite that `import billing`. Do not edit `billing.py`.

## How grading works

`test_unit_testing.py` copies your `main.py` into a scratch directory as `test_student.py`, next to a copy of `billing.py`, and runs pytest on it in a fresh process. It checks:

1. **Correct code passes.** The suite passes against the unmodified `billing.py` and contains at least 30 tests.
2. **Bugs get caught.** Around twenty deliberately broken variants of `billing.py` are run against your suite. Each must make it fail with test failures (exit code 1). An empty suite, an import error, or a hang does not count as catching a bug.
3. **It is fast.** The suite finishes in under 10 seconds. Real retry backoff adds up to several seconds per exhausted retry, so the sleep must be patched.

The injected bugs fall into categories: rounding, boundaries, operation ordering, validation, clock and timezone, retry policy and backoff math, retry classification, gateway call arguments, post-charge side effects, error isolation, logging, and file atomicity. Some are subtle. For example, a bug may be visible only if your test checks the exact log level or the timezone of the clock.

## The API under test

### Part 1: Line totals and invoice totals

- `round_money(amount: Decimal) -> Decimal`: round to cents, ties away from zero.
- `LineItem(sku, unit_price, quantity)`, a frozen dataclass.
  - `.line_total` is `round_money(unit_price * quantity)`.
  - An empty sku, a negative unit price, or `quantity < 1` raises `InvalidInvoice`.
- `Invoice(invoice_id, customer_email, lines, issued_on, tax_rate=Decimal("0"), currency="USD")`.
  - No lines raises `InvalidInvoice`.
  - `tax_rate` outside `[0, 1)` raises `InvalidInvoice`.
- `invoice_totals(invoice) -> Totals(subtotal, discount, tax, total)`.
  - `subtotal` is the sum of line totals.
  - `discount` is `round_money(subtotal * discount_rate(subtotal))`.
  - `tax` is `round_money((subtotal - discount) * tax_rate)`.
  - `total` is `subtotal - discount + tax`.
- Property check: for any cart, `total == subtotal - discount + tax`, and `total >= 0`.

### Part 2: Volume discount tiers

- `discount_rate(subtotal: Decimal) -> Decimal`.
  - At least 1000: `0.10`.
  - At least 500: `0.05`.
  - At least 100: `0.02`.
  - Otherwise: `0`.
  - Boundaries are inclusive.

### Part 3: Clock and due dates

- `utcnow() -> datetime`: the current time, timezone-aware UTC. It is a module-level function, so tests replace it with `monkeypatch`.
- `due_date(issued_on: date, net_days: int = 30) -> date`: `ValueError` when `net_days < 0`.
- `issue_due_date(net_days: int = 30) -> date`: the due date for an invoice issued on the UTC calendar day of `utcnow()`.
- `is_overdue(invoice_due: date) -> bool`: True only strictly after the due day, judged by the UTC date of `utcnow()`.

### Part 4: Charging and retries

- `Gateway` protocol: `charge(*, amount, currency, token, idempotency_key) -> str` returns a transaction id. It raises `TransientGatewayError` (retryable) or `PaymentDeclined` (terminal).
- `PaymentService(gateway, notifier, max_attempts=4, base_delay=1.0, max_delay=8.0)`.
  - `max_attempts < 1` or a negative delay raises `ValueError`.
  - `backoff_delay(attempt: int) -> float` returns `min(base_delay * 2 ** (attempt - 1), max_delay)`. Attempts are 1-based.
- `charge(invoice, token) -> str`.
  - Calls `gateway.charge` with `amount` = invoice total, `currency` = invoice currency, `idempotency_key` = invoice id.
  - A total of zero or less raises `InvalidInvoice` before any gateway call.
  - After each transient failure except the last, it calls `time.sleep(backoff_delay(attempt))`. There is no sleep after the final attempt.
  - When all attempts are exhausted it raises `PaymentFailed`, chained from the last transient error.
  - `PaymentDeclined` is raised immediately with no retry.
  - Any other exception propagates without retry.
  - Returns the transaction id on success.

### Part 5: Receipts and logging

- After a successful charge, `notifier.send(to=customer_email, subject="Receipt for invoice <id>", body=...)` is called once. The body contains `Total: <total> <currency>` and the transaction id.
- A receipt failure never fails the charge. The transaction id is still returned, and the failure is logged at `ERROR` with the traceback attached.
- Each transient failure is logged at `WARNING` with the message `... (attempt N of M): ...`.
- A decline is logged at `ERROR`.

### Part 6: CSV export

- `export_invoices_csv(invoices: Iterable[Invoice], path: Path) -> int` returns the row count.
  - The header is `EXPORT_FIELDS`, one row per invoice.
  - Money is a two-decimal string such as `"5.00"`. `issued_on` is ISO format.
  - The write is atomic: rows go to a temp file in the same directory, which is then `os.replace`d into place.
  - If the invoice iterable raises part way through, the previous file is untouched and no temp file is left behind. The exception propagates.
  - An empty iterable writes a header-only file and returns `0`.

## Test-suite rules

- Every test is independent, with no ordering dependence.
- Never really sleep. Patch `time.sleep`, for example through `monkeypatch` or `unittest.mock.patch`.
- Never depend on the wall clock. Patch `utcnow`.
- No network, and no leaked global state: restore environment variables, cwd, logging levels, and module attributes.
- Deterministic. Hypothesis is allowed, with `derandomize=True`.

## Concepts exercised

- Test organisation: classes per part, `parametrize` with clear `ids`, `pytest.raises(..., match=...)`.
- Boundary-value analysis and property-based testing with Hypothesis.
- Test doubles: `create_autospec` for protocol ports, `side_effect` sequences, `assert_called_once_with`, `call_args_list`, `patch.object`.
- Fixtures: `monkeypatch` for the clock and sleep, `tmp_path` for file I/O, `caplog` and `caplog.at_level` for logging.
- Exception chaining (`__cause__`) and asserting on what was *not* called.
- Filesystem invariants: atomicity, no leftover temp files.

## Run commands

- Grade your suite (all grader checks): `uv run pytest exercises/16_unit_testing`
- Iterate on your suite directly (from the exercise directory, so `billing` is importable): `cd exercises/16_unit_testing` then `uv run python -m pytest -o addopts= --import-mode=prepend main.py -q`
- Validate the reference suite: `uv run pytest exercises/16_unit_testing --solutions`

## Stretch (untested)

- Write a small mutation-testing harness of your own, then compare its kill rate with the grader's.
- Replace the hand-written clock fixture with a fake that advances only when you tell it to.
- Add a Hypothesis stateful test for the retry loop: random sequences of transient and success outcomes, with the invariant that the gateway call count never exceeds `max_attempts`.
