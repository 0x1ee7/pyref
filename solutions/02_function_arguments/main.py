"""Function arguments: reference implementation for the exercises in README.md."""

from __future__ import annotations

import inspect
import math
import operator
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import update_wrapper
from typing import Any, Final

# ---------------------------------------------------------------------------
# Part 1: HTTP request builder
# ---------------------------------------------------------------------------

_ALLOWED_METHODS: Final = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
DEFAULT_TIMEOUT: Final = 10.0


@dataclass(frozen=True, slots=True)
class Request:
    """A fully validated HTTP request description."""

    method: str
    url: str
    headers: dict[str, str]
    params: dict[str, str]
    timeout: float
    extra: dict[str, Any]


def build_request(
    method: str,
    url: str,
    /,
    *,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, object] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    **extra: Any,
) -> Request:
    """Validate and normalise the pieces of an HTTP request.

    ``method`` and ``url`` are positional-only so that ``method=`` can travel
    in ``extra`` without clashing with the verb.
    """
    if not isinstance(method, str):
        raise TypeError(f"method must be str, got {type(method).__name__}")
    verb = method.upper()
    if verb not in _ALLOWED_METHODS:
        raise ValueError(f"unsupported HTTP method: {method!r}")

    if not isinstance(url, str):
        raise TypeError(f"url must be str, got {type(url).__name__}")
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"url must be http(s): {url!r}")

    return Request(
        method=verb,
        url=url,
        headers=_normalise_headers(headers),
        params=_render_params(params),
        timeout=_check_timeout(timeout),
        extra=dict(extra),
    )


def _normalise_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if headers is None:
        return {}
    if not isinstance(headers, Mapping):
        raise TypeError("headers must be a mapping")
    out: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError("header names and values must be str")
        name = key.lower()
        if name in out:
            raise ValueError(f"duplicate header after normalisation: {name!r}")
        out[name] = value
    return out


def _render_params(params: Mapping[str, object] | None) -> dict[str, str]:
    if params is None:
        return {}
    if not isinstance(params, Mapping):
        raise TypeError("params must be a mapping")
    out: dict[str, str] = {}
    for key, value in params.items():
        if not isinstance(key, str):
            raise TypeError("param names must be str")
        out[key] = _render_param(value)
    return out


def _render_param(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    raise TypeError(f"unsupported param value type: {type(value).__name__}")


def _check_timeout(timeout: object) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TypeError(f"timeout must be a number, got {type(timeout).__name__}")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"timeout must be finite and positive, got {timeout!r}")
    return float(timeout)


# ---------------------------------------------------------------------------
# Part 2: SQL-ish query builder
# ---------------------------------------------------------------------------

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?")


def _identifier(name: object, role: str) -> str:
    if not isinstance(name, str):
        raise TypeError(f"{role} must be str, got {type(name).__name__}")
    if _IDENTIFIER.fullmatch(name) is None:
        raise ValueError(f"invalid {role}: {name!r}")
    return name


def select(
    *columns: str,
    table: str,
    where: Mapping[str, object] | None = None,
    order_by: str | Iterable[str] = (),
    limit: int | None = None,
) -> tuple[str, tuple[object, ...]]:
    """Render a SELECT statement with placeholders; values never touch the SQL text."""
    projection = ", ".join(_identifier(c, "column") for c in columns) or "*"
    sql = [f"SELECT {projection} FROM {_identifier(table, 'table')}"]
    params: list[object] = []

    if where is not None:
        if not isinstance(where, Mapping):
            raise TypeError("where must be a mapping")
        clauses: list[str] = []
        for column, value in where.items():
            name = _identifier(column, "column")
            if value is None:
                clauses.append(f"{name} IS NULL")
            else:
                clauses.append(f"{name} = ?")
                params.append(value)
        if clauses:
            sql.append("WHERE " + " AND ".join(clauses))

    terms = [order_by] if isinstance(order_by, str) else list(order_by)
    if terms:
        rendered: list[str] = []
        for term in terms:
            if not isinstance(term, str):
                raise TypeError(f"order_by terms must be str, got {type(term).__name__}")
            descending = term.startswith("-")
            name = _identifier(term.removeprefix("-"), "order_by column")
            rendered.append(f"{name} DESC" if descending else name)
        sql.append("ORDER BY " + ", ".join(rendered))

    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError(f"limit must be int, got {type(limit).__name__}")
        if limit < 0:
            raise ValueError(f"limit must be non-negative, got {limit}")
        sql.append("LIMIT ?")
        params.append(limit)

    return " ".join(sql), tuple(params)


def select_from_spec(spec: Mapping[str, Any]) -> tuple[str, tuple[object, ...]]:
    """Call :func:`select` from a configuration mapping via ``*`` / ``**`` unpacking."""
    options = dict(spec)
    columns = options.pop("columns", ())
    if isinstance(columns, str):
        raise TypeError("'columns' must be a sequence of names, not a single string")
    return select(*columns, **options)


# ---------------------------------------------------------------------------
# Part 3: PATCH semantics with a sentinel
# ---------------------------------------------------------------------------


class _Missing:
    """Type of the MISSING sentinel: 'the caller did not pass this argument'."""

    _instance: _Missing | None = None

    def __new__(cls) -> _Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"

    def __copy__(self) -> _Missing:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> _Missing:
        return self


MISSING: Final = _Missing()

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_MAX_NICKNAME = 32


def update_user(
    user: Mapping[str, Any],
    *,
    email: str | None | _Missing = MISSING,
    nickname: str | None | _Missing = MISSING,
) -> dict[str, Any]:
    """PATCH-style update: MISSING keeps a field, None clears it, a value sets it."""
    updated = dict(user)
    if email is not MISSING:
        updated["email"] = _clean_email(email)
    if nickname is not MISSING:
        updated["nickname"] = _clean_nickname(nickname)
    return updated


def _clean_email(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"email must be str or None, got {type(value).__name__}")
    cleaned = value.strip().lower()
    if _EMAIL.fullmatch(cleaned) is None:
        raise ValueError(f"invalid email: {value!r}")
    return cleaned


def _clean_nickname(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"nickname must be str or None, got {type(value).__name__}")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("nickname must not be blank")
    if len(cleaned) > _MAX_NICKNAME:
        raise ValueError(f"nickname longer than {_MAX_NICKNAME} characters")
    return cleaned


# ---------------------------------------------------------------------------
# Part 4: dependency injection by parameter name
# ---------------------------------------------------------------------------


class MissingDependencyError(LookupError):
    """A parameter has no explicit argument, no provider, and no default."""


def call_with_dependencies[T](
    func: Callable[..., T],
    providers: Mapping[str, Callable[[], object]],
    /,
    *args: object,
    **kwargs: object,
) -> T:
    """Call ``func``, filling unbound parameters from ``providers`` by name.

    Explicit arguments win; providers are called lazily, only for parameters
    that still need a value; defaults are the last resort.
    """
    signature = inspect.signature(func)
    bound = signature.bind_partial(*args, **kwargs)
    for name, param in signature.parameters.items():
        if name in bound.arguments or param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        if name in providers:
            bound.arguments[name] = providers[name]()
        elif param.default is not inspect.Parameter.empty:
            bound.arguments[name] = param.default
        else:
            raise MissingDependencyError(f"no value for parameter {name!r}")
    return func(*bound.args, **bound.kwargs)


# ---------------------------------------------------------------------------
# Part 5: event bus that forwards arguments
# ---------------------------------------------------------------------------


@dataclass(eq=False, slots=True)
class _Subscription:
    listener: Callable[..., object]
    once: bool


class EventBus:
    """Synchronous publish/subscribe with argument forwarding."""

    def __init__(self) -> None:
        self._subscriptions: dict[str, list[_Subscription]] = {}

    def subscribe(self, event: str, listener: Callable[..., object], *, once: bool = False) -> None:
        if not callable(listener):
            raise TypeError(f"listener must be callable, got {type(listener).__name__}")
        self._subscriptions.setdefault(event, []).append(_Subscription(listener, once))

    def unsubscribe(self, event: str, listener: Callable[..., object]) -> bool:
        subscriptions = self._subscriptions.get(event, [])
        for index, subscription in enumerate(subscriptions):
            if subscription.listener == listener:
                del subscriptions[index]
                return True
        return False

    def publish(self, event: str, /, *args: object, **kwargs: object) -> list[object]:
        """Call every listener for ``event`` with the given arguments; return their results."""
        results: list[object] = []
        # Snapshot: listeners may (un)subscribe while we iterate.
        for subscription in list(self._subscriptions.get(event, ())):
            if subscription.once:
                self._discard(event, subscription)
            results.append(subscription.listener(*args, **kwargs))
        return results

    def _discard(self, event: str, subscription: _Subscription) -> None:
        subscriptions = self._subscriptions.get(event, [])
        for index, candidate in enumerate(subscriptions):
            if candidate is subscription:
                del subscriptions[index]
                return


# ---------------------------------------------------------------------------
# Part 6: argument-aware memoization
# ---------------------------------------------------------------------------


def make_cache_key(
    func: Callable[..., object],
    args: tuple[object, ...],
    kwargs: Mapping[str, object],
) -> tuple[tuple[str, object], ...]:
    """Key a call by its bound arguments, so equivalent spellings collide on purpose."""
    signature = inspect.signature(func)
    bound = signature.bind(*args, **kwargs)
    bound.apply_defaults()
    key: list[tuple[str, object]] = []
    for name, value in bound.arguments.items():
        if signature.parameters[name].kind is inspect.Parameter.VAR_KEYWORD:
            value = tuple(sorted(value.items(), key=operator.itemgetter(0)))
        key.append((name, value))
    return tuple(key)


class Memoized:
    """Callable wrapper caching results by :func:`make_cache_key`."""

    def __init__(self, func: Callable[..., object]) -> None:
        self._func = func
        self.cache: dict[tuple[tuple[str, object], ...], object] = {}
        self.hits = 0
        self.misses = 0
        update_wrapper(self, func)

    def __call__(self, *args: object, **kwargs: object) -> object:
        key = make_cache_key(self._func, args, kwargs)
        if key in self.cache:  # raises TypeError for unhashable arguments
            self.hits += 1
            return self.cache[key]
        self.misses += 1
        result = self._func(*args, **kwargs)
        self.cache[key] = result
        return result


def memoize(func: Callable[..., object]) -> Memoized:
    """Cache ``func`` on its bound arguments; exceptions are not cached."""
    return Memoized(func)
