"""Typed service building blocks: reference implementation."""

import functools
import math
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import (
    Annotated,
    Concatenate,
    Final,
    Literal,
    NewType,
    NotRequired,
    Protocol,
    Required,
    Self,
    TypedDict,
    TypeGuard,
    TypeIs,
    assert_never,
    get_args,
    get_origin,
    overload,
    runtime_checkable,
)

# --------------------------------------------------------------------------- #
# Part 1: generic repository and structural protocols
# --------------------------------------------------------------------------- #


class HasId(Protocol):
    """Structural contract for repository entities: anything exposing an int ``id``."""

    @property
    def id(self) -> int: ...


class NotFoundError(LookupError):
    """No stored entity has the requested id."""


class DuplicateIdError(ValueError):
    """An entity with the same id is already stored."""


class Repo[T: HasId]:
    """In-memory repository keyed by ``id``. Iteration follows insertion order."""

    def __init__(self) -> None:
        self._items: dict[int, T] = {}

    def __len__(self) -> int:
        return len(self._items)

    def add(self, item: T) -> T:
        if item.id in self._items:
            raise DuplicateIdError(f"entity {item.id} already exists")
        self._items[item.id] = item
        return item

    def get(self, entity_id: int) -> T | None:
        return self._items.get(entity_id)

    def require(self, entity_id: int) -> T:
        try:
            return self._items[entity_id]
        except KeyError:
            raise NotFoundError(f"no entity with id {entity_id}") from None

    def update(self, item: T) -> T:
        if item.id not in self._items:
            raise NotFoundError(f"no entity with id {item.id}")
        self._items[item.id] = item  # reassigning an existing key keeps its position
        return item

    def remove(self, entity_id: int) -> T:
        try:
            return self._items.pop(entity_id)
        except KeyError:
            raise NotFoundError(f"no entity with id {entity_id}") from None

    def all(self) -> list[T]:
        return list(self._items.values())

    def where(self, predicate: Callable[[T], bool]) -> list[T]:
        return [item for item in self._items.values() if predicate(item)]


def first[T](items: Iterable[T]) -> T:
    """Return the first item, consuming at most one element."""
    for item in items:
        return item
    raise LookupError("first() of an empty iterable")


def first_or[T, D](items: Iterable[T], default: D) -> T | D:
    """Return the first item, or ``default`` when the iterable is empty."""
    for item in items:
        return item
    return default


def normalize_key[K: (str, bytes)](key: K) -> K:
    """Strip surrounding whitespace and lowercase, preserving str/bytes."""
    if not isinstance(key, (str, bytes)):
        raise TypeError(f"key must be str or bytes, not {type(key).__name__}")
    return key.strip().lower()


@runtime_checkable
class Sink[T](Protocol):
    """Anything with ``write(item)``; nominal inheritance is not required."""

    def write(self, item: T) -> None: ...


class MemorySink[T]:
    """Sink that keeps everything it receives, in order."""

    def __init__(self) -> None:
        self._items: list[T] = []

    def write(self, item: T) -> None:
        self._items.append(item)

    @property
    def items(self) -> tuple[T, ...]:
        return tuple(self._items)


def fan_out[T](item: T, sinks: Iterable[Sink[T]]) -> int:
    """Write ``item`` to every sink; return how many sinks were written to."""
    count = 0
    for sink in sinks:
        sink.write(item)
        count += 1
    return count


# --------------------------------------------------------------------------- #
# Part 2: typed event bus
# --------------------------------------------------------------------------- #

UserId = NewType("UserId", int)


class UserCreated(TypedDict):
    kind: Literal["user.created"]
    user_id: UserId
    email: str


class OrderPlaced(TypedDict):
    kind: Literal["order.placed"]
    order_id: int
    total_cents: int
    coupon: NotRequired[str]


type Event = UserCreated | OrderPlaced
type Unsubscribe = Callable[[], None]


def user_topic(user_id: UserId) -> str:
    return f"user.{user_id}.events"


@dataclass(eq=False)
class _Subscription:
    handler: Callable[..., None]
    active: bool = True


class EventBus:
    """Routes events to handlers registered per ``kind``."""

    def __init__(self) -> None:
        self._subs: dict[str, list[_Subscription]] = {}

    @overload
    def subscribe(
        self, kind: Literal["user.created"], handler: Callable[[UserCreated], None]
    ) -> Unsubscribe: ...
    @overload
    def subscribe(
        self, kind: Literal["order.placed"], handler: Callable[[OrderPlaced], None]
    ) -> Unsubscribe: ...
    def subscribe(self, kind: str, handler: Callable[..., None]) -> Unsubscribe:
        subs = self._subs.setdefault(kind, [])
        sub = _Subscription(handler)
        subs.append(sub)

        def unsubscribe() -> None:
            if sub.active:
                sub.active = False
                subs.remove(sub)

        return unsubscribe

    def publish(self, event: Event) -> int:
        """Invoke handlers for ``event['kind']`` in subscription order.

        The set of handlers is snapshotted at the start, so subscriptions added
        during dispatch do not see the current event; subscriptions cancelled
        during dispatch are skipped.
        """
        invoked = 0
        for sub in list(self._subs.get(event["kind"], [])):
            if sub.active:
                sub.handler(event)
                invoked += 1
        return invoked


def describe_event(event: Event) -> str:
    if event["kind"] == "user.created":
        return f"user {event['user_id']} registered <{event['email']}>"
    if event["kind"] == "order.placed":
        coupon = event.get("coupon")
        suffix = "" if coupon is None else f" (coupon {coupon})"
        return f"order {event['order_id']} placed: {event['total_cents']} cents{suffix}"
    assert_never(event)


# --------------------------------------------------------------------------- #
# Part 3: typed settings
# --------------------------------------------------------------------------- #

DEFAULT_HTTP_TIMEOUT: Final[float] = 30.0


@dataclass(frozen=True, slots=True)
class Range:
    """Inclusive integer bounds; used as ``Annotated`` metadata."""

    lo: int
    hi: int


PORT_RANGE = Range(1, 65535)
POOL_RANGE = Range(1, 1000)
Port = Annotated[int, PORT_RANGE]


class DatabaseSettings(TypedDict):
    host: str
    port: Port
    name: str
    pool_size: NotRequired[int]


class Settings(TypedDict, total=False):
    service_name: Required[str]
    database: Required[DatabaseSettings]
    http_timeout: float
    debug: bool


class SettingsError(ValueError):
    """A setting is missing or failed validation."""


def field_range(hint: object) -> Range | None:
    """Return the ``Range`` attached to an ``Annotated`` hint, if any."""
    if get_origin(hint) is not Annotated:
        return None
    for metadata in get_args(hint)[1:]:
        if isinstance(metadata, Range):
            return metadata
    return None


_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})
_FALSY: Final = frozenset({"0", "false", "no", "off"})


def _raw(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key)
    if value is None:
        return None
    return value.strip() or None  # blank counts as unset


def _required(env: Mapping[str, str], key: str) -> str:
    value = _raw(env, key)
    if value is None:
        raise SettingsError(f"{key} is required")
    return value


def _parse_int(key: str, raw: str, bounds: Range) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{key}: expected an integer, got {raw!r}") from None
    if not bounds.lo <= value <= bounds.hi:
        raise SettingsError(f"{key}: must be in {bounds.lo}..{bounds.hi}, got {value}")
    return value


def _parse_timeout(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(f"HTTP_TIMEOUT: expected a number, got {raw!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise SettingsError(f"HTTP_TIMEOUT: must be a finite positive number, got {raw!r}")
    return value


def _parse_bool(key: str, raw: str) -> bool:
    lowered = raw.lower()
    if lowered in _TRUTHY:
        return True
    if lowered in _FALSY:
        return False
    raise SettingsError(f"{key}: expected a boolean, got {raw!r}")


def load_settings(env: Mapping[str, str]) -> Settings:
    """Build typed settings from an environment-style mapping."""
    database: DatabaseSettings = {
        "host": _required(env, "DB_HOST"),
        "port": _parse_int("DB_PORT", _required(env, "DB_PORT"), PORT_RANGE),
        "name": _required(env, "DB_NAME"),
    }
    pool_raw = _raw(env, "DB_POOL_SIZE")
    if pool_raw is not None:
        database["pool_size"] = _parse_int("DB_POOL_SIZE", pool_raw, POOL_RANGE)

    timeout_raw = _raw(env, "HTTP_TIMEOUT")
    debug_raw = _raw(env, "DEBUG")
    return {
        "service_name": _required(env, "SERVICE_NAME"),
        "database": database,
        "http_timeout": DEFAULT_HTTP_TIMEOUT if timeout_raw is None else _parse_timeout(timeout_raw),
        "debug": False if debug_raw is None else _parse_bool("DEBUG", debug_raw),
    }


# --------------------------------------------------------------------------- #
# Part 4: signature-preserving decorators
# --------------------------------------------------------------------------- #


def retry[**P, R](
    *, attempts: int, on: tuple[type[BaseException], ...] = (Exception,)
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Call the wrapped function up to ``attempts`` times, retrying on ``on``."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            attempt = 1
            while True:
                try:
                    return func(*args, **kwargs)
                except on:
                    if attempt >= attempts:
                        raise
                    attempt += 1

        return wrapper

    return decorator


TenantId = NewType("TenantId", str)
_current_tenant: ContextVar[TenantId] = ContextVar("current_tenant")


@contextmanager
def tenant_scope(tenant: TenantId) -> Iterator[TenantId]:
    """Bind ``tenant`` for the dynamic extent of the ``with`` block."""
    if not tenant.strip():
        raise ValueError("tenant must be non-blank")
    token = _current_tenant.set(tenant)
    try:
        yield tenant
    finally:
        _current_tenant.reset(token)


def current_tenant() -> TenantId:
    try:
        return _current_tenant.get()
    except LookupError:
        raise LookupError("no tenant in scope; wrap the call in tenant_scope(...)") from None


def with_tenant[**P, R](func: Callable[Concatenate[TenantId, P], R]) -> Callable[P, R]:
    """Inject the ambient tenant as the first argument; callers omit it."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        return func(current_tenant(), *args, **kwargs)

    return wrapper


# --------------------------------------------------------------------------- #
# Part 5: immutable query builder with Self
# --------------------------------------------------------------------------- #

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _identifier(name: str, what: str) -> str:
    if not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"invalid {what} name: {name!r}")
    return name


@dataclass(frozen=True)
class QueryBuilder:
    """Immutable SELECT builder; every method returns a new instance of the same class."""

    table: str
    filters: tuple[tuple[str, object], ...] = ()
    orders: tuple[tuple[str, bool], ...] = field(default=())  # (column, descending)
    row_limit: int | None = None

    def __post_init__(self) -> None:
        _identifier(self.table, "table")

    def where(self, column: str, value: object) -> Self:
        """Equality filter; ``None`` becomes ``IS NULL`` (``= NULL`` never matches)."""
        return replace(self, filters=(*self.filters, (_identifier(column, "column"), value)))

    def order_by(self, column: str, *, descending: bool = False) -> Self:
        return replace(self, orders=(*self.orders, (_identifier(column, "column"), descending)))

    def limit(self, n: int) -> Self:
        if n < 0:
            raise ValueError("limit must be >= 0")
        return replace(self, row_limit=n)

    def build(self) -> tuple[str, tuple[object, ...]]:
        clauses: list[str] = []
        params: list[object] = []
        for column, value in self.filters:
            if value is None:
                clauses.append(f"{column} IS NULL")
            else:
                clauses.append(f"{column} = ?")
                params.append(value)

        sql = f"SELECT * FROM {self.table}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if self.orders:
            sql += " ORDER BY " + ", ".join(
                f"{column} {'DESC' if descending else 'ASC'}" for column, descending in self.orders
            )
        if self.row_limit is not None:
            sql += " LIMIT ?"
            params.append(self.row_limit)
        return sql, tuple(params)


# --------------------------------------------------------------------------- #
# Part 6: result unions, TypeIs/TypeGuard, exhaustive match
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Ok[T]:
    value: T


@dataclass(frozen=True, slots=True)
class Err[E]:
    error: E


type Result[T, E] = Ok[T] | Err[E]
type PaymentError = Literal["declined", "insufficient_funds", "gateway_timeout"]


def is_ok[T, E](result: Result[T, E]) -> TypeIs[Ok[T]]:
    return isinstance(result, Ok)


def all_str(values: Sequence[object]) -> TypeGuard[Sequence[str]]:
    """``TypeGuard`` (not ``TypeIs``): a False answer does not narrow the negative branch."""
    return all(isinstance(value, str) for value in values)


def collect[T, E](results: Iterable[Result[T, E]]) -> Result[list[T], E]:
    """Gather all values, short-circuiting on the first ``Err``."""
    values: list[T] = []
    for result in results:
        if isinstance(result, Err):
            return result
        values.append(result.value)
    return Ok(values)


def http_status(error: PaymentError) -> int:
    match error:
        case "declined":
            return 402
        case "insufficient_funds":
            return 422
        case "gateway_timeout":
            return 504
        case _:
            assert_never(error)


def render_result(result: Result[int, PaymentError]) -> str:
    match result:
        case Ok(value=value):
            return f"charged {value}"
        case Err(error=error):
            return f"failed: {error} (HTTP {http_status(error)})"
    assert_never(result)
