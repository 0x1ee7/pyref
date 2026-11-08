"""Part 7: static contracts, checked by running mypy --strict over your main.py.

The graded file is copied next to the two usage files into a temporary
directory, so ``from main import ...`` always resolves to the code under test.
Results are cached per workdir for the whole session.
"""

import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import mypy.api

PUBLIC_API = (
    "HasId",
    "NotFoundError",
    "DuplicateIdError",
    "Repo",
    "first",
    "first_or",
    "normalize_key",
    "Sink",
    "MemorySink",
    "fan_out",
    "UserId",
    "UserCreated",
    "OrderPlaced",
    "Event",
    "Unsubscribe",
    "EventBus",
    "user_topic",
    "describe_event",
    "DEFAULT_HTTP_TIMEOUT",
    "Range",
    "Port",
    "DatabaseSettings",
    "Settings",
    "SettingsError",
    "field_range",
    "load_settings",
    "retry",
    "TenantId",
    "tenant_scope",
    "current_tenant",
    "with_tenant",
    "QueryBuilder",
    "Ok",
    "Err",
    "Result",
    "PaymentError",
    "is_ok",
    "all_str",
    "collect",
    "http_status",
    "render_result",
)

_DIAGNOSTIC = re.compile(r"^(?P<file>.+?):(?P<line>\d+): error: (?P<message>.*?)(?:\s+\[(?P<code>[\w-]+)\])?$")
_EXPECT = re.compile(r"#\s*expect:\s*(?P<code>[\w-]+)")


@dataclass(frozen=True)
class Diagnostic:
    file: str
    line: int
    code: str | None
    message: str


_CACHE: dict[Path, list[Diagnostic]] = {}


def _mypy_diagnostics(workdir: Path, exercise_dir: Path) -> list[Diagnostic]:
    """Run ``mypy --strict`` once per workdir over main.py and both usage files."""
    key = workdir.resolve()
    if key in _CACHE:
        return _CACHE[key]

    with tempfile.TemporaryDirectory(prefix="pyref-mypy-") as tmp:
        root = Path(tmp)
        shutil.copy2(workdir / "main.py", root / "main.py")
        for name in ("usage_ok.py", "usage_bad.py"):
            shutil.copy2(exercise_dir / name, root / name)
        stdout, _stderr, _status = mypy.api.run(
            [
                "--strict",
                "--show-error-codes",
                "--no-error-summary",
                "--cache-dir",
                str(root / ".mypy_cache"),
                str(root / "main.py"),
                str(root / "usage_ok.py"),
                str(root / "usage_bad.py"),
            ]
        )

    diagnostics: list[Diagnostic] = []
    for raw in stdout.splitlines():
        match = _DIAGNOSTIC.match(raw)
        if match is None:
            continue  # notes and continuation lines
        file = re.split(r"[\\/]", match["file"])[-1]
        diagnostics.append(Diagnostic(file, int(match["line"]), match["code"], match["message"]))
    _CACHE[key] = diagnostics
    return diagnostics


def _format(diagnostics: list[Diagnostic]) -> str:
    return "\n".join(f"  {d.file}:{d.line} [{d.code}] {d.message}" for d in diagnostics) or "  (none)"


class TestPart7StaticContracts:
    def test_main_defines_public_api_and_passes_strict_mypy(
        self, main: ModuleType, workdir: Path, exercise_dir: Path
    ) -> None:
        missing = [name for name in PUBLIC_API if not hasattr(main, name)]
        assert not missing, f"main.py is missing public names: {', '.join(missing)}"
        errors = [d for d in _mypy_diagnostics(workdir, exercise_dir) if d.file == "main.py"]
        assert not errors, f"main.py must pass mypy --strict; errors:\n{_format(errors)}"

    def test_usage_ok_type_checks_cleanly(self, workdir: Path, exercise_dir: Path) -> None:
        errors = [d for d in _mypy_diagnostics(workdir, exercise_dir) if d.file == "usage_ok.py"]
        assert not errors, (
            "usage_ok.py must type-check cleanly under mypy --strict against main.py; errors:\n"
            f"{_format(errors)}"
        )

    def test_usage_bad_reports_exactly_the_marked_errors(self, workdir: Path, exercise_dir: Path) -> None:
        source = (exercise_dir / "usage_bad.py").read_text(encoding="utf-8").splitlines()
        expected = sorted(
            (number, match["code"])
            for number, text in enumerate(source, start=1)
            if (match := _EXPECT.search(text)) is not None
        )
        assert len(expected) >= 15, "usage_bad.py must keep its expect markers (sanity check)"

        actual = sorted(
            (d.line, d.code)
            for d in _mypy_diagnostics(workdir, exercise_dir)
            if d.file == "usage_bad.py" and d.code is not None
        )
        assert actual == expected, (
            "usage_bad.py must fail exactly at the marked lines with the marked codes.\n"
            f"expected (line, code): {expected}\n"
            f"actual   (line, code): {actual}\n"
            f"diagnostics:\n{_format([d for d in _mypy_diagnostics(workdir, exercise_dir) if d.file == 'usage_bad.py'])}"
        )
