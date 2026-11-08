"""Grader for exercise 16: runs your main.py suite against billing.py and injected bugs.

Three properties are graded:
  1. the suite passes against the unmodified billing.py and has at least MIN_TESTS tests;
  2. every mutant of billing.py makes the suite fail with test failures (exit 1);
  3. the suite finishes within TIME_BUDGET_S, so retry backoff must be patched, not slept.

Each run happens in a fresh scratch directory with billing.py and the suite as
test_student.py, in a separate pytest process that ignores the repo configuration.
Mutants are exact source replacements; each pattern must occur exactly once in
billing.py, so no mutant can silently be a no-op.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

MIN_TESTS = 30
TIME_BUDGET_S = 10.0
RUN_TIMEOUT_S = 60.0

# (name, exact text in billing.py, replacement)
MUTANTS: tuple[tuple[str, str, str], ...] = (
    ("banker_rounding", "return amount.quantize(CENT, rounding=ROUND_HALF_UP)", "return round(amount, 2)"),
    ("tier_boundary_exclusive", 'if subtotal >= Decimal("500"):', 'if subtotal > Decimal("500"):'),
    ("tier_rate_wrong", 'return Decimal("0.05")', 'return Decimal("0.04")'),
    ("discount_not_removed_from_taxable", "taxable = subtotal - discount", "taxable = subtotal"),
    ("tax_on_gross_subtotal", "tax = round_money(taxable * invoice.tax_rate)", "tax = round_money(subtotal * invoice.tax_rate)"),
    ("line_total_unrounded", "return round_money(self.unit_price * self.quantity)", "return self.unit_price * self.quantity"),
    ("zero_quantity_allowed", "if self.quantity < 1:", "if self.quantity < 0:"),
    ("overdue_on_due_day", "return utcnow().date() > invoice_due", "return utcnow().date() >= invoice_due"),
    ("clock_is_naive", "return datetime.now(UTC)", "return datetime.now(UTC).replace(tzinfo=None)"),
    ("backoff_linear", "self.base_delay * 2 ** (attempt - 1), self.max_delay", "self.base_delay * attempt, self.max_delay"),
    ("backoff_uncapped", "return min(self.base_delay * 2 ** (attempt - 1), self.max_delay)", "return self.base_delay * 2 ** (attempt - 1)"),
    ("one_attempt_short", "if attempt == self.max_attempts:", "if attempt == self.max_attempts - 1:"),
    ("declined_is_retried", "                raise\n            except TransientGatewayError as exc:", "                continue\n            except TransientGatewayError as exc:"),
    ("idempotency_key_is_token", "idempotency_key=invoice.invoice_id,", "idempotency_key=token,"),
    ("charge_subtotal_not_total", "amount=totals.total,", "amount=totals.subtotal,"),
    ("receipt_never_sent", "self._send_receipt(invoice, totals, txn_id)", "pass"),
    ("receipt_error_escapes", "except Exception:", "except KeyError:"),
    ("receipt_failure_logged_as_info", "logger.exception(", "logger.info("),
    ("sleep_ignores_backoff", "time.sleep(self.backoff_delay(attempt))", "time.sleep(0)"),
    ("export_leaks_temp_file", "Path(tmp_name).unlink(missing_ok=True)", "pass"),
)


@dataclass(frozen=True)
class RunResult:
    returncode: int
    output: str
    elapsed: float


def _tail(text: str, lines: int = 40) -> str:
    return "\n".join(text.splitlines()[-lines:])


def _report(what: str, result: RunResult) -> str:
    return f"{what}\npytest exited {result.returncode}; last output:\n{_tail(result.output)}"


def _passed_count(output: str) -> int:
    match = re.search(r"(\d+) passed", output)
    return int(match.group(1)) if match else 0


@functools.cache
def _run_suite(student_source: str, billing_source: str) -> RunResult:
    """Run the student suite against billing_source in an isolated pytest process."""
    with tempfile.TemporaryDirectory(prefix="pyref-unit-testing-") as scratch_name:
        scratch = Path(scratch_name)
        (scratch / "billing.py").write_text(billing_source, encoding="utf-8")
        (scratch / "test_student.py").write_text(student_source, encoding="utf-8")
        (scratch / "empty.ini").write_text("", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
        env["PYTHONPATH"] = str(scratch)
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-c",
            "empty.ini",
            "--rootdir",
            ".",
            "test_student.py",
        ]
        start = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd,
                cwd=scratch,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=RUN_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            pytest.fail(f"student suite did not finish within {RUN_TIMEOUT_S:.0f}s (hang or real sleeps?)")
        elapsed = time.perf_counter() - start
    return RunResult(proc.returncode, proc.stdout + proc.stderr, elapsed)


@pytest.fixture
def sources(workdir: Path, exercise_dir: Path) -> tuple[str, str]:
    student = (workdir / "main.py").read_text(encoding="utf-8")
    billing = (exercise_dir / "billing.py").read_text(encoding="utf-8")
    return student, billing


class TestPart1SuitePassesOnCorrectBilling:
    def test_suite_passes_against_original_billing(self, sources: tuple[str, str]) -> None:
        student, billing = sources
        result = _run_suite(student, billing)
        assert result.returncode == 0, _report("the suite must pass against the unmodified billing.py", result)

    def test_suite_has_at_least_min_tests(self, sources: tuple[str, str]) -> None:
        student, billing = sources
        result = _run_suite(student, billing)
        passed = _passed_count(result.output)
        assert passed >= MIN_TESTS, _report(f"expected at least {MIN_TESTS} passing tests, got {passed}", result)


class TestPart2MutantsAreKilled:
    @pytest.mark.parametrize(("name", "old", "new"), MUTANTS, ids=[m[0] for m in MUTANTS])
    def test_mutant_is_killed(self, name: str, old: str, new: str, sources: tuple[str, str]) -> None:
        student, billing = sources
        baseline = _run_suite(student, billing)
        assert baseline.returncode == 0, _report("mutants only count once the suite passes on billing.py", baseline)
        occurrences = billing.count(old)
        assert occurrences == 1, f"mutant {name!r}: pattern must occur exactly once in billing.py, found {occurrences}"
        result = _run_suite(student, billing.replace(old, new))
        assert result.returncode == 1, _report(
            f"mutant {name!r} survived (expected exit 1 = test failures, got {result.returncode})",
            result,
        )


class TestPart3SuiteRespectsTimeBudget:
    def test_suite_finishes_within_budget(self, sources: tuple[str, str]) -> None:
        student, billing = sources
        result = _run_suite(student, billing)
        assert result.returncode == 0, _report("the suite must pass before its runtime counts", result)
        assert result.elapsed < TIME_BUDGET_S, (
            f"suite took {result.elapsed:.1f}s, budget is {TIME_BUDGET_S:.0f}s; "
            "retry backoff must not really sleep (patch time.sleep)"
        )
