"""pyref-service: a stdlib-only WSGI service packaged for containers."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import logging
import os
import re
import signal
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
from types import FrameType
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from wsgiref.simple_server import WSGIRequestHandler, make_server
from wsgiref.types import StartResponse, WSGIApplication, WSGIEnvironment

DISTRIBUTION = "pyref-service"
FALLBACK_VERSION = "0.0.0+unknown"
REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_KEY = "pyref.request_id"

ReadinessCheck = Callable[[], object]

_DB_SCHEMES = ("postgresql", "sqlite")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)?")
_DURATION_UNITS = {
    "ms": timedelta(milliseconds=1),
    "s": timedelta(seconds=1),
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
}
_HEX_SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}")
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_DOTENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DOTENV_EXPORT_RE = re.compile(r"export\s+")
_DOTENV_INLINE_COMMENT_RE = re.compile(r"\s+#")
_DOTENV_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


# ---------------------------------------------------------------------------
# Part 1: 12-factor configuration
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Aggregated configuration problems; one ``"VAR: problem"`` entry each."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors: tuple[str, ...] = tuple(errors)
        super().__init__("invalid configuration: " + "; ".join(self.errors))


class DotenvError(ValueError):
    """A malformed line in a ``.env`` file; the message starts with ``line N:``."""


def parse_bool(text: str) -> bool:
    value = text.strip().lower()
    if value in _TRUE_WORDS:
        return True
    if value in _FALSE_WORDS:
        return False
    raise ValueError(f"not a boolean: {text!r}")


def parse_duration(text: str) -> timedelta:
    match = _DURATION_RE.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"invalid duration: {text!r}")
    amount = float(match.group(1))
    return _DURATION_UNITS[match.group(2) or "s"] * amount


def parse_list(text: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in text.split(",") if item.strip())


def mask_url_password(url: str) -> str:
    parts = urlsplit(url)
    if parts.password is None:
        return url
    userinfo, _, hostport = parts.netloc.rpartition("@")
    user = userinfo.split(":", 1)[0]
    return urlunsplit(parts._replace(netloc=f"{user}:***@{hostport}"))


def _parse_port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        raise ValueError(f"must be an integer between 1 and 65535, got {text!r}") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"must be an integer between 1 and 65535, got {port}")
    return port


def _parse_timeout(text: str) -> timedelta:
    value = parse_duration(text)
    if value <= timedelta(0):
        raise ValueError("must be greater than zero")
    return value


def _parse_log_level(text: str) -> str:
    level = text.upper()
    if level not in _LOG_LEVELS:
        raise ValueError(f"must be one of {', '.join(_LOG_LEVELS)}")
    return level


def _parse_git_sha(text: str) -> str:
    if not _HEX_SHA_RE.fullmatch(text):
        raise ValueError("must be 7-40 hexadecimal characters")
    return text.lower()


def _database_problem(url: str) -> str | None:
    try:
        scheme = urlsplit(url).scheme
    except ValueError:
        return "not a valid URL"
    if scheme not in _DB_SCHEMES:
        return f"unsupported scheme {scheme!r}; expected postgresql or sqlite"
    return None


def _field[T](
    environ: Mapping[str, str],
    errors: list[str],
    name: str,
    parse: Callable[[str], T],
    default: T,
) -> T:
    """Parse an optional variable; empty means unset, bad values are collected."""
    text = environ.get(name, "").strip()
    if not text:
        return default
    try:
        return parse(text)
    except ValueError as exc:
        errors.append(f"{name}: {exc}")
        return default


def _required(environ: Mapping[str, str], errors: list[str], name: str) -> str:
    text = environ.get(name, "").strip()
    if not text:
        errors.append(f"{name}: required but not set")
    return text


@dataclass(frozen=True, slots=True, repr=False)
class Settings:
    database_url: str
    api_token: str
    app_name: str = DISTRIBUTION
    port: int = 8080
    debug: bool = False
    allowed_hosts: tuple[str, ...] = ()
    request_timeout: timedelta = timedelta(seconds=30)
    log_level: str = "INFO"
    git_sha: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        errors: list[str] = []

        database_url = _required(environ, errors, "DATABASE_URL")
        if database_url:
            problem = _database_problem(database_url)
            if problem is not None:
                errors.append(f"DATABASE_URL: {problem}")

        api_token = _required(environ, errors, "API_TOKEN")
        if api_token and len(api_token) < 16:
            errors.append("API_TOKEN: must be at least 16 characters")

        app_name = _field(environ, errors, "APP_NAME", str, DISTRIBUTION)
        port = _field(environ, errors, "PORT", _parse_port, 8080)
        debug = _field(environ, errors, "DEBUG", parse_bool, False)
        allowed_hosts = _field(environ, errors, "ALLOWED_HOSTS", parse_list, ())
        request_timeout = _field(environ, errors, "REQUEST_TIMEOUT", _parse_timeout, timedelta(seconds=30))
        log_level = _field(environ, errors, "LOG_LEVEL", _parse_log_level, "INFO")
        git_sha = _field(environ, errors, "GIT_SHA", _parse_git_sha, None)

        if errors:
            raise ConfigError(errors)
        return cls(
            database_url=database_url,
            api_token=api_token,
            app_name=app_name,
            port=port,
            debug=debug,
            allowed_hosts=allowed_hosts,
            request_timeout=request_timeout,
            log_level=log_level,
            git_sha=git_sha,
        )

    def __repr__(self) -> str:
        values: dict[str, Any] = {f.name: getattr(self, f.name) for f in fields(self)}
        values["database_url"] = mask_url_password(self.database_url)
        values["api_token"] = "***"
        body = ", ".join(f"{name}={value!r}" for name, value in values.items())
        return f"Settings({body})"


def _parse_dotenv_value(raw: str) -> str:
    if not raw:
        return ""
    quote = raw[0]
    if quote == "'":
        end = raw.find("'", 1)
        if end == -1:
            raise ValueError("unterminated quoted value")
        value, rest = raw[1:end], raw[end + 1 :]
    elif quote == '"':
        chars: list[str] = []
        i = 1
        while True:
            if i >= len(raw):
                raise ValueError("unterminated quoted value")
            ch = raw[i]
            if ch == '"':
                break
            if ch == "\\" and i + 1 < len(raw) and raw[i + 1] in _DOTENV_ESCAPES:
                chars.append(_DOTENV_ESCAPES[raw[i + 1]])
                i += 2
                continue
            chars.append(ch)
            i += 1
        value, rest = "".join(chars), raw[i + 1 :]
    else:
        comment = _DOTENV_INLINE_COMMENT_RE.search(raw)
        return raw[: comment.start()] if comment else raw

    if rest.strip() and not rest.lstrip().startswith("#"):
        raise ValueError("unexpected text after closing quote")
    return value


def load_dotenv(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        export = _DOTENV_EXPORT_RE.match(line)
        if export is not None:
            line = line[export.end() :]
        key, sep, rest = line.partition("=")
        if not sep:
            raise DotenvError(f"line {number}: expected KEY=VALUE")
        key = key.strip()
        if not _DOTENV_KEY_RE.fullmatch(key):
            raise DotenvError(f"line {number}: invalid key {key!r}")
        try:
            values[key] = _parse_dotenv_value(rest.strip())
        except ValueError as exc:
            raise DotenvError(f"line {number}: {exc}") from None
    return values


def read_env_file(path: Path) -> dict[str, str]:
    return load_dotenv(path.read_text(encoding="utf-8"))


def load_settings(environ: Mapping[str, str], env_file: Path | None = None) -> Settings:
    dotenv_values = read_env_file(env_file) if env_file is not None else {}
    return Settings.from_env({**dotenv_values, **environ})


# ---------------------------------------------------------------------------
# Part 2: WSGI app with health and version endpoints
# ---------------------------------------------------------------------------


def get_version(distribution: str = DISTRIBUTION) -> str:
    # Looked up at call time so the metadata backend can be swapped (e.g. in tests).
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return FALLBACK_VERSION


def _json_response(
    start_response: StartResponse,
    method: str,
    status: int,
    payload: object,
    extra_headers: Sequence[tuple[str, str]] = (),
) -> Iterable[bytes]:
    body = json.dumps(payload).encode("utf-8")
    headers = [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
        *extra_headers,
    ]
    start_response(f"{status} {HTTPStatus(status).phrase}", headers)
    return [] if method == "HEAD" else [body]


def create_app(
    settings: Settings,
    *,
    readiness_checks: Mapping[str, ReadinessCheck] | None = None,
) -> WSGIApplication:
    checks = dict(readiness_checks or {})

    def healthz() -> tuple[int, object]:
        return 200, {"status": "ok"}

    def readyz() -> tuple[int, object]:
        results: dict[str, str] = {}
        for name, check in checks.items():
            try:
                check()
            except Exception as exc:
                results[name] = f"error: {type(exc).__name__}: {exc}"
            else:
                results[name] = "ok"
        ready = all(state == "ok" for state in results.values())
        payload = {"status": "ready" if ready else "not_ready", "checks": results}
        return (200 if ready else 503), payload

    def version() -> tuple[int, object]:
        return 200, {"app": settings.app_name, "version": get_version(), "git_sha": settings.git_sha}

    routes: dict[str, Callable[[], tuple[int, object]]] = {
        "/healthz": healthz,
        "/readyz": readyz,
        "/version": version,
    }

    def app(environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        path = environ.get("PATH_INFO", "")
        method = environ.get("REQUEST_METHOD", "GET").upper()
        handler = routes.get(path)
        if handler is None:
            return _json_response(start_response, method, 404, {"error": "not_found", "path": path})
        if method not in ("GET", "HEAD"):
            return _json_response(
                start_response,
                method,
                405,
                {"error": "method_not_allowed"},
                extra_headers=[("Allow", "GET, HEAD")],
            )
        status, payload = handler()
        return _json_response(start_response, method, status, payload)

    return app


# ---------------------------------------------------------------------------
# Part 3: structured JSON access logging
# ---------------------------------------------------------------------------


class _AccessLogBody:
    """Response iterable that emits its access record once, when closed."""

    def __init__(self, body: Iterable[bytes], emit: Callable[[], None]) -> None:
        self._body = body
        self._emit = emit
        self._emitted = False

    def __iter__(self) -> Iterable[bytes]:
        return iter(self._body)

    def close(self) -> None:
        body_close = getattr(self._body, "close", None)
        try:
            if body_close is not None:
                body_close()
        finally:
            if not self._emitted:
                self._emitted = True
                self._emit()


def _request_id(incoming: str) -> str:
    return incoming if _REQUEST_ID_RE.fullmatch(incoming) else uuid.uuid4().hex


def access_log(
    app: WSGIApplication,
    *,
    logger: logging.Logger,
    clock: Callable[[], float] = time.perf_counter,
) -> WSGIApplication:
    def middleware(environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        request_id = _request_id(environ.get("HTTP_X_REQUEST_ID", ""))
        environ[REQUEST_ID_KEY] = request_id
        method = environ.get("REQUEST_METHOD", "")
        path = environ.get("PATH_INFO", "")
        status_seen = {"code": 500}
        started = clock()

        def emit(status: int) -> None:
            duration_ms = round((clock() - started) * 1000, 3)
            record = {
                "method": method,
                "path": path,
                "status": status,
                "duration_ms": duration_ms,
                "request_id": request_id,
            }
            logger.info(json.dumps(record))

        def capturing_start_response(
            status: str,
            headers: list[tuple[str, str]],
            exc_info: Any = None,
        ) -> Callable[[bytes], object]:
            status_seen["code"] = int(status.split(" ", 1)[0])
            return start_response(status, [*headers, (REQUEST_ID_HEADER, request_id)], exc_info)

        try:
            body = app(environ, capturing_start_response)
        except BaseException:
            emit(500)
            raise
        return _AccessLogBody(body, lambda: emit(status_seen["code"]))

    return middleware


# ---------------------------------------------------------------------------
# Part 4: CLI entry point
# ---------------------------------------------------------------------------


class _QuietRequestHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        """Access logging is the middleware's job; keep wsgiref's stderr quiet."""


def build_app(settings: Settings) -> WSGIApplication:
    return access_log(create_app(settings), logger=logging.getLogger("pyref.access"))


def serve(app: WSGIApplication, host: str, port: int) -> int:
    server = make_server(host, port, app, handler_class=_QuietRequestHandler)

    def request_shutdown(_signum: int) -> None:
        # server.shutdown() blocks until serve_forever() returns, so it cannot run
        # on the main thread that is inside serve_forever().
        threading.Thread(target=server.shutdown, name="pyref-shutdown", daemon=True).start()

    restore = install_signal_handlers(request_shutdown)
    try:
        server.serve_forever()
    finally:
        restore()
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=DISTRIBUTION, description="pyref-service command line")
    parser.add_argument("--env-file", type=Path, default=None, metavar="PATH", help="optional .env file")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    serve_cmd = commands.add_parser("serve", help="run the HTTP service")
    serve_cmd.add_argument("--host", default="127.0.0.1", help="bind address (default: %(default)s)")
    serve_cmd.add_argument("--port", type=int, default=None, help="overrides PORT")
    serve_cmd.add_argument("--dry-run", action="store_true", help="report the address without binding")

    commands.add_parser("check-config", help="validate configuration and exit")
    commands.add_parser("version", help="print the package version")
    return parser


def _load_or_report(env_file: Path | None) -> Settings | None:
    try:
        return load_settings(os.environ, env_file)
    except ConfigError as exc:
        for entry in exc.errors:
            print(f"error: {entry}", file=sys.stderr)
    except (DotenvError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
    return None


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if args.command == "version":
        print(get_version())
        return 0

    settings = _load_or_report(args.env_file)
    if settings is None:
        return 2

    if args.command == "check-config":
        print("configuration ok")
        return 0

    host: str = args.host
    port: int = args.port if args.port is not None else settings.port
    if args.dry_run:
        print(f"dry run: {settings.app_name} would listen on {host}:{port}")
        return 0

    logging.basicConfig(level=settings.log_level, format="%(message)s")
    return serve(build_app(settings), host, port)


# ---------------------------------------------------------------------------
# Part 5: graceful shutdown on SIGINT / SIGTERM
# ---------------------------------------------------------------------------


def install_signal_handlers(
    shutdown: Callable[[int], None],
    *,
    signals: Sequence[signal.Signals] = (signal.SIGINT, signal.SIGTERM),
) -> Callable[[], None]:
    fired = False

    def handler(signum: int, frame: FrameType | None) -> None:
        nonlocal fired
        if fired:
            return
        fired = True
        shutdown(signum)

    previous = [(sig, signal.signal(sig, handler)) for sig in signals]
    restored = False

    def restore() -> None:
        nonlocal restored
        if restored:
            return
        restored = True
        for sig, old in previous:
            signal.signal(sig, signal.SIG_DFL if old is None else old)

    return restore


if __name__ == "__main__":
    raise SystemExit(main())
