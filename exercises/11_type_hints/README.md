# 11 · Type hints as an engineering tool

Your service's shared building blocks, typed so that misuse is caught by mypy
before it reaches production. Implement everything in `main.py`. Tests ship
failing; a Part is done when its tests pass **and** the static checks in Part 7
pass.

Target: Python 3.13+ syntax (PEP 695 generics and `type` statements). Every
public name below must exist in `main.py`; signatures are the contract.

Runtime behaviour is checked by pytest. Typing precision is checked by running
`mypy --strict` over your `main.py` against `usage_ok.py` (must be clean) and
`usage_bad.py` (must fail at exactly the marked lines with the marked codes).
Run mypy yourself while you work:

```
uv run --no-sync mypy --strict exercises/11_type_hints/main.py
```

## Part 1: generic repository and structural protocols

- `class HasId(Protocol)`: read-only property `id -> int`. Structural; entities do not inherit it.
- `class NotFoundError(LookupError)`, `class DuplicateIdError(ValueError)`.
- `class Repo[T: HasId]` (PEP 695, bounded type variable). Constructor takes no arguments.
  - `add(item: T) -> T`: `DuplicateIdError` if the id is already stored.
  - `get(entity_id: int) -> T | None`.
  - `require(entity_id: int) -> T`: `NotFoundError` (message includes the id).
  - `update(item: T) -> T`: replaces by id, keeps insertion position; `NotFoundError` if absent.
  - `remove(entity_id: int) -> T`: returns the removed item; `NotFoundError` if absent.
  - `all() -> list[T]`: insertion order; a new list each call.
  - `where(predicate: Callable[[T], bool]) -> list[T]`.
  - `__len__`.
- `def first[T](items: Iterable[T]) -> T`: consumes at most one element; `LookupError` if empty.
- `def first_or[T, D](items: Iterable[T], default: D) -> T | D`.
- `def normalize_key[K: (str, bytes)](key: K) -> K` (constrained type variable): strips surrounding whitespace and lowercases, keeping the input type. Any other type raises `TypeError`.
- `@runtime_checkable class Sink[T](Protocol)`: method `write(item: T) -> None`. Contravariant in `T` (PEP 695 infers it).
- `class MemorySink[T]`: structurally satisfies `Sink[T]` without inheriting it. `write(item)`, and `items -> tuple[T, ...]` (snapshot).
- `def fan_out[T](item: T, sinks: Iterable[Sink[T]]) -> int`: writes to each sink in order; returns the count.

Concepts exercised: PEP 695 class and function generics, bounded and constrained type variables, Protocol vs nominal subtyping, `runtime_checkable` limits (method presence only), variance inference.

## Part 2: typed event bus

- `UserId = NewType("UserId", int)`.
- `user_topic(user_id: UserId) -> str`: `"user.<id>.events"`.
- `class UserCreated(TypedDict)`: `kind: Literal["user.created"]`, `user_id: UserId`, `email: str`.
- `class OrderPlaced(TypedDict)`: `kind: Literal["order.placed"]`, `order_id: int`, `total_cents: int`, `coupon: NotRequired[str]`.
- `type Event = UserCreated | OrderPlaced`.
- `type Unsubscribe = Callable[[], None]`.
- `class EventBus`:
  - `subscribe(kind, handler) -> Unsubscribe`, typed with `@overload`: `"user.created"` takes `Callable[[UserCreated], None]`, `"order.placed"` takes `Callable[[OrderPlaced], None]`. A mismatched pair must be a static error.
  - `publish(event: Event) -> int`: invokes handlers registered for `event["kind"]` in subscription order; returns the number actually invoked. The handler set is snapshotted at the start of a publish: subscriptions added during dispatch miss the current event; subscriptions cancelled during dispatch are skipped.
  - Each `Unsubscribe` removes only its own registration (the same callable subscribed twice is two registrations) and is idempotent.
- `def describe_event(event: Event) -> str`: exhaustive over `kind`, with `assert_never` on the fall-through.
  - User: `user 9 registered <ada@example.com>`.
  - Order: `order 7 placed: 1250 cents`, with ` (coupon SPRING)` appended when a coupon is present.

Concepts exercised: TypedDict with Literal discriminants, tagged-union narrowing, `NewType`, `@overload` with a single implementation, `assert_never`, `Callable` variance, snapshot iteration.

## Part 3: typed settings

- `DEFAULT_HTTP_TIMEOUT: Final[float] = 30.0`.
- `@dataclass(frozen=True, slots=True) class Range(lo: int, hi: int)`: inclusive bounds, used as `Annotated` metadata.
- `PORT_RANGE = Range(1, 65535)`, `POOL_RANGE = Range(1, 1000)`.
- `Port = Annotated[int, PORT_RANGE]` (a plain assignment, not a `type` statement).
- `class DatabaseSettings(TypedDict)`: `host: str`, `port: Port`, `name: str`, `pool_size: NotRequired[int]`.
- `class Settings(TypedDict, total=False)`: `service_name: Required[str]`, `database: Required[DatabaseSettings]`, `http_timeout: float`, `debug: bool`.
- `class SettingsError(ValueError)`.
- `def field_range(hint: object) -> Range | None`: the `Range` in an `Annotated` hint's metadata, or `None`.
- `def load_settings(env: Mapping[str, str]) -> Settings`. Environment variables:
  - Required: `SERVICE_NAME`, `DB_HOST`, `DB_PORT` (int in `PORT_RANGE`), `DB_NAME`.
  - Optional: `DB_POOL_SIZE` (int in `POOL_RANGE`; omitted from the result when unset), `HTTP_TIMEOUT` (finite float > 0; default `DEFAULT_HTTP_TIMEOUT`), `DEBUG` (boolean; default `False`).
  - Values are stripped. A blank value counts as unset, so a blank required value raises.
  - Booleans: `1 true yes on` / `0 false no off`, case-insensitive. Anything else is an error.
  - Errors raise `SettingsError` with a message that starts with the variable name (for example `DB_PORT: ...`).

Concepts exercised: `Required`/`NotRequired` in `total=False` TypedDicts, `Annotated` metadata that survives `get_type_hints(..., include_extras=True)`, `Final`, bounded parsing that reports the offending key.

## Part 4: signature-preserving decorators

- `def retry[**P, R](*, attempts: int, on: tuple[type[BaseException], ...] = (Exception,)) -> Callable[[Callable[P, R]], Callable[P, R]]`.
  - `attempts` counts total calls (`attempts=1` means no retry). `attempts < 1` raises `ValueError` when the decorator is built.
  - Retries only exceptions in `on`. After the final attempt, re-raises the last exception. No sleeping.
  - Wrapper preserves metadata (`functools.wraps`).
- `TenantId = NewType("TenantId", str)`.
- `@contextmanager def tenant_scope(tenant: TenantId) -> Iterator[TenantId]`: binds the tenant in a `ContextVar` for the `with` block and restores the previous value on exit, including on exceptions. Blank tenant raises `ValueError`.
- `def current_tenant() -> TenantId`: `LookupError` outside any scope.
- `def with_tenant[**P, R](func: Callable[Concatenate[TenantId, P], R]) -> Callable[P, R]`: passes `current_tenant()` as the first argument. Callers omit it. Preserves metadata. Outside a scope, the call raises `LookupError`.

Concepts exercised: `ParamSpec` with `*args: P.args, **kwargs: P.kwargs`, `Concatenate` to remove a leading parameter, `ContextVar` with token reset, and why a decorator that drops its own arguments needs `R` in the outer signature.

## Part 5: immutable query builder with `Self`

- `class QueryBuilder` (frozen dataclass): `QueryBuilder(table: str)`; `table` is validated as an identifier at construction.
  - `where(column: str, value: object) -> Self`: equality filter. `value is None` compiles to `IS NULL` and adds no parameter.
  - `order_by(column: str, *, descending: bool = False) -> Self`.
  - `limit(n: int) -> Self`: `ValueError` if `n < 0`. `limit(0)` is kept.
  - `build() -> tuple[str, tuple[object, ...]]`: SQL with `?` placeholders. Clause order is `WHERE`, `ORDER BY`, `LIMIT`. Parameters are in placeholder order, and the `LIMIT` value is last.
- Identifiers (table, columns) must match `[A-Za-z_][A-Za-z0-9_]*`; otherwise `ValueError`. Values are never interpolated into SQL.
- Every method returns a new instance of the receiver's class. The original is unchanged. A subclass (`class UserQuery(QueryBuilder)`) keeps its type through chained calls.

Concepts exercised: `Self` in a non-final class hierarchy, frozen dataclasses with `replace`, why `Self` beats `-> QueryBuilder`, identifier validation as SQL injection defence, `None` vs `= NULL`.

## Part 6: result unions, `TypeIs`/`TypeGuard`, exhaustive `match`

- `@dataclass(frozen=True, slots=True) class Ok[T](value: T)`, `class Err[E](error: E)`.
- `type Result[T, E] = Ok[T] | Err[E]`.
- `type PaymentError = Literal["declined", "insufficient_funds", "gateway_timeout"]`.
- `def is_ok[T, E](result: Result[T, E]) -> TypeIs[Ok[T]]`.
- `def all_str(values: Sequence[object]) -> TypeGuard[Sequence[str]]`: True when every element is a `str` (vacuously True when empty).
- `def collect[T, E](results: Iterable[Result[T, E]]) -> Result[list[T], E]`: `Ok` of all values. On the first `Err`, stop consuming and return that `Err` object unchanged.
- `def http_status(error: PaymentError) -> int`: `declined` → 402, `insufficient_funds` → 422, `gateway_timeout` → 504. Exhaustive `match` with `assert_never`; an unknown value raises `AssertionError` at runtime.
- `def render_result(result: Result[int, PaymentError]) -> str`: `Ok(5)` → `charged 5`; `Err("declined")` → `failed: declined (HTTP 402)`. Exhaustive over `Ok`/`Err` with `assert_never`; a non-`Result` argument raises `AssertionError`.

Concepts exercised: `TypeIs` (both branches narrow) vs `TypeGuard` (positive branch only), class patterns in `match`, `assert_never` for exhaustiveness, generic `Result` unions, short-circuit consumption.

## Part 7: static contracts

No runtime API beyond Parts 1–6. Part 7 is judged by mypy, which the test
suite runs for you:

- `main.py` defines every public name and passes `mypy --strict`.
- `usage_ok.py` (provided) type-checks with zero errors. It exercises every public type in its intended way, including `Self` on a subclass, `TypeIs` narrowing, and an exhaustive `match`.
- `usage_bad.py` (provided) contains one deliberate misuse per marked line (`# expect: <code>`). Your types must reject each one with that code on that line, and nothing else. A bare `Any` would fail this.

Concepts exercised: writing types precise enough that the checker rejects misuse, not merely accepts correct use.

## Run

```
uv run pytest exercises/11_type_hints
uv run pytest exercises/11_type_hints -k part1
uv run pytest exercises/11_type_hints -k part7
uv run pytest exercises/11_type_hints --solutions
```

## Stretch

- Make `EventBus` generic over an event union so `subscribe` needs no overloads. Compare the error messages mypy gives for a mismatched handler under each design.
- Replace `Settings` with a `dataclass` plus a `load_settings` that uses `typing.get_type_hints` to coerce each field automatically. Where does that approach stop being type-safe?
- Add a `TypeIs`-based `is_str_keyed(mapping)` guard for `Mapping[object, object]` and test its negative branch.
