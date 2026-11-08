# 02 · Function arguments

Six small services, each leaning on a different corner of Python's calling convention. Every part is a real-world shape: an HTTP client helper, a query builder, a PATCH endpoint, a dependency-injecting runner, an event bus, and a memoizer. Implement everything in `main.py`.

Conventions: names, signatures, and error types below are the contract. Tests call your functions with exactly these shapes. Where this README says `TypeError`, Python's own argument binding usually raises it for you; where it says `ValueError`, you raise it.

---

## Part 1 · HTTP request builder

```python
build_request(method, url, /, *, headers=None, params=None, timeout=10.0, **extra) -> Request
```

- `method` and `url` are **positional-only**. Passing them by keyword is a `TypeError`. This is why `method=` can be used as ordinary extra data.
- `headers`, `params`, `timeout` are **keyword-only**. A positional dict is a `TypeError`.
- Unknown keyword arguments go into `extra` (a fresh `dict`).
- `Request` is a frozen dataclass you define in `main.py` with attributes `method`, `url`, `headers`, `params`, `timeout`, `extra`.

Rules:

- `method`: must be `str`; it is uppercased. Allowed: `GET POST PUT PATCH DELETE HEAD OPTIONS`. Otherwise `ValueError`. Non-`str` is `TypeError`.
- `url`: must be `str` starting with `http://` or `https://` (case-insensitive). Otherwise `ValueError`; non-`str` is `TypeError`.
- `headers`: `None` or a mapping of `str -> str`. Names are lowercased. Two names that collide after lowercasing are a `ValueError`. Non-`str` keys or values are `TypeError`. The result is a new `dict`; it never aliases the caller's mapping.
- `params`: `None` or a mapping of `str -> value`. Values render as: `bool` to `"true"`/`"false"`, `str`, `int`, and `float` through `str()`. Anything else (lists, `None`, ...) is `TypeError`. Non-`str` keys are `TypeError`. The result is a new `dict`.
- `timeout`: `int` or `float`, not `bool`. Non-numeric is `TypeError`. Must be finite and `> 0`; otherwise `ValueError`. Stored as `float`.
- Defaults are fresh per call: `headers` and `params` on the returned `Request` are independent `dict`s on every call.

Concepts exercised: positional-only `/`, keyword-only `*`, `**extra` capturing shadowed names, mutable-default safety, defensive copies.

---

## Part 2 · SQL-ish query builder

```python
select(*columns, table, where=None, order_by=(), limit=None) -> tuple[str, tuple[object, ...]]
select_from_spec(spec: Mapping[str, Any]) -> tuple[str, tuple[object, ...]]
```

- `table` is **keyword-only and required**. `select("id", "users")` is a `TypeError`.
- Returns `(sql, params)`. Values are always passed through placeholders, never interpolated.
- Identifiers (columns, table, where keys, order terms) must fullmatch `[A-Za-z_][A-Za-z0-9_]*` with an optional single dotted part, e.g. `u.id`. Non-`str` is `TypeError`; invalid text is `ValueError`.
- No columns renders `*`. The projection is `SELECT <cols> FROM <table>`.
- `where`: `None` or a mapping. Each entry is `col = ?` with the value as a parameter; a `None` value renders `col IS NULL` with no parameter. Entries are joined with `AND`. An empty mapping adds no clause. Non-mapping is `TypeError`.
- `order_by`: a single `str` (one term, not characters) or an iterable of `str`. A leading `-` means `DESC`. Invalid term text is `ValueError`; non-`str` terms are `TypeError`.
- `limit`: `None` or a non-negative `int` (not `bool`). Renders `LIMIT ?` with the value as a parameter. Negative is `ValueError`; wrong type is `TypeError`.
- Clause order: `WHERE`, `ORDER BY`, `LIMIT`. Parameters appear in the same order as their placeholders.
- `select_from_spec(spec)` unpacks a configuration dict into the call: `columns` (a sequence) becomes `*columns`, and every other key becomes a keyword argument. A `columns` value that is a bare `str` is `TypeError` (it would otherwise split into characters). Unknown keys surface as the `TypeError` Python raises for the keyword.

Concepts exercised: `*args` before keyword-only parameters, required keyword-only args, `**` unpacking at call sites, the bare-string-is-iterable pitfall.

Example:

```python
select("id", "email", table="users", where={"active": True, "deleted_at": None}, order_by="-id", limit=20)
# ("SELECT id, email FROM users WHERE active = ? AND deleted_at IS NULL ORDER BY id DESC LIMIT ?", (True, 20))
```

---

## Part 3 · PATCH semantics with a sentinel

```python
MISSING  # module-level singleton sentinel: "argument was not provided"
update_user(user, *, email=MISSING, nickname=MISSING) -> dict[str, Any]
```

- `MISSING` is a singleton instance of a class you define. `repr(MISSING) == "MISSING"`. It is not `None`. `copy.copy` and `copy.deepcopy` return the same object.
- Omitted fields (still `MISSING`) are left untouched. Omitted keys are not added to the result.
- `None` means **clear**: the key is set to `None`.
- `email`: `str` is stripped and lowercased, then must fullmatch `[^@\s]+@[^@\s]+\.[^@\s]+`; otherwise `ValueError`. Non-`str`/non-`None` is `TypeError`.
- `nickname`: `str` is stripped; empty after stripping is `ValueError`; longer than 32 characters is `ValueError`. Non-`str`/non-`None` is `TypeError`.
- `user` is a mapping. The function returns a **new** `dict` and never mutates the input.
- `email` and `nickname` are keyword-only: a positional second argument is `TypeError`.

Concepts exercised: sentinel vs `None` for "not provided" vs "clear", keyword-only PATCH arguments, identity-preserving copy protocol for singletons.

---

## Part 4 · Dependency injection by parameter name

```python
class MissingDependencyError(LookupError): ...

call_with_dependencies(func, providers, /, *args, **kwargs) -> Any
```

Behaviour, like a tiny pytest-fixtures resolver:

- `func` and `providers` are positional-only, so a target parameter may be named `func` or `providers`.
- `providers` maps a parameter name to a zero-argument callable. Providers are called **lazily**: only for parameters that need one, and at most once per call.
- Resolution order per parameter: explicit argument (from `*args`/`**kwargs`) → provider → default. If none applies, raise `MissingDependencyError` whose message contains the parameter name.
- Explicit arguments win; their providers are never called.
- `*args` and `**kwargs` parameters of the target are never injected.
- Positional-only target parameters are supplied positionally; a keyword attempt on them is a `TypeError`.
- `func` may be a `functools.partial`. Keywords bound by `partial` are respected through `inspect.signature`.
- Unknown explicit keyword arguments are a `TypeError` (Python's binding does this).
- `MissingDependencyError` must subclass `LookupError`.

Use `inspect.signature`, `Signature.bind_partial`, and `BoundArguments` (`.arguments`, `.args`, `.kwargs`). Do not use `apply_defaults` blindly. Injecting from providers must still build a call that Python accepts.

Concepts exercised: `inspect.signature` introspection, `bind_partial`, `BoundArguments`, parameter kinds, `functools.partial` signatures, lazy callables.

---

## Part 5 · Event bus that forwards arguments

```python
class EventBus:
    def subscribe(self, event: str, listener: Callable[..., object], *, once: bool = False) -> None
    def unsubscribe(self, event: str, listener: Callable[..., object]) -> bool
    def publish(self, event: str, /, *args: object, **kwargs: object) -> list[object]
```

- `event` is **positional-only** on `publish`, so a payload may use `event=` as a keyword and it is forwarded to listeners unchanged.
- `publish` calls each listener in subscription order with `*args, **kwargs` and returns their results as a list. No listeners → `[]`.
- `subscribe`: `listener` must be callable (`TypeError` otherwise). `once` is keyword-only. A `once` listener is removed **before** it is called, so it fires at most once even if it re-enters `publish` or raises.
- `unsubscribe` removes the first subscription for that listener (`==` comparison, so bound methods work) and returns `True`, or `False` if none matched.
- `publish` iterates over a **snapshot**: unsubscribing inside a listener does not skip listeners already scheduled in the current publish.
- A listener exception propagates out of `publish` immediately; later listeners are not called.

Concepts exercised: `*args/**kwargs` forwarding, positional-only names to avoid collisions, keyword-only flags, snapshot iteration, `functools.partial` listeners.

---

## Part 6 · Argument-aware memoization

```python
make_cache_key(func, args, kwargs) -> tuple
memoize(func) -> Memoized
```

- `make_cache_key` binds the call against `inspect.signature(func)`, applies defaults, and returns a hashable key. Different spellings of the same call yield equal keys: `f(2, 3)`, `f(2, height=3)`, `f(2, 3, scale=1)` when `scale` defaults to `1`.
- `**kwargs` values are keyed by sorted name, so keyword order does not matter. `*args` contributes its tuple.
- A call that does not bind raises `TypeError` from `Signature.bind`, before the function runs.
- `memoize(func)` returns a callable object (`Memoized`) with:
  - `hits: int` and `misses: int` counters;
  - `cache: dict` mapping keys to results;
  - `__name__`, `__doc__`, `__wrapped__` copied from `func` (use `functools.update_wrapper`).
- A call whose key is unhashable (e.g. a `list` argument) raises `TypeError` and does not count as a miss or run the function.
- Exceptions are not cached: a failing call re-runs the function the next time.

Concepts exercised: `Signature.bind`, `apply_defaults`, `BoundArguments`, `update_wrapper`, hashing rules for cache keys.

---

## Concepts exercised

- Positional-only (`/`), keyword-only (`*`), `*args`, `**kwargs`
- Required keyword-only parameters and keyword-only flags
- Defaults, mutable-default safety, defensive copies
- Sentinels vs `None` for "not provided" vs "clear"
- Argument forwarding and call-site unpacking (`*`, `**`)
- `inspect.signature`, `Signature.bind`, `bind_partial`, `BoundArguments`, `apply_defaults`
- `functools.partial` and `functools.update_wrapper`

## Run

```
uv run pytest exercises/02_function_arguments
uv run pytest exercises/02_function_arguments -k part2
uv run pytest exercises/02_function_arguments --solutions
```

Every test should fail until `main.py` is implemented. The `--solutions` run uses the reference implementation.

## Stretch (untested)

- Add `Request.with_headers(**more)` that merges without mutating, and reason about why it cannot use `**more` for header names containing `-`.
- Make `make_cache_key` treat `1` and `True` as distinct keys. Decide how to do it without breaking hashing of equal values.
- Give `call_with_dependencies` a `strict=True` mode that rejects providers for parameters the target does not declare.
