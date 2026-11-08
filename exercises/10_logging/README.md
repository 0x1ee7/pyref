# 10 · Production logging with the standard library

You are adding logging to **shop**, a small order-processing service that
other teams import as a library and that also runs as an application. The
library side must stay silent and never hijack the host's logging; the
application side needs console and rotating-file output, JSON for the log
pipeline, request ids for tracing, redaction so credentials never reach disk,
and a non-blocking path so a slow disk cannot stall request handling.

Implement everything in `main.py`. The package namespace is `shop`; every
module logger lives under it (`get_logger("db")` → `shop.db`).

## Part 1: hierarchy, library etiquette, per-module levels

- `get_logger(name: str) -> logging.Logger`
  - `"payments"` → logger `shop.payments`; `"shop.db"` → `shop.db`;
    `"shop"` → `shop`; `"shopping"` → `shop.shopping` (prefix match is on the
    dotted boundary only).
  - Empty name → `ValueError`.
  - Returns the stdlib cached instance (`logging.getLogger` identity holds).
- `configure_library_logging() -> None`: attach **exactly one**
  `logging.NullHandler` to `shop`, however many times it is called. It must not
  touch the root logger, add any other handler, or set a level.
- `effective_level(name: str) -> int`: the level that applies to `shop.<name>`
  (inherits from `shop` when unset).
- `set_module_levels(levels: Mapping[str, str | int]) -> None`
  - Keys must be `shop.<something>`; otherwise `ValueError`.
  - Values are level names (case-insensitive, `"debug"` is fine) or ints.
    Unknown names → `ValueError`.
  - All-or-nothing: validate every entry before changing any logger.

## Part 2: idempotent setup and dictConfig

- `setup_logging(level: str | int = "INFO", *, stream: TextIO | None = None) -> logging.Logger`
  - Configures `shop`: sets its level, attaches one console handler writing to
    `stream` (default: `sys.stderr` as it is at call time), sets
    `propagate = False` so records do not also reach root handlers.
  - Repeated calls replace the previous console handler; they never stack. The
    replaced handler is closed. Does not touch root.
  - Format: `"%(asctime)s %(levelname)-8s %(name)s: %(message)s"`.
  - Unknown level → `ValueError`.
- `make_dict_config(*, level: str = "INFO", log_file: Path | None = None, module_levels: Mapping[str, str] | None = None) -> dict[str, Any]`
  - Returns a valid `logging.config.dictConfig` mapping, version 1.
  - `disable_existing_loggers` is `False`. There is **no** `root` key.
  - Handlers: `console` (`logging.StreamHandler`, `ext://sys.stderr`) always;
    `file` (`logging.FileHandler`, UTF-8, append) when `log_file` is given.
    Both use the formatter `plain` with the format from Part 2 above.
  - `shop` logger: `level`, both handlers, `propagate: False`.
  - `module_levels` entries become child logger configs with only a `level`
    (they inherit the `shop` handlers through propagation). Keys outside
    `shop.<name>` → `ValueError`.

## Part 3: lazy arguments, exception logging, adapters

- `charge(logger, order_id: str, amount_cents: int, gateway: Callable[[str, int], str]) -> str`
  - `amount_cents <= 0` → `ValueError`, before calling the gateway.
  - Logs at DEBUG `"charging order %s for %d cents"` with `%`-style args
    (args must be passed to the logger, not formatted by you; disabled levels
    must not render them).
  - Calls `gateway(order_id, amount_cents)`. On exception: `logger.exception`
    with `"charge failed for order %s"` (traceback attached), then re-raise the
    same exception object.
  - On success logs at INFO `"charged order %s txn=%s"` and returns the txn id.
- `bind_fields(logger: logging.Logger | logging.LoggerAdapter, **fields: object) -> logging.LoggerAdapter`
  - Returns a new `LoggerAdapter` whose records carry `fields` as attributes
    (via `extra`). Per-call `extra=` overrides bound fields with the same name.
  - Binding on top of an adapter merges fields; the parent is never mutated.
  - Field names that clash with `LogRecord` attributes (`msg`, `args`,
    `levelname`, `message`, ...) → `ValueError`.

## Part 4: structured JSON and request ids

- `JsonFormatter(logging.Formatter)`: `format()` returns one JSON object per
  record, with:
  - core keys `timestamp` (UTC, ISO 8601, millisecond precision, e.g.
    `2026-01-01T00:00:00.123+00:00`), `level`, `logger`, `message` (rendered
    with `getMessage()`);
  - every `extra` attribute promoted to a top-level key;
  - `exc_info` as the formatted traceback string when the record has one;
  - non-JSON-serialisable values rendered with `str()`;
  - core keys cannot be overridden by `extra`;
  - a record with a traceback is still exactly one line.
- `request_id_var: contextvars.ContextVar[str | None]` (default `None`).
- `bind_request_id(request_id: str)`: context manager that sets the context
  var for the block and restores the previous value on exit (also on
  exception). Yields the id.
- `RequestIdFilter(logging.Filter)`: if the record has no `request_id`
  attribute, sets it from the context var, or `"-"` when unset. An explicit
  `extra={"request_id": ...}` wins. Always returns `True`.

## Part 5: secret redaction

- `redact(text: str) -> str`: masks secret values, keeping the key and
  separator:
  - `key=value`, `key: value`, `key: "value"`, `key='value with spaces'` for
    keys `password`, `passwd`, `secret`, `token`, `api_key`/`api-key`,
    `access_token`, `client_secret` (whole words only, so `tokens-bucket`
    and `secretary=ok` are untouched);
  - `Authorization` headers, including quoted dict reprs such as
    `{'Authorization': 'Bearer abc'}` and `Authorization: Basic xyz`.
  - Masked value is `***`, quotes preserved.
- `RedactingFilter(logging.Filter)`: rewrites the rendered message through
  `redact`, clears `args`, and masks record attributes whose names are secret
  (case-insensitive, same key set as `redact`). Never drops records; always
  returns `True`. Attach it to a handler so records from child loggers are
  covered.

## Part 6: rotating files, installed idempotently

- `install_rotating_file(logger, path: Path, *, max_bytes: int, backup_count: int) -> logging.handlers.RotatingFileHandler`
  - `max_bytes <= 0` or `backup_count < 0` → `ValueError`.
  - Creates a `RotatingFileHandler` (UTF-8, `delay=True` so no file exists
    until the first record) with format `"%(levelname)s %(name)s %(message)s"`,
    and attaches it to `logger`.
  - Idempotent per absolute path: a second call for the same path returns the
    existing handler; a different path gets its own handler.
  - Rotation keeps at most `backup_count` backups (`x.log.1` … `x.log.N`).

## Part 7: non-blocking logging via QueueHandler / QueueListener

- `BackgroundLogging(logger, handlers: Sequence[logging.Handler], *, queue_size: int = 1000)`
  is a context manager:
  - `queue_size <= 0` → `ValueError`.
  - On enter: a bounded queue, a `QueueHandler` attached to `logger`, and a
    `QueueListener` (with `respect_handler_level=True`) delivering to
    `handlers` on a worker thread. Entering a running instance → `RuntimeError`.
  - Records are rendered at enqueue time (`QueueHandler.prepare`), so later
    mutation of arguments does not leak into the output.
  - The caller never blocks: when the queue is full, the record is dropped and
    counted. `dropped` property exposes the count; `running` is `True` while
    active.
  - On exit (including via exception): detach the `QueueHandler`, drain the
    queue, join the worker, flush the target handlers. Records accepted before
    exit are delivered. Target handlers are **not** closed; the caller owns
    them. `running` is `False` afterwards.

## Concepts exercised

- Logger hierarchy, `propagate`, `NullHandler` and library etiquette
- Levels, effective levels, per-module overrides, validation before mutation
- Idempotent handler management and `dictConfig` (`disable_existing_loggers`)
- Lazy `%`-args, `logger.exception`, `LoggerAdapter` and `extra`
- Custom `Formatter` (JSON, UTC timestamps, `formatException`), `LogRecord` internals
- `contextvars` for request-scoped state, `Filter` on handlers vs loggers
- Regex-based redaction, `Filter` mutation of records
- `RotatingFileHandler` with `delay=True`, Windows-safe rotation
- `QueueHandler` / `QueueListener`, bounded queues, clean shutdown

## Run

```
uv run pytest exercises/10_logging                 # all parts, should fail until implemented
uv run pytest exercises/10_logging -k part4        # one part
uv run pytest exercises/10_logging --solutions     # grade the reference implementation
```

## Stretch (untested)

- Add a `sampling` filter that keeps 1 in N DEBUG records per logger.
- Emit `logging.handlers.SysLogHandler` configuration for a Unix target and
  guard it behind `sys.platform`.
- Replace `BackgroundLogging`'s drop-on-full policy with a bounded wait and a
  counter for timeouts; compare latency under load.
