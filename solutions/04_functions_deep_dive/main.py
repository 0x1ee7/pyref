"""Functions deep dive: closures, decorators, functools, introspection, and dispatch."""

from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import functools
import hashlib
import inspect
import logging
import time
import types
import urllib.parse
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from decimal import Decimal
from enum import Enum
from typing import Any, NamedTuple, ParamSpec, TypeVar, Union, get_args, get_origin, get_type_hints

P = ParamSpec("P")
R = TypeVar("R")


# ---------------------------------------------------------------------------
# Part 1: closures and first-class functions
# ---------------------------------------------------------------------------


def make_multiplier(factor: int) -> Callable[[int], int]:
    """Return a function that multiplies its argument by ``factor``."""

    def multiply(value: int) -> int:
        return value * factor

    return multiply


def make_counter(start: int = 0) -> Callable[[], int]:
    """Return a zero-argument callable yielding ``start``, ``start + 1``, ... per call."""
    count = start

    def next_value() -> int:
        nonlocal count
        value = count
        count += 1
        return value

    return next_value


def compose(*funcs: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """Compose single-argument callables left to right.

    ``compose(f, g)(x) == g(f(x))``. With no arguments the result is identity.
    Non-callable arguments raise ``TypeError`` immediately, not on first call.
    """
    for func in funcs:
        if not callable(func):
            raise TypeError(f"compose() argument {func!r} is not callable")

    def pipeline(value: Any) -> Any:
        for func in funcs:
            value = func(value)
        return value

    return pipeline


def _make_adder(offset: int) -> Callable[[int], int]:
    def add(value: int) -> int:
        return value + offset

    return add


def make_adders(n: int) -> list[Callable[[int], int]]:
    """Return ``n`` adders where adder ``i`` adds ``i``.

    A naive ``[lambda v: v + i for i in range(n)]`` late-binds ``i`` and makes
    every adder add ``n - 1``; a factory function binds each value correctly.
    """
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    return [_make_adder(i) for i in range(n)]


# ---------------------------------------------------------------------------
# Part 2: decorators, optional arguments, functools.wraps
# ---------------------------------------------------------------------------


def timed(
    func: Callable[P, R] | None = None,
    *,
    logger: logging.Logger | None = None,
    level: int = logging.DEBUG,
    clock: Callable[[], float] = time.perf_counter,
) -> Any:
    """Log how long each call takes. Usable as ``@timed`` or ``@timed(...)``.

    The log record is emitted in a ``finally`` so failing calls are timed too;
    exceptions are never swallowed.
    """
    if func is None:
        return functools.partial(timed, logger=logger, level=level, clock=clock)
    if not callable(func):
        raise TypeError(f"timed() decorates a callable, got {func!r}")

    log = logger if logger is not None else logging.getLogger(func.__module__)

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        start = clock()
        try:
            return func(*args, **kwargs)
        finally:
            elapsed = clock() - start
            log.log(level, "%s took %.3fs", func.__qualname__, elapsed)

    return wrapper


# ---------------------------------------------------------------------------
# Part 3: parameterised decorators and stacking
# ---------------------------------------------------------------------------


def retry(
    func: Callable[P, R] | None = None,
    *,
    attempts: int = 3,
    exceptions: type[BaseException] | tuple[type[BaseException], ...] = (Exception,),
    backoff: float = 0.0,
    sleep: Callable[[float], object] = time.sleep,
) -> Any:
    """Retry a call on ``exceptions`` up to ``attempts`` total attempts.

    Between attempts ``sleep(backoff * 2 ** (attempt - 1))`` is called, so the
    delays are ``backoff, 2*backoff, 4*backoff, ...``; ``sleep`` is called even
    when the delay is ``0.0``. The last exception propagates unchanged.
    """
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        raise ValueError(f"attempts must be an int >= 1, got {attempts!r}")
    if func is not None and not callable(func):
        raise TypeError(f"retry() decorates a callable, got {func!r}; pass options as keywords")
    if func is None:
        return functools.partial(
            retry, attempts=attempts, exceptions=exceptions, backoff=backoff, sleep=sleep
        )

    caught = (exceptions,) if isinstance(exceptions, type) else tuple(exceptions)

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        for attempt in range(1, attempts + 1):
            try:
                return func(*args, **kwargs)
            except caught:
                if attempt == attempts:
                    raise
                sleep(backoff * 2 ** (attempt - 1))
        raise AssertionError("unreachable: loop always returns or raises")

    return wrapper


# ---------------------------------------------------------------------------
# Part 4: introspection and runtime annotation checks
# ---------------------------------------------------------------------------


def _type_name(hint: Any) -> str:
    return hint.__name__ if isinstance(hint, type) else str(hint)


def _matches(value: Any, hint: Any) -> bool:
    if hint is Any:
        return True
    origin = get_origin(hint)
    if origin is Union or origin is types.UnionType:
        return any(_matches(value, arg) for arg in get_args(hint))
    if origin is not None:
        hint = origin  # list[int] -> list: container contents are not inspected
    if isinstance(hint, type):
        return isinstance(value, hint)
    return True  # TypeVars, Literal, etc. are not enforced


def _check(qualname: str, name: str, value: Any, hint: Any) -> None:
    if _matches(value, hint):
        return
    if name == "return":
        raise TypeError(
            f"{qualname}() returned {type(value).__name__}, expected {_type_name(hint)}"
        )
    raise TypeError(
        f"{qualname}(): {name!r} must be {_type_name(hint)}, got {type(value).__name__}"
    )


def validate_args(func: Callable[P, R]) -> Callable[P, R]:
    """Enforce ``func``'s annotations at call time.

    Only arguments the caller actually passed are checked (unsupplied defaults
    are not), and the return value is checked when a return annotation exists.
    Missing or extra arguments raise ``TypeError`` from ``Signature.bind``.
    """
    signature = inspect.signature(func)
    hints = get_type_hints(func)
    qualname = func.__qualname__

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        bound = signature.bind(*args, **kwargs)
        for name, value in bound.arguments.items():
            hint = hints.get(name)
            if hint is None:
                continue
            kind = signature.parameters[name].kind
            if kind is inspect.Parameter.VAR_POSITIONAL:
                for item in value:
                    _check(qualname, name, item, hint)
            elif kind is inspect.Parameter.VAR_KEYWORD:
                for item in value.values():
                    _check(qualname, name, item, hint)
            else:
                _check(qualname, name, value, hint)
        result = func(*args, **kwargs)
        if "return" in hints:
            _check(qualname, "return", result, hints["return"])
        return result

    return wrapper


def signature_summary(func: Callable[..., Any], *, follow_wrapped: bool = True) -> dict[str, Any]:
    """Summarise a callable's introspectable shape.

    Keys:
      ``name``: ``func.__name__``.
      ``parameters``: list of ``(name, kind_name, has_default)`` tuples from
          ``inspect.signature(func, follow_wrapped=follow_wrapped)``.
      ``defaults``: the raw ``func.__defaults__`` attribute, or ``None``.
      ``wrapped_depth``: how many ``__wrapped__`` hops lead to the innermost callable.
    """
    signature = inspect.signature(func, follow_wrapped=follow_wrapped)
    depth = 0
    current: Any = func
    while hasattr(current, "__wrapped__"):
        current = current.__wrapped__
        depth += 1
    return {
        "name": func.__name__,
        "parameters": [
            (param.name, param.kind.name, param.default is not inspect.Parameter.empty)
            for param in signature.parameters.values()
        ],
        "defaults": getattr(func, "__defaults__", None),
        "wrapped_depth": depth,
    }


# ---------------------------------------------------------------------------
# Part 5: class-based decorators and method decorators
# ---------------------------------------------------------------------------


class CallCounter:
    """Count calls to a function or method. State lives on the decorator object.

    ``__get__`` makes it binding-aware, so it also works on methods. The count is
    shared by every instance because there is only one decorator object per method.
    """

    def __init__(self, func: Callable[..., Any]) -> None:
        functools.update_wrapper(self, func)
        self.func = func
        self.count = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.count += 1
        return self.func(*args, **kwargs)

    def __get__(self, instance: object | None, owner: type | None = None) -> Any:
        if instance is None:
            return self
        return types.MethodType(self, instance)

    def reset(self) -> None:
        self.count = 0


class RateLimitExceeded(RuntimeError):
    """Raised when a rate-limited method is called too often."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limit exceeded; retry after {retry_after:.3f}s")
        self.retry_after = retry_after


def rate_limited(
    max_calls: int, period: float, *, clock: Callable[[], float] = time.monotonic
) -> Callable[[Callable[..., R]], Callable[..., R]]:
    """Sliding-window limit of ``max_calls`` per ``period`` seconds, per instance.

    The window is stored in the instance's ``__dict__`` so two objects never
    share a budget. Rejected calls are not recorded. Requires instances with a
    ``__dict__`` (no ``__slots__`` without ``__dict__``).
    """
    if max_calls < 1:
        raise ValueError(f"max_calls must be >= 1, got {max_calls}")
    if period <= 0:
        raise ValueError(f"period must be positive, got {period}")

    def decorator(method: Callable[..., R]) -> Callable[..., R]:
        key = f"_rate_limit[{method.__qualname__}]"

        @functools.wraps(method)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> R:
            window: collections.deque[float] = self.__dict__.setdefault(key, collections.deque())
            now = clock()
            while window and now - window[0] >= period:
                window.popleft()
            if len(window) >= max_calls:
                raise RateLimitExceeded(window[0] + period - now)
            window.append(now)
            return method(self, *args, **kwargs)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Part 6: functools toolkit
# ---------------------------------------------------------------------------


class CacheInfo(NamedTuple):
    hits: int
    misses: int
    maxsize: int | None
    currsize: int


def ttl_cache(
    seconds: float, *, clock: Callable[[], float] = time.monotonic, maxsize: int | None = None
) -> Callable[[Callable[..., R]], Callable[..., R]]:
    """Memoise by ``(args, sorted kwargs)`` with a time-to-live per entry.

    An entry is valid while ``clock() < stored_at + seconds``. With ``maxsize``
    set, the least recently used entry is evicted when the cache overflows.
    Positional and keyword spellings of the same call are distinct keys.
    Unhashable arguments raise ``TypeError``.
    """
    if seconds <= 0:
        raise ValueError(f"seconds must be positive, got {seconds}")
    if maxsize is not None and maxsize < 1:
        raise ValueError(f"maxsize must be None or >= 1, got {maxsize}")

    def decorator(func: Callable[..., R]) -> Callable[..., R]:
        store: OrderedDict[Hashable, tuple[R, float]] = OrderedDict()
        hits = 0
        misses = 0

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> R:
            nonlocal hits, misses
            key = (args, tuple(sorted(kwargs.items())))
            now = clock()
            entry = store.get(key)
            if entry is not None and now < entry[1]:
                hits += 1
                store.move_to_end(key)
                return entry[0]
            misses += 1
            value = func(*args, **kwargs)
            store[key] = (value, now + seconds)
            store.move_to_end(key)
            if maxsize is not None:
                while len(store) > maxsize:
                    store.popitem(last=False)
            return value

        def cache_info() -> CacheInfo:
            return CacheInfo(hits, misses, maxsize, len(store))

        def cache_clear() -> None:
            nonlocal hits, misses
            store.clear()
            hits = misses = 0

        wrapper.cache_info = cache_info  # type: ignore[attr-defined]
        wrapper.cache_clear = cache_clear  # type: ignore[attr-defined]
        return wrapper

    return decorator


@functools.total_ordering
class Version:
    """Semantic-style ``MAJOR.MINOR.PATCH`` version; comparisons are numeric."""

    __slots__ = ("major", "minor", "patch")

    def __init__(self, major: int, minor: int, patch: int) -> None:
        for label, value in (("major", major), ("minor", minor), ("patch", patch)):
            if value < 0:
                raise ValueError(f"{label} must be >= 0, got {value}")
        self.major = major
        self.minor = minor
        self.patch = patch

    @classmethod
    def parse(cls, text: str) -> Version:
        raw = text[1:] if text.startswith("v") else text
        parts = raw.split(".")
        if len(parts) != 3 or not all(part.isascii() and part.isdigit() for part in parts):
            raise ValueError(f"invalid version {text!r}")
        return cls(int(parts[0]), int(parts[1]), int(parts[2]))

    def _key(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() == other._key()

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() < other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def __repr__(self) -> str:
        return f"Version({self.major}, {self.minor}, {self.patch})"


class Release:
    """A release whose checksum is computed once, on first access."""

    def __init__(self, version: Version, artifacts: Iterable[str]) -> None:
        self.version = version
        self.artifacts = list(artifacts)

    @functools.cached_property
    def checksum(self) -> str:
        manifest = "\n".join(sorted(self.artifacts))
        return hashlib.sha256(manifest.encode()).hexdigest()


def _merge_two(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_two(existing, value)
        else:
            merged[key] = value
    return merged


def merge_settings(*layers: Mapping[str, Any]) -> dict[str, Any]:
    """Fold layers left to right; later layers win; nested mappings merge recursively.

    Inputs are never mutated.
    """
    return functools.reduce(_merge_two, layers, {})


def _build_url(base: str, *segments: str, **query: str) -> str:
    path = "/".join(segment.strip("/") for segment in segments if segment)
    root = base.rstrip("/")
    url = f"{root}/{path}" if path else root
    if query:
        url += "?" + urllib.parse.urlencode(sorted(query.items()))
    return url


def make_url_builder(base: str) -> Callable[..., str]:
    """Return a ``partial`` of the URL builder with ``base`` pre-bound."""
    return functools.partial(_build_url, base)


# ---------------------------------------------------------------------------
# Part 7: singledispatch serialisation and a command registry
# ---------------------------------------------------------------------------


@functools.singledispatch
def to_jsonish(obj: object) -> Any:
    """Convert ``obj`` into JSON-compatible primitives.

    Unregistered dataclass instances are serialised field by field; anything
    else raises ``TypeError``.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {field.name: to_jsonish(getattr(obj, field.name)) for field in dataclasses.fields(obj)}
    raise TypeError(f"cannot serialise {type(obj).__qualname__}")


@to_jsonish.register(str)
@to_jsonish.register(int)  # also covers bool, which is an int subclass
@to_jsonish.register(float)
@to_jsonish.register(type(None))
def _passthrough(obj: Any) -> Any:
    return obj


@to_jsonish.register(Decimal)
def _decimal(obj: Decimal) -> str:
    return str(obj)  # string keeps precision that a float would lose


# datetime is a subclass of date; singledispatch picks the most specific class.
@to_jsonish.register(dt.date)
def _date(obj: dt.date) -> str:
    return obj.isoformat()


@to_jsonish.register(dt.datetime)
def _datetime(obj: dt.datetime) -> str:
    return obj.isoformat()


@to_jsonish.register(Enum)
def _enum(obj: Enum) -> Any:
    return to_jsonish(obj.value)


@to_jsonish.register(list)
@to_jsonish.register(tuple)
def _sequence(obj: Sequence[Any]) -> list[Any]:
    return [to_jsonish(item) for item in obj]


@to_jsonish.register(set)
@to_jsonish.register(frozenset)
def _set(obj: Iterable[Any]) -> list[Any]:
    items = [to_jsonish(item) for item in obj]
    try:
        return sorted(items)
    except TypeError:  # mixed types: fall back to a stable textual order
        return sorted(items, key=repr)


@to_jsonish.register(dict)
def _dict(obj: dict[Any, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in obj.items():
        if not isinstance(key, str):
            raise TypeError(f"JSON object keys must be str, got {type(key).__qualname__}")
        result[key] = to_jsonish(value)
    return result


class UnknownCommand(KeyError):
    """No command is registered under the requested name."""


class CommandRegistry:
    """Maps command names to callables. Each instance is independent."""

    def __init__(self) -> None:
        self._commands: dict[str, Callable[..., Any]] = {}

    def command(self, name: str | Callable[..., Any] | None = None) -> Any:
        """Register a function. Usable as ``@reg.command``, ``@reg.command()`` or ``@reg.command("name")``.

        The default name is the function name with underscores replaced by dashes.
        The decorator returns the function unchanged.
        """
        if callable(name):
            return self._register(name, None)
        if name is not None and not isinstance(name, str):
            raise TypeError(f"command name must be str, got {type(name).__qualname__}")

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            return self._register(func, name)

        return decorator

    def _register(self, func: Callable[..., Any], name: str | None) -> Callable[..., Any]:
        key = name if name is not None else func.__name__.replace("_", "-")
        if key in self._commands:
            raise ValueError(f"command {key!r} is already registered")
        self._commands[key] = func
        return func

    def _lookup(self, name: str) -> Callable[..., Any]:
        try:
            return self._commands[name]
        except KeyError:
            raise UnknownCommand(name) from None

    def dispatch(self, name: str, *args: Any, **kwargs: Any) -> Any:
        return self._lookup(name)(*args, **kwargs)

    def names(self) -> list[str]:
        return sorted(self._commands)

    def help(self, name: str) -> str:
        """First line of the command's docstring, or ``""`` if it has none."""
        doc = inspect.getdoc(self._lookup(name))
        return doc.splitlines()[0] if doc else ""
