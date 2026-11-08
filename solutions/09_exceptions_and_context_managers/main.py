"""Reference implementation: exceptions and context managers.

Scenarios: a config-loading error-translation layer, a dotted-key settings
reader, a try/else/finally step runner, an in-memory transaction with
savepoints, contextlib-based helpers, and a batch runner that reports
failures as an ExceptionGroup and triages them into transient and permanent.
"""

from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableMapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext, redirect_stdout
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Protocol, TextIO, TypeVar

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Part 1: domain exception hierarchy and translation
# ---------------------------------------------------------------------------


class DomainError(Exception):
    """Base class for every error raised by this module."""


class ConfigError(DomainError):
    """A configuration file or setting is unusable."""


class ConfigNotFound(ConfigError):
    """The config file does not exist."""


class ConfigUnreadable(ConfigError):
    """The config file exists but cannot be read (permissions, a directory, ...)."""


class ConfigInvalid(ConfigError):
    """The config file or a setting has the wrong shape, encoding, or type."""


class SettingMissing(ConfigError):
    """A dotted setting key is not present."""

    def __init__(self, key: str) -> None:
        # The key is the only positional arg, so pickling calls cls(key) and
        # round-trips. Passing a formatted message here would break unpickling.
        super().__init__(key)
        self.key = key

    def __str__(self) -> str:
        return f"setting {self.key!r} is not defined"


def load_config(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read a UTF-8 JSON config file whose root must be an object."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as err:
        raise ConfigNotFound(f"config file not found: {path}") from err
    except UnicodeDecodeError as err:
        raise ConfigInvalid(f"config file is not valid UTF-8: {path}") from err
    except OSError as err:
        raise ConfigUnreadable(f"cannot read config file: {path}") from err

    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise ConfigInvalid(f"invalid JSON in {path}: {err.msg} (line {err.lineno})") from err

    if not isinstance(data, dict):
        # Not raised inside an except block, so there is no implicit context to clear.
        raise ConfigInvalid(f"config root must be an object, not {type(data).__name__}")
    return data


# ---------------------------------------------------------------------------
# Part 2: chaining, notes, and walking the cause/context chain
# ---------------------------------------------------------------------------


def get_setting(
    config: Mapping[str, Any],
    dotted_key: str,
    *,
    cast: Callable[[Any], T] | None = None,
) -> Any:
    """Return ``config`` value at a dotted path, optionally converted by ``cast``."""
    node: Any = config
    try:
        for part in dotted_key.split("."):
            node = node[part]
    except (KeyError, TypeError):
        # Hide the internal lookup failure; the public error is SettingMissing.
        raise SettingMissing(dotted_key) from None

    if cast is None:
        return node
    try:
        return cast(node)
    except (ValueError, TypeError) as err:
        wrapped = ConfigInvalid(f"setting {dotted_key!r} has an unusable value: {err}")
        wrapped.add_note(f"raw value of {dotted_key!r}: {node!r}")
        raise wrapped from err


def describe_chain(exc: BaseException) -> list[str]:
    """Render ``exc`` and the exceptions it chains to, outermost first.

    Follows ``__cause__`` when set, otherwise ``__context__`` unless it was
    suppressed. Stops on cycles.
    """
    lines: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current)
        name = type(current).__name__
        lines.append(f"{name}: {message}" if message else name)
        if current.__cause__ is not None:
            current = current.__cause__
        elif current.__suppress_context__:
            current = None
        else:
            current = current.__context__
    return lines


# ---------------------------------------------------------------------------
# Part 3: try / else / finally semantics
# ---------------------------------------------------------------------------


class StepFailed(DomainError):
    """A named step raised an ordinary Exception."""

    def __init__(self, step_name: str) -> None:
        super().__init__(step_name)
        self.step_name = step_name

    def __str__(self) -> str:
        return f"step {self.step_name!r} failed"


def run_checked(name: str, step: Callable[[], T], log: list[str]) -> T:
    """Run ``step`` and record start / ok-or-failed / end in ``log``.

    ``ok`` is logged in ``else`` so a failure in the step never reports success.
    ``end`` is logged in ``finally`` so it appears even for BaseException
    (KeyboardInterrupt, SystemExit), which is not wrapped.
    """
    log.append(f"start {name}")
    try:
        result = step()
    except Exception as err:
        log.append(f"failed {name}")
        raise StepFailed(name) from err
    else:
        log.append(f"ok {name}")
        return result
    finally:
        log.append(f"end {name}")


# ---------------------------------------------------------------------------
# Part 4: class-based context manager with savepoints
# ---------------------------------------------------------------------------


class SavepointRollback(DomainError):
    """Raise inside ``tx.savepoint()`` to discard that block's changes and continue."""


_DELETED: Final[object] = object()


class Transaction:
    """Buffer writes against a mapping; apply them atomically on clean exit."""

    def __init__(self, store: MutableMapping[str, Any]) -> None:
        self._store = store
        self._pending: dict[str, Any] = {}
        self._active = False

    def __enter__(self) -> Transaction:
        if self._active:
            raise RuntimeError("transaction is not reentrant")
        self._active = True
        self._pending = {}
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        try:
            if exc_type is None:
                for key, value in self._pending.items():
                    if value is _DELETED:
                        self._store.pop(key, None)
                    else:
                        self._store[key] = value
        finally:
            # Clean exit commits; any exception rolls back by discarding pending.
            self._pending = {}
            self._active = False
        return False  # never suppress

    def _require_active(self) -> None:
        if not self._active:
            raise RuntimeError("transaction is not active")

    def get(self, key: str) -> Any:
        self._require_active()
        if key in self._pending:
            value = self._pending[key]
            if value is _DELETED:
                raise KeyError(key)
            return value
        return self._store[key]

    def set(self, key: str, value: Any) -> None:
        self._require_active()
        self._pending[key] = value

    def delete(self, key: str) -> None:
        self.get(key)  # raises KeyError if absent; also checks active
        self._pending[key] = _DELETED

    def savepoint(self) -> _Savepoint:
        self._require_active()
        return _Savepoint(self)


class _Savepoint:
    """Snapshot the pending writes on entry; restore them on error."""

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx
        self._snapshot: dict[str, Any] = {}

    def __enter__(self) -> None:
        self._snapshot = dict(self._tx._pending)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if exc_type is None:
            return False  # keep this block's writes
        self._tx._pending.clear()
        self._tx._pending.update(self._snapshot)
        return issubclass(exc_type, SavepointRollback)  # suppress only the rollback signal


# ---------------------------------------------------------------------------
# Part 5: contextmanager generators and ExitStack
# ---------------------------------------------------------------------------


@contextmanager
def temporary_env(**variables: str | None) -> Iterator[None]:
    """Set env vars for the block; ``None`` unsets. Originals are restored on any exit."""
    saved: dict[str, str | None] = {name: os.environ.get(name) for name in variables}
    try:
        for name, value in variables.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, original in saved.items():
            if original is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = original


@contextmanager
def open_all(paths: Sequence[str | os.PathLike[str]], *, encoding: str = "utf-8") -> Iterator[list[TextIO]]:
    """Open every path for reading. If one fails, close the ones already opened."""
    with contextlib.ExitStack() as stack:
        files = [stack.enter_context(open(path, encoding=encoding)) for path in paths]
        yield files


def unlink_all(paths: Iterable[str | os.PathLike[str]]) -> int:
    """Delete each file; missing files are skipped. Returns how many were removed."""
    deleted = 0
    for path in paths:
        with contextlib.suppress(FileNotFoundError):
            Path(path).unlink()
            deleted += 1
    return deleted


class Timer:
    """Elapsed time for one ``timer()`` block: live while running, frozen on exit."""

    def __init__(self, label: str, clock: Callable[[], float]) -> None:
        self.label = label
        self._clock = clock
        self._start = clock()
        self._frozen: float | None = None

    @property
    def elapsed(self) -> float:
        if self._frozen is not None:
            return self._frozen
        return self._clock() - self._start

    def _stop(self) -> None:
        if self._frozen is None:
            self._frozen = self._clock() - self._start


@contextmanager
def timer(
    label: str,
    *,
    clock: Callable[[], float] = time.perf_counter,
    sink: Callable[[str, float], None] | None = None,
) -> Iterator[Timer]:
    """Time a block with an injectable clock; ``sink`` receives (label, elapsed) on exit."""
    t = Timer(label, clock)
    try:
        yield t
    finally:
        t._stop()
        if sink is not None:
            sink(label, t.elapsed)


# ---------------------------------------------------------------------------
# Part 6: contextlib.closing, nullcontext, redirect_stdout, chdir
# ---------------------------------------------------------------------------


class SupportsClose(Protocol):
    def close(self) -> None: ...


R = TypeVar("R", bound=SupportsClose)


def with_resource(factory: Callable[[], R], consume: Callable[[R], T]) -> T:
    """Create a resource, pass it to ``consume``, and always close it."""
    with contextlib.closing(factory()) as resource:
        return consume(resource)


def lock_or_null(lock: AbstractContextManager[Any] | None) -> AbstractContextManager[Any]:
    """Return ``lock`` when given, otherwise a no-op context manager."""
    return nullcontext() if lock is None else lock


def capture_stdout(fn: Callable[..., T], *args: Any, **kwargs: Any) -> tuple[T, str]:
    """Run ``fn`` with stdout captured. On error, attach the captured text as a note."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        try:
            result = fn(*args, **kwargs)
        except Exception as err:
            err.add_note(f"captured stdout: {buffer.getvalue()!r}")
            raise
    return result, buffer.getvalue()


def list_names_in(directory: str | os.PathLike[str]) -> list[str]:
    """Sorted entry names of ``directory``. The cwd is restored even on error."""
    with contextlib.chdir(directory):
        return sorted(os.listdir("."))


# ---------------------------------------------------------------------------
# Part 7: ExceptionGroup batches, retry classification, triage
# ---------------------------------------------------------------------------


class RetryExhausted(DomainError):
    """A transient failure persisted through every allowed attempt."""

    def __init__(self, attempts: int) -> None:
        super().__init__(attempts)
        self.attempts = attempts

    def __str__(self) -> str:
        return f"gave up after {self.attempts} attempts"


_TRANSIENT_ERRNOS: Final = frozenset({errno.EAGAIN, errno.EBUSY})


def is_transient(exc: BaseException) -> bool:
    """True for failures worth retrying: timeouts, connection drops, busy resources."""
    if isinstance(exc, (TimeoutError, ConnectionError, InterruptedError)):
        return True
    return isinstance(exc, OSError) and exc.errno in _TRANSIENT_ERRNOS


def run_batch(tasks: Sequence[tuple[str, Callable[[], T]]]) -> dict[str, T]:
    """Run every task, then raise one ExceptionGroup if any failed.

    ``BaseException`` (e.g. KeyboardInterrupt) is not collected; it stops the
    batch immediately, as it should.
    """
    names = [name for name, _ in tasks]
    if len(set(names)) != len(names):
        raise ValueError("task names must be unique")

    results: dict[str, T] = {}
    failures: list[Exception] = []
    for name, task in tasks:
        try:
            results[name] = task()
        except Exception as err:
            err.add_note(f"task {name!r} failed")
            failures.append(err)

    if failures:
        raise ExceptionGroup(f"{len(failures)} of {len(tasks)} tasks failed", failures)
    return results


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int,
    base_delay: float,
    sleep: Callable[[float], None] = time.sleep,
    classify: Callable[[BaseException], bool] = is_transient,
) -> T:
    """Call ``fn`` up to ``attempts`` times, sleeping ``base_delay * 2**n`` between transient failures."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as err:
            if not classify(err):
                raise
            if attempt == attempts:
                raise RetryExhausted(attempts) from err
            sleep(base_delay * 2 ** (attempt - 1))
    raise AssertionError("unreachable: loop always returns or raises")


def triage(group: ExceptionGroup) -> tuple[ExceptionGroup | None, ExceptionGroup | None]:
    """Split a group into (transient, permanent) subgroups; None where empty."""
    return group.split(is_transient)
