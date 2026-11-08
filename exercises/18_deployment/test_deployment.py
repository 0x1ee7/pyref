"""Deployment exercises: config, WSGI app, access log, CLI, signals, packaging lint."""

from __future__ import annotations

import dataclasses
import importlib.metadata
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from wsgiref.types import StartResponse, WSGIApplication, WSGIEnvironment
from wsgiref.util import setup_testing_defaults

import pytest

DATABASE_URL = "postgresql://app:hunter2@db.internal:5432/orders"
SECRET_PASSWORD = "hunter2"
API_TOKEN = "tok_live_0123456789abcdef"
SERVICE_KEYS = (
    "APP_NAME",
    "PORT",
    "DEBUG",
    "ALLOWED_HOSTS",
    "DATABASE_URL",
    "API_TOKEN",
    "REQUEST_TIMEOUT",
    "LOG_LEVEL",
    "GIT_SHA",
)
REQUEST_ID_RE = re.compile(r"[0-9a-f]{32}")


def valid_env(**overrides: str) -> dict[str, str]:
    env = {"DATABASE_URL": DATABASE_URL, "API_TOKEN": API_TOKEN}
    env.update(overrides)
    return env


def scrubbed_env(**extra: str) -> dict[str, str]:
    """Copy of the process environment without any service variables."""
    env = {k: v for k, v in os.environ.items() if k not in SERVICE_KEYS}
    env.update(extra)
    return env


def fake_clock(*values: float) -> Callable[[], float]:
    readings = iter(values)
    return lambda: next(readings)


def ok_app(environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"ok"]


def make_environ(
    method: str = "GET",
    path: str = "/",
    headers: Mapping[str, str] | None = None,
    query: str = "",
) -> dict[str, Any]:
    environ: dict[str, Any] = {}
    setup_testing_defaults(environ)
    environ["REQUEST_METHOD"] = method
    environ["PATH_INFO"] = path
    environ["QUERY_STRING"] = query
    for name, value in (headers or {}).items():
        environ["HTTP_" + name.upper().replace("-", "_")] = value
    return environ


def call_wsgi(
    app: WSGIApplication,
    method: str = "GET",
    path: str = "/",
    headers: Mapping[str, str] | None = None,
    query: str = "",
) -> tuple[int, dict[str, str], bytes]:
    """Invoke a WSGI app like a server would: start_response, join body, close."""
    seen: dict[str, Any] = {}

    def start_response(status: str, response_headers: list[tuple[str, str]], exc_info: Any = None) -> Callable[[bytes], None]:
        seen["status"] = status
        seen["headers"] = {k.lower(): v for k, v in response_headers}
        return lambda data: None

    chunks = app(make_environ(method, path, headers, query), start_response)
    try:
        body = b"".join(chunks)
    finally:
        close = getattr(chunks, "close", None)
        if close is not None:
            close()
    code = int(seen["status"].split(" ", 1)[0])
    return code, seen["headers"], body


def json_payload(body: bytes) -> dict[str, Any]:
    payload = json.loads(body)
    assert isinstance(payload, dict), f"expected a JSON object, got {payload!r}"
    return payload


def error_entries(errors: tuple[str, ...], name: str) -> list[str]:
    return [e for e in errors if e.split(":", 1)[0] == name]


def settings_for(main: ModuleType, **overrides: str) -> Any:
    return main.Settings.from_env(valid_env(**overrides))


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in SERVICE_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in valid_env().items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def sentinel_handlers() -> Iterable[Callable[[int, Any], None]]:
    """Stand-in originals for SIGINT/SIGTERM.

    If a student forgets to install a handler, raise_signal reaches this
    sentinel instead of Python's default SIGINT handler (which would raise
    KeyboardInterrupt and abort the whole pytest run).
    """

    def original(signum: int, frame: Any) -> None:
        return None

    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    for sig in saved:
        signal.signal(sig, original)
    try:
        yield original
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, signal.SIG_DFL if handler is None else handler)


@pytest.fixture
def access_logger(caplog: pytest.LogCaptureFixture) -> Iterable[logging.Logger]:
    name = "pyref.tests.access"
    with caplog.at_level(logging.INFO, logger=name):
        yield logging.getLogger(name)


def access_records(caplog: pytest.LogCaptureFixture, logger: logging.Logger) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == logger.name]


# ---------------------------------------------------------------------------
# Part 1: 12-factor configuration
# ---------------------------------------------------------------------------


class TestPart1Config:
    def test_defaults_apply_when_only_required_vars_are_set(self, main: ModuleType) -> None:
        settings = main.Settings.from_env(valid_env())
        assert settings.database_url == DATABASE_URL
        assert settings.api_token == API_TOKEN
        assert settings.app_name == "pyref-service"
        assert settings.port == 8080
        assert settings.debug is False
        assert settings.allowed_hosts == ()
        assert settings.request_timeout == timedelta(seconds=30)
        assert settings.log_level == "INFO"
        assert settings.git_sha is None

    def test_every_missing_required_var_is_reported(self, main: ModuleType) -> None:
        with pytest.raises(main.ConfigError) as info:
            main.Settings.from_env({})
        assert error_entries(info.value.errors, "DATABASE_URL"), info.value.errors
        assert error_entries(info.value.errors, "API_TOKEN"), info.value.errors
        assert "required" in error_entries(info.value.errors, "API_TOKEN")[0]

    def test_all_bad_variables_are_reported_in_a_single_error(self, main: ModuleType) -> None:
        env = valid_env(PORT="eighty", DEBUG="maybe", REQUEST_TIMEOUT="5x", LOG_LEVEL="LOUD")
        with pytest.raises(main.ConfigError) as info:
            main.Settings.from_env(env)
        names = sorted(e.split(":", 1)[0] for e in info.value.errors)
        assert names == ["DEBUG", "LOG_LEVEL", "PORT", "REQUEST_TIMEOUT"], f"got {info.value.errors!r}"
        for name in names:
            assert name in str(info.value), f"{name} missing from str(ConfigError)"

    @pytest.mark.parametrize("raw", ["0", "65536", "-1", "80.5", "eighty"])
    def test_port_rejects_out_of_range_or_non_integer(self, main: ModuleType, raw: str) -> None:
        with pytest.raises(main.ConfigError) as info:
            main.Settings.from_env(valid_env(PORT=raw))
        assert error_entries(info.value.errors, "PORT"), f"PORT={raw!r} accepted"

    def test_port_accepts_inclusive_bounds(self, main: ModuleType) -> None:
        assert main.Settings.from_env(valid_env(PORT="1")).port == 1
        assert main.Settings.from_env(valid_env(PORT="65535")).port == 65535

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1", True), ("TRUE", True), (" yes ", True), ("on", True),
            ("0", False), ("False", False), ("no", False), ("off", False),
        ],
    )
    def test_parse_bool_accepts_common_spellings(self, main: ModuleType, raw: str, expected: bool) -> None:
        assert main.parse_bool(raw) is expected

    def test_parse_bool_rejects_unknown_spelling(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.parse_bool("maybe")

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("250ms", timedelta(milliseconds=250)),
            ("30s", timedelta(seconds=30)),
            ("5m", timedelta(minutes=5)),
            ("1h", timedelta(hours=1)),
            ("2", timedelta(seconds=2)),
            ("1.5s", timedelta(seconds=1.5)),
        ],
    )
    def test_parse_duration_units(self, main: ModuleType, raw: str, expected: timedelta) -> None:
        assert main.parse_duration(raw) == expected

    @pytest.mark.parametrize("raw", ["", "-1s", "10d", "5 minutes", "abc"])
    def test_parse_duration_rejects_garbage(self, main: ModuleType, raw: str) -> None:
        with pytest.raises(ValueError):
            main.parse_duration(raw)

    def test_request_timeout_must_be_positive(self, main: ModuleType) -> None:
        with pytest.raises(main.ConfigError) as info:
            main.Settings.from_env(valid_env(REQUEST_TIMEOUT="0s"))
        assert error_entries(info.value.errors, "REQUEST_TIMEOUT"), info.value.errors

    def test_allowed_hosts_are_split_stripped_and_filtered(self, main: ModuleType) -> None:
        settings = main.Settings.from_env(valid_env(ALLOWED_HOSTS=" api.example.com, ,admin.example.com,, "))
        assert settings.allowed_hosts == ("api.example.com", "admin.example.com")

    def test_empty_optional_variable_falls_back_to_default(self, main: ModuleType) -> None:
        settings = main.Settings.from_env(valid_env(PORT="", DEBUG="   "))
        assert settings.port == 8080
        assert settings.debug is False

    def test_empty_required_variable_is_reported_as_missing(self, main: ModuleType) -> None:
        with pytest.raises(main.ConfigError) as info:
            main.Settings.from_env(valid_env(API_TOKEN=""))
        entries = error_entries(info.value.errors, "API_TOKEN")
        assert entries and "required" in entries[0], info.value.errors

    @pytest.mark.parametrize("url", ["postgresql://u:p@h:5432/db", "sqlite:///var/lib/app.db"])
    def test_database_url_accepts_supported_schemes(self, main: ModuleType, url: str) -> None:
        assert main.Settings.from_env(valid_env(DATABASE_URL=url)).database_url == url

    def test_database_url_rejects_unknown_scheme_without_leaking_password(self, main: ModuleType) -> None:
        with pytest.raises(main.ConfigError) as info:
            main.Settings.from_env(valid_env(DATABASE_URL="mysql://app:s3cr3t-pass@db/orders"))
        assert error_entries(info.value.errors, "DATABASE_URL"), info.value.errors
        assert "s3cr3t-pass" not in str(info.value)

    def test_api_token_error_does_not_echo_the_value(self, main: ModuleType) -> None:
        short = "tok_short_123"
        with pytest.raises(main.ConfigError) as info:
            main.Settings.from_env(valid_env(API_TOKEN=short))
        assert error_entries(info.value.errors, "API_TOKEN"), info.value.errors
        assert short not in str(info.value)

    def test_log_level_is_case_insensitive_and_stored_uppercase(self, main: ModuleType) -> None:
        assert main.Settings.from_env(valid_env(LOG_LEVEL="warning")).log_level == "WARNING"

    def test_git_sha_is_validated_and_lowercased(self, main: ModuleType) -> None:
        assert main.Settings.from_env(valid_env(GIT_SHA="ABCDEF1234")).git_sha == "abcdef1234"
        with pytest.raises(main.ConfigError) as info:
            main.Settings.from_env(valid_env(GIT_SHA="not-a-sha"))
        assert error_entries(info.value.errors, "GIT_SHA"), info.value.errors

    def test_settings_is_frozen(self, main: ModuleType) -> None:
        settings = main.Settings.from_env(valid_env())
        with pytest.raises(dataclasses.FrozenInstanceError):
            settings.port = 9000  # type: ignore[misc]

    def test_repr_masks_token_and_database_password(self, main: ModuleType) -> None:
        text = repr(main.Settings.from_env(valid_env()))
        assert API_TOKEN not in text, "api_token leaked into repr"
        assert SECRET_PASSWORD not in text, "database password leaked into repr"
        assert "***" in text
        assert "db.internal" in text, "host should stay visible for debugging"

    def test_dotenv_grammar_comments_export_quotes_and_inline_comments(self, main: ModuleType) -> None:
        text = "\n".join(
            [
                "# comment line",
                "export APP_NAME=orders-api",
                "PORT = 9000",
                "DEBUG='true'   # inline comment",
                'API_TOKEN="tok with spaces # not a comment"',
                "DATABASE_URL=postgresql://u:p@h/db  # trailing comment",
                "EMPTY=",
                r'GREETING="line1\nline2 \"quoted\""',
                r"LITERAL='no \n escapes'",
                "",
            ]
        )
        assert main.load_dotenv(text) == {
            "APP_NAME": "orders-api",
            "PORT": "9000",
            "DEBUG": "true",
            "API_TOKEN": "tok with spaces # not a comment",
            "DATABASE_URL": "postgresql://u:p@h/db",
            "EMPTY": "",
            "GREETING": 'line1\nline2 "quoted"',
            "LITERAL": r"no \n escapes",
        }

    def test_dotenv_reports_line_number_for_malformed_entry(self, main: ModuleType) -> None:
        with pytest.raises(ValueError, match="line 2"):
            main.load_dotenv("A=1\nnot a pair\n")

    def test_dotenv_rejects_unterminated_quote(self, main: ModuleType) -> None:
        with pytest.raises(ValueError, match="unterminated"):
            main.load_dotenv('A="oops')

    def test_real_environment_overrides_env_file(self, main: ModuleType, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("PORT=9000\nLOG_LEVEL=DEBUG\n", encoding="utf-8")
        settings = main.load_settings(valid_env(PORT="7000"), env_file)
        assert settings.port == 7000, "real env must win over .env"
        assert settings.log_level == "DEBUG", "values only in .env must still apply"

    def test_missing_env_file_raises_file_not_found(self, main: ModuleType, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            main.load_settings(valid_env(), tmp_path / "nope.env")


# ---------------------------------------------------------------------------
# Part 2: WSGI app with health and version endpoints
# ---------------------------------------------------------------------------


def _boom() -> None:
    raise ConnectionError("connection refused")


class TestPart2Wsgi:
    def test_healthz_is_ok_even_when_a_dependency_check_fails(self, main: ModuleType) -> None:
        app = main.create_app(settings_for(main), readiness_checks={"db": _boom})
        code, _, body = call_wsgi(app, path="/healthz")
        assert code == 200, body
        assert json_payload(body) == {"status": "ok"}

    def test_readyz_with_no_checks_is_ready(self, main: ModuleType) -> None:
        app = main.create_app(settings_for(main))
        code, _, body = call_wsgi(app, path="/readyz")
        assert code == 200
        assert json_payload(body) == {"status": "ready", "checks": {}}

    def test_readyz_is_503_when_any_check_raises_and_reports_all(self, main: ModuleType) -> None:
        app = main.create_app(settings_for(main), readiness_checks={"cache": lambda: None, "db": _boom})
        code, headers, body = call_wsgi(app, path="/readyz")
        assert code == 503, body
        payload = json_payload(body)
        assert payload["status"] == "not_ready"
        assert payload["checks"]["cache"] == "ok"
        assert payload["checks"]["db"].startswith("error:")
        assert "connection refused" in payload["checks"]["db"]
        assert headers["content-type"].startswith("application/json")

    def test_readiness_checks_run_on_every_request(self, main: ModuleType) -> None:
        calls: list[int] = []
        app = main.create_app(settings_for(main), readiness_checks={"probe": lambda: calls.append(1)})
        call_wsgi(app, path="/readyz")
        call_wsgi(app, path="/readyz")
        assert len(calls) == 2, "checks must not be cached between requests"

    def test_version_reports_package_version_and_git_sha(self, main: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(importlib.metadata, "version", lambda name: "1.2.3")
        app = main.create_app(settings_for(main, GIT_SHA="abc1234"))
        code, _, body = call_wsgi(app, path="/version")
        assert code == 200
        assert json_payload(body) == {"app": "pyref-service", "version": "1.2.3", "git_sha": "abc1234"}

    def test_version_falls_back_when_distribution_is_not_installed(self, main: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        def missing(name: str) -> str:
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, "version", missing)
        app = main.create_app(settings_for(main))
        code, _, body = call_wsgi(app, path="/version")
        assert code == 200
        payload = json_payload(body)
        assert payload["version"] == "0.0.0+unknown"
        assert payload["git_sha"] is None

    def test_unknown_path_returns_json_404(self, main: ModuleType) -> None:
        app = main.create_app(settings_for(main))
        code, headers, body = call_wsgi(app, path="/nope")
        assert code == 404
        assert headers["content-type"].startswith("application/json")
        assert json_payload(body)["error"] == "not_found"

    def test_wrong_method_on_known_route_is_405_with_allow(self, main: ModuleType) -> None:
        app = main.create_app(settings_for(main))
        code, headers, body = call_wsgi(app, method="POST", path="/healthz")
        assert code == 405, body
        assert headers.get("allow") == "GET, HEAD"
        assert json_payload(body)["error"] == "method_not_allowed"

    def test_head_has_headers_and_no_body(self, main: ModuleType) -> None:
        app = main.create_app(settings_for(main))
        code, headers, body = call_wsgi(app, method="HEAD", path="/healthz")
        assert code == 200
        assert body == b""
        assert int(headers["content-length"]) > 0

    def test_content_length_matches_body_bytes(self, main: ModuleType) -> None:
        app = main.create_app(settings_for(main, GIT_SHA="abc1234"))
        _, headers, body = call_wsgi(app, path="/version")
        assert int(headers["content-length"]) == len(body)


# ---------------------------------------------------------------------------
# Part 3: structured JSON access logging
# ---------------------------------------------------------------------------


class TestPart3AccessLog:
    def test_one_json_record_per_request_with_timing(self, main: ModuleType, access_logger: logging.Logger, caplog: pytest.LogCaptureFixture) -> None:
        app = main.access_log(ok_app, logger=access_logger, clock=fake_clock(10.0, 10.0125))
        call_wsgi(app, method="GET", path="/healthz")
        records = access_records(caplog, access_logger)
        assert len(records) == 1, f"expected one access record, got {len(records)}"
        record = records[0]
        assert set(record) == {"method", "path", "status", "duration_ms", "request_id"}
        assert record["method"] == "GET"
        assert record["path"] == "/healthz"
        assert record["status"] == 200
        assert record["duration_ms"] == 12.5
        assert REQUEST_ID_RE.fullmatch(record["request_id"])

    def test_incoming_request_id_is_propagated_to_log_and_response(self, main: ModuleType, access_logger: logging.Logger, caplog: pytest.LogCaptureFixture) -> None:
        app = main.access_log(ok_app, logger=access_logger)
        _, headers, _ = call_wsgi(app, headers={"X-Request-ID": "req-abc-123"})
        assert headers["x-request-id"] == "req-abc-123"
        assert access_records(caplog, access_logger)[0]["request_id"] == "req-abc-123"

    def test_invalid_incoming_request_id_is_replaced(self, main: ModuleType, access_logger: logging.Logger) -> None:
        app = main.access_log(ok_app, logger=access_logger)
        _, headers, _ = call_wsgi(app, headers={"X-Request-ID": "bad id with spaces"})
        assert "bad id with spaces" not in headers.get("x-request-id", "")
        assert REQUEST_ID_RE.fullmatch(headers.get("x-request-id", "")), headers

    def test_generated_request_id_when_header_absent(self, main: ModuleType, access_logger: logging.Logger) -> None:
        app = main.access_log(ok_app, logger=access_logger)
        _, headers, _ = call_wsgi(app)
        assert REQUEST_ID_RE.fullmatch(headers.get("x-request-id", "")), headers

    def test_query_string_is_not_logged(self, main: ModuleType, access_logger: logging.Logger, caplog: pytest.LogCaptureFixture) -> None:
        app = main.access_log(ok_app, logger=access_logger)
        call_wsgi(app, path="/version", query="token=secret-value")
        record = access_records(caplog, access_logger)[0]
        assert record["path"] == "/version"
        assert "secret-value" not in json.dumps(record)

    def test_raising_app_logs_500_and_reraises(self, main: ModuleType, access_logger: logging.Logger, caplog: pytest.LogCaptureFixture) -> None:
        def broken(environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
            raise RuntimeError("kaboom")

        app = main.access_log(broken, logger=access_logger)
        with pytest.raises(RuntimeError, match="kaboom"):
            call_wsgi(app, path="/explode")
        records = access_records(caplog, access_logger)
        assert len(records) == 1, records
        assert records[0]["status"] == 500
        assert records[0]["path"] == "/explode"

    def test_closing_twice_emits_exactly_one_record(self, main: ModuleType, access_logger: logging.Logger, caplog: pytest.LogCaptureFixture) -> None:
        app = main.access_log(ok_app, logger=access_logger)
        body = app(make_environ(), lambda status, headers, exc_info=None: None)
        b"".join(body)
        body.close()
        body.close()
        assert len(access_records(caplog, access_logger)) == 1

    def test_logger_configuration_is_left_untouched(self, main: ModuleType, access_logger: logging.Logger) -> None:
        before = (list(access_logger.handlers), access_logger.level, access_logger.propagate)
        app = main.access_log(ok_app, logger=access_logger)
        call_wsgi(app)
        after = (list(access_logger.handlers), access_logger.level, access_logger.propagate)
        assert after == before


# ---------------------------------------------------------------------------
# Part 4: CLI entry point
# ---------------------------------------------------------------------------


class TestPart4Cli:
    def test_check_config_succeeds_with_valid_environment(self, main: ModuleType, clean_env: None, capsys: pytest.CaptureFixture[str]) -> None:
        assert main.main(["check-config"]) == 0
        out, err = capsys.readouterr()
        assert "ok" in out.lower(), out
        assert err == ""

    def test_check_config_prints_one_error_line_per_bad_variable(self, main: ModuleType, clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setenv("PORT", "eighty")
        monkeypatch.delenv("API_TOKEN")
        assert main.main(["check-config"]) == 2
        err_lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
        assert len(err_lines) == 2, err_lines
        assert all(line.startswith("error:") for line in err_lines), err_lines
        assert any("PORT" in line for line in err_lines)
        assert any("API_TOKEN" in line for line in err_lines)

    def test_env_file_values_are_used(self, main: ModuleType, clean_env: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        env_file = tmp_path / "service.env"
        env_file.write_text("PORT=9100\n", encoding="utf-8")
        assert main.main(["--env-file", str(env_file), "serve", "--dry-run"]) == 0
        assert "9100" in capsys.readouterr().out

    def test_missing_env_file_is_reported_with_exit_2(self, main: ModuleType, clean_env: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert main.main(["--env-file", str(tmp_path / "nope.env"), "check-config"]) == 2
        assert "nope.env" in capsys.readouterr().err

    def test_malformed_env_file_is_reported_with_line_number(self, main: ModuleType, clean_env: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        env_file = tmp_path / "bad.env"
        env_file.write_text("garbage line\n", encoding="utf-8")
        assert main.main(["--env-file", str(env_file), "check-config"]) == 2
        assert "line 1" in capsys.readouterr().err

    def test_version_prints_package_version(self, main: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setattr(importlib.metadata, "version", lambda name: "9.9.9")
        assert main.main(["version"]) == 0
        assert capsys.readouterr().out.strip() == "9.9.9"

    def test_version_needs_no_configuration(self, main: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        for key in SERVICE_KEYS:
            monkeypatch.delenv(key, raising=False)
        assert main.main(["version"]) == 0
        assert capsys.readouterr().out.strip() != ""

    def test_serve_dry_run_reports_address_without_listening(self, main: ModuleType, clean_env: None, capsys: pytest.CaptureFixture[str]) -> None:
        assert main.main(["serve", "--dry-run", "--host", "0.0.0.0", "--port", "9001"]) == 0
        assert "0.0.0.0:9001" in capsys.readouterr().out

    def test_serve_dry_run_fails_on_invalid_configuration(self, main: ModuleType, clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.delenv("DATABASE_URL")
        assert main.main(["serve", "--dry-run"]) == 2
        assert "DATABASE_URL" in capsys.readouterr().err

    def test_usage_error_returns_2_instead_of_raising(self, main: ModuleType, clean_env: None, capsys: pytest.CaptureFixture[str]) -> None:
        assert main.main(["no-such-command"]) == 2
        assert "usage" in capsys.readouterr().err.lower()

    def test_script_runs_via_subprocess_and_succeeds(self, workdir: Path) -> None:
        result = subprocess.run(
            [sys.executable, "main.py", "check-config"],
            cwd=workdir,
            env=scrubbed_env(**valid_env()),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "ok" in result.stdout.lower()

    def test_script_exits_2_with_error_on_bad_configuration(self, workdir: Path) -> None:
        result = subprocess.run(
            [sys.executable, "main.py", "check-config"],
            cwd=workdir,
            env=scrubbed_env(API_TOKEN=API_TOKEN),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 2, result.stdout
        assert "DATABASE_URL" in result.stderr


# ---------------------------------------------------------------------------
# Part 5: graceful shutdown on SIGINT / SIGTERM
# ---------------------------------------------------------------------------


class TestPart5Signals:
    def test_sigint_triggers_shutdown_with_signal_number(self, main: ModuleType, sentinel_handlers: Any) -> None:
        received: list[int] = []
        restore = main.install_signal_handlers(received.append)
        try:
            signal.raise_signal(signal.SIGINT)
        finally:
            restore()
        assert received == [signal.SIGINT]

    def test_repeated_signals_trigger_shutdown_only_once(self, main: ModuleType, sentinel_handlers: Any) -> None:
        received: list[int] = []
        restore = main.install_signal_handlers(received.append)
        try:
            for _ in range(3):
                signal.raise_signal(signal.SIGTERM)
        finally:
            restore()
        assert received == [signal.SIGTERM], f"shutdown called {len(received)} times"

    def test_restore_reinstates_original_handlers_and_is_idempotent(self, main: ModuleType, sentinel_handlers: Any) -> None:
        restore = main.install_signal_handlers(lambda signum: None)
        assert signal.getsignal(signal.SIGINT) is not sentinel_handlers
        restore()
        assert signal.getsignal(signal.SIGINT) is sentinel_handlers
        assert signal.getsignal(signal.SIGTERM) is sentinel_handlers
        restore()
        assert signal.getsignal(signal.SIGINT) is sentinel_handlers

    def test_signals_argument_limits_which_signals_are_hooked(self, main: ModuleType, sentinel_handlers: Any) -> None:
        received: list[int] = []
        restore = main.install_signal_handlers(received.append, signals=(signal.SIGTERM,))
        try:
            assert signal.getsignal(signal.SIGINT) is sentinel_handlers
            signal.raise_signal(signal.SIGTERM)
        finally:
            restore()
        assert received == [signal.SIGTERM]


# ---------------------------------------------------------------------------
# Part 6: packaging and container lint (text inspection is the point here)
# ---------------------------------------------------------------------------

PINNED_BASE = re.compile(r"python:3\.1[3-9]\.\d+-slim-[a-z]+")


def _pyproject(workdir: Path) -> dict[str, Any]:
    return tomllib.loads((workdir / "pyproject.toml").read_text(encoding="utf-8"))


def _dockerfile_instructions(workdir: Path) -> list[tuple[str, str]]:
    """Logical (KEYWORD, args) pairs: comments and blanks dropped, continuations joined."""
    logical: list[str] = []
    buffer = ""
    for raw in (workdir / "Dockerfile").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        logical.append((buffer + line).strip())
        buffer = ""
    if buffer.strip():
        logical.append(buffer.strip())
    instructions: list[tuple[str, str]] = []
    for entry in logical:
        keyword, _, args = entry.partition(" ")
        instructions.append((keyword.upper(), args.strip()))
    return instructions


def _copy_sources(args: str) -> list[str]:
    tokens = [t for t in args.split() if not t.startswith("--")]
    return tokens[:-1]


class TestPart6Packaging:
    def test_project_metadata_is_complete(self, workdir: Path) -> None:
        project = _pyproject(workdir).get("project", {})
        assert project.get("name") == "pyref-service", f"name={project.get('name')!r}"
        assert re.fullmatch(r"\d+\.\d+\.\d+", str(project.get("version", ""))), "version must be X.Y.Z"
        assert project.get("dependencies") == [], "stdlib only: dependencies must be []"

    def test_requires_python_floor_is_3_13(self, workdir: Path) -> None:
        spec = _pyproject(workdir).get("project", {}).get("requires-python", "")
        match = re.fullmatch(r">=\s*3\.(\d+)(?:\.\d+)?", spec)
        assert match is not None, f"requires-python={spec!r} must be a '>=3.x' floor"
        assert int(match.group(1)) >= 13

    def test_console_script_points_at_main(self, workdir: Path) -> None:
        scripts = _pyproject(workdir).get("project", {}).get("scripts", {})
        assert scripts.get("pyref-service") == "main:main", scripts

    def test_build_system_is_declared(self, workdir: Path) -> None:
        build = _pyproject(workdir).get("build-system", {})
        assert build.get("requires"), "build-system.requires must be non-empty"
        assert build.get("build-backend"), "build-system.build-backend missing"

    def test_base_image_is_pinned_slim_python(self, workdir: Path) -> None:
        froms = [args for kw, args in _dockerfile_instructions(workdir) if kw == "FROM"]
        assert froms, "Dockerfile has no FROM instruction"
        for args in froms:
            image = args.split()[0]
            assert PINNED_BASE.fullmatch(image), f"{image!r} must be python:3.1x.y-slim-<suite>"

    def test_dependency_manifest_is_copied_before_source(self, workdir: Path) -> None:
        copies = [_copy_sources(args) for kw, args in _dockerfile_instructions(workdir) if kw == "COPY"]
        manifest = next((i for i, srcs in enumerate(copies) if "pyproject.toml" in srcs), None)
        source = next((i for i, srcs in enumerate(copies) if any(s in {".", "./", "*"} for s in srcs)), None)
        assert manifest is not None, "pyproject.toml is never COPYed"
        assert source is not None, "the source tree is never COPYed"
        assert manifest < source, "pyproject.toml must be copied before the source tree"

    def test_python_runtime_env_flags_are_set(self, workdir: Path) -> None:
        env: dict[str, str] = {}
        for kw, args in _dockerfile_instructions(workdir):
            if kw == "ENV":
                env.update(re.findall(r"(\w+)=(\S+)", args))
        assert env.get("PYTHONDONTWRITEBYTECODE") == "1", env
        assert env.get("PYTHONUNBUFFERED") == "1", env

    def test_runs_as_non_root_user(self, workdir: Path) -> None:
        users = [args for kw, args in _dockerfile_instructions(workdir) if kw == "USER"]
        assert users, "no USER instruction"
        assert users[-1].split(":")[0] not in {"root", "0", ""}, f"last USER is {users[-1]!r}"

    def test_exposes_numeric_port_and_healthcheck_targets_healthz(self, workdir: Path) -> None:
        instructions = _dockerfile_instructions(workdir)
        exposes = [args for kw, args in instructions if kw == "EXPOSE"]
        assert exposes and any(re.fullmatch(r"\d+(/tcp|/udp)?", tok) for tok in exposes[-1].split()), exposes
        checks = [args for kw, args in instructions if kw == "HEALTHCHECK"]
        assert checks, "no HEALTHCHECK instruction"
        assert checks[-1].upper() != "NONE"
        assert "/healthz" in checks[-1], "HEALTHCHECK must probe /healthz"

    def test_entrypoint_and_cmd_use_exec_form(self, workdir: Path) -> None:
        instructions = _dockerfile_instructions(workdir)
        for keyword in ("ENTRYPOINT", "CMD"):
            values = [args for kw, args in instructions if kw == keyword]
            assert values, f"no {keyword} instruction"
            try:
                parsed = json.loads(values[-1])
            except json.JSONDecodeError:
                pytest.fail(f"{keyword} must use exec (JSON array) form, got {values[-1]!r}")
            assert isinstance(parsed, list) and all(isinstance(x, str) for x in parsed), f"{keyword}: {parsed!r}"

    def test_dockerignore_excludes_vcs_venv_caches_and_secrets(self, workdir: Path) -> None:
        patterns: set[str] = set()
        for raw in (workdir / ".dockerignore").read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("**/"):
                line = line[3:]
            patterns.add(line.rstrip("/").removeprefix("./"))
        required = {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".hypothesis", ".env"}
        missing = sorted(required - patterns)
        assert not missing, f".dockerignore is missing {missing}"
