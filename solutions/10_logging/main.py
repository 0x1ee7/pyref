"""Reference implementation: production logging with the standard library."""

from __future__ import annotations

import contextvars
import json
import logging
import logging.config
import logging.handlers
import os
import queue
import re
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, TextIO

NAMESPACE = "shop"
DEFAULT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

# Attributes every LogRecord carries, plus those Formatter adds while formatting.
# Anything else on a record came from ``extra=`` or from a filter.
_RECORD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "exc_text",
}


# ---------------------------------------------------------------------------
# Part 1: hierarchy, library etiquette, per-module levels
# ---------------------------------------------------------------------------


def get_logger(name: str) -> logging.Logger:
    """Return the logger for ``name``, namespaced under ``shop``."""
    if not name:
        raise ValueError("logger name must be non-empty")
    if name == NAMESPACE or name.startswith(NAMESPACE + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{NAMESPACE}.{name}")


def configure_library_logging() -> None:
    """Make the ``shop`` namespace silent by default, without touching root.

    A library must never add a real handler or set a level: that decision
    belongs to the application. A single NullHandler suppresses the
    "No handlers could be found" fallback.
    """
    library_logger = logging.getLogger(NAMESPACE)
    if not any(isinstance(h, logging.NullHandler) for h in library_logger.handlers):
        library_logger.addHandler(logging.NullHandler())


def effective_level(name: str) -> int:
    """Return the level that actually applies to the ``shop`` logger ``name``."""
    return get_logger(name).getEffectiveLevel()


def _resolve_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    try:
        return logging.getLevelNamesMapping()[level.upper()]
    except KeyError:
        raise ValueError(f"unknown log level: {level!r}") from None


def _check_child_name(name: str) -> None:
    if not name.startswith(NAMESPACE + "."):
        raise ValueError(f"module logger must be 'shop.<name>', got {name!r}")


def set_module_levels(levels: Mapping[str, str | int]) -> None:
    """Override levels for specific ``shop.<name>`` loggers.

    Everything is validated before any logger is touched, so a bad entry
    leaves the configuration unchanged.
    """
    resolved: dict[str, int] = {}
    for name, level in levels.items():
        _check_child_name(name)
        resolved[name] = _resolve_level(level)
    for name, level in resolved.items():
        logging.getLogger(name).setLevel(level)


# ---------------------------------------------------------------------------
# Part 2: idempotent setup and dictConfig
# ---------------------------------------------------------------------------


class _ManagedStreamHandler(logging.StreamHandler):
    """Marks handlers installed by :func:`setup_logging` so they can be replaced."""


def setup_logging(level: str | int = "INFO", *, stream: TextIO | None = None) -> logging.Logger:
    """Attach one console handler to ``shop`` and make it the only output path."""
    resolved = _resolve_level(level)
    logger = logging.getLogger(NAMESPACE)
    for handler in list(logger.handlers):
        if isinstance(handler, _ManagedStreamHandler):
            logger.removeHandler(handler)
            handler.close()
    handler = _ManagedStreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(logging.Formatter(DEFAULT_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(resolved)
    # Without this, every record also reaches the root logger's handlers.
    logger.propagate = False
    return logger


def make_dict_config(
    *,
    level: str = "INFO",
    log_file: Path | None = None,
    module_levels: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build a ``logging.config.dictConfig`` mapping for the ``shop`` namespace."""
    overrides = dict(module_levels or {})
    for name in overrides:
        _check_child_name(name)

    handlers: dict[str, Any] = {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
            "formatter": "plain",
        },
    }
    shop_handlers = ["console"]
    if log_file is not None:
        handlers["file"] = {
            "class": "logging.FileHandler",
            "filename": str(log_file),
            "mode": "a",
            "encoding": "utf-8",
            "formatter": "plain",
        }
        shop_handlers.append("file")

    loggers: dict[str, Any] = {
        NAMESPACE: {"level": level, "handlers": shop_handlers, "propagate": False},
    }
    # Children carry no handlers and propagate to the shop handlers.
    for name, module_level in overrides.items():
        loggers[name] = {"level": module_level}

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"plain": {"format": DEFAULT_FORMAT}},
        "handlers": handlers,
        "loggers": loggers,
    }


# ---------------------------------------------------------------------------
# Part 3: lazy arguments, exception logging, adapters
# ---------------------------------------------------------------------------


def charge(
    logger: logging.Logger,
    order_id: str,
    amount_cents: int,
    gateway: Callable[[str, int], str],
) -> str:
    """Charge an order through ``gateway`` and log the outcome."""
    if amount_cents <= 0:
        raise ValueError(f"amount_cents must be positive, got {amount_cents}")
    # %-args are only rendered if the record is actually emitted.
    logger.debug("charging order %s for %d cents", order_id, amount_cents)
    try:
        txn = gateway(order_id, amount_cents)
    except Exception:
        logger.exception("charge failed for order %s", order_id)
        raise
    logger.info("charged order %s txn=%s", order_id, txn)
    return txn


class _FieldsAdapter(logging.LoggerAdapter):
    """Merges bound fields with per-call ``extra=``; per-call keys win."""

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        kwargs["extra"] = {**self.extra, **kwargs.get("extra", {})}
        return msg, kwargs


def bind_fields(
    logger: logging.Logger | logging.LoggerAdapter,
    **fields: object,
) -> logging.LoggerAdapter:
    """Return a new adapter carrying ``fields``; the input is never mutated."""
    for key in fields:
        if key in _RECORD_ATTRS:
            raise ValueError(f"field name {key!r} clashes with a LogRecord attribute")
    base: logging.Logger
    merged: dict[str, object] = {}
    if isinstance(logger, logging.LoggerAdapter):
        base = logger.logger
        merged.update(logger.extra or {})
    else:
        base = logger
    merged.update(fields)
    return _FieldsAdapter(base, merged)


# ---------------------------------------------------------------------------
# Part 4: structured JSON and request ids
# ---------------------------------------------------------------------------

_CORE_KEYS = frozenset({"timestamp", "level", "logger", "message", "exc_info"})

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)


class JsonFormatter(logging.Formatter):
    """One JSON object per record, with extras promoted to top-level keys."""

    def format(self, record: logging.LogRecord) -> str:
        extras = {
            key: value
            for key, value in vars(record).items()
            if key not in _RECORD_ATTRS and key not in _CORE_KEYS
        }
        payload: dict[str, Any] = {
            **extras,
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        elif record.exc_text:
            # Already rendered, e.g. by a QueueHandler upstream.
            payload["exc_info"] = record.exc_text
        return json.dumps(payload, default=str)


@contextmanager
def bind_request_id(request_id: str) -> Iterator[str]:
    """Bind ``request_id`` for the duration of the block, restoring the outer value."""
    token = request_id_var.set(request_id)
    try:
        yield request_id
    finally:
        request_id_var.reset(token)


class RequestIdFilter(logging.Filter):
    """Stamp ``request_id`` from the context. Explicit ``extra=`` values win."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get() or "-"
        return True


# ---------------------------------------------------------------------------
# Part 5: secret redaction
# ---------------------------------------------------------------------------

SENSITIVE_FIELDS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "access_token",
        "client_secret",
        "authorization",
    }
)

_KV_RE = re.compile(
    r"""(?P<key>\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?token|client[_-]?secret)\b["']?)"""
    r"""(?P<sep>\s*[=:]\s*)"""
    r"""(?:(?P<q>["'])[^"'\n]*(?P=q)|[^\s"',;&]+)""",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(
    r"""(?P<key>\bauthorization["']?)(?P<sep>\s*[=:]\s*)"""
    r"""(?:(?P<q>["'])[^"'\n]*(?P=q)|[^\s"',;&]+(?:\s+[^\s"',;&]+)?)""",
    re.IGNORECASE,
)


def _mask(match: re.Match[str]) -> str:
    quote = match.group("q") or ""
    return f"{match.group('key')}{match.group('sep')}{quote}***{quote}"


def redact(text: str) -> str:
    """Mask secret values in ``key=value``, ``key: "value"`` and Authorization headers."""
    text = _AUTH_RE.sub(_mask, text)
    return _KV_RE.sub(_mask, text)


class RedactingFilter(logging.Filter):
    """Redacts the rendered message and any sensitive-named record attributes.

    Attach to a handler so records from child loggers are covered too.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        for name in list(vars(record)):
            if name.lower() in SENSITIVE_FIELDS:
                setattr(record, name, "***")
        return True


# ---------------------------------------------------------------------------
# Part 6: rotating files, installed idempotently
# ---------------------------------------------------------------------------


def install_rotating_file(
    logger: logging.Logger,
    path: Path,
    *,
    max_bytes: int,
    backup_count: int,
) -> logging.handlers.RotatingFileHandler:
    """Attach a size-rotating file handler to ``logger`` unless one already exists for ``path``."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if backup_count < 0:
        raise ValueError("backup_count must be >= 0")
    target = os.path.abspath(path)
    for handler in logger.handlers:
        if isinstance(handler, logging.handlers.RotatingFileHandler) and handler.baseFilename == target:
            return handler
    handler = logging.handlers.RotatingFileHandler(
        target,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        delay=True,
    )
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    return handler


# ---------------------------------------------------------------------------
# Part 7: non-blocking logging via QueueHandler / QueueListener
# ---------------------------------------------------------------------------


class _DroppingQueueHandler(logging.handlers.QueueHandler):
    """Never blocks the caller: when the queue is full the record is counted and dropped."""

    def __init__(self, target: queue.Queue[logging.LogRecord]) -> None:
        super().__init__(target)
        # Handler.handle() holds the handler lock around emit(), so this
        # counter needs no lock of its own.
        self.dropped = 0

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1


class _BlockingStopListener(logging.handlers.QueueListener):
    """Waits for room for the stop sentinel instead of failing when the queue is full."""

    def enqueue_sentinel(self) -> None:
        self.queue.put(self._sentinel)


class BackgroundLogging:
    """Route records from ``logger`` through a queue to ``handlers`` on a worker thread.

    On exit the queue is drained, the worker is joined, and the target
    handlers are flushed. The target handlers are not closed; the caller owns them.
    """

    def __init__(
        self,
        logger: logging.Logger,
        handlers: Sequence[logging.Handler],
        *,
        queue_size: int = 1000,
    ) -> None:
        if queue_size <= 0:
            raise ValueError("queue_size must be positive")
        self._logger = logger
        self._handlers = tuple(handlers)
        self._queue_size = queue_size
        self._queue_handler: _DroppingQueueHandler | None = None
        self._listener: _BlockingStopListener | None = None

    @property
    def dropped(self) -> int:
        return self._queue_handler.dropped if self._queue_handler is not None else 0

    @property
    def running(self) -> bool:
        return self._listener is not None

    def __enter__(self) -> BackgroundLogging:
        if self._listener is not None:
            raise RuntimeError("BackgroundLogging is already running")
        records: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=self._queue_size)
        self._queue_handler = _DroppingQueueHandler(records)
        self._listener = _BlockingStopListener(
            records, *self._handlers, respect_handler_level=True
        )
        self._listener.start()
        self._logger.addHandler(self._queue_handler)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._queue_handler is not None:
            self._logger.removeHandler(self._queue_handler)
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        for handler in self._handlers:
            handler.flush()
