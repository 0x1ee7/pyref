# 01 · Built-in data structures

You are building the internal `ops_toolkit` helpers for a small API gateway and
its billing/inventory side-jobs. Nothing here is an algorithm puzzle: each part is
a component you would actually ship, and the tests encode the production edge
cases (clock boundaries, mutation of inputs, ties, sharing of nested state).

Everything goes in `main.py`. Standard library only. Public names, signatures
and behaviour below are the full spec; the tests are the acceptance criteria.

Type parameters use PEP 695 syntax (Python 3.13+).

---

## Part 1 — TTL + LRU cache

Gateway response cache: bounded size, least-recently-used eviction, and each
entry expires `ttl` seconds after it was written. Time comes from an injected
`clock` callable (default `time.monotonic`) so tests and callers control it.

```python
class TTLCache[K, V]:
    def __init__(self, maxsize: int, ttl: float, clock: Callable[[], float] = time.monotonic) -> None: ...
    def get(self, key: K, default: V | None = None) -> V | None: ...
    def put(self, key: K, value: V) -> None: ...
    def keys(self) -> list[K]: ...
    def __contains__(self, key: object) -> bool: ...
    def __len__(self) -> int: ...
    @property
    def hits(self) -> int: ...
    @property
    def misses(self) -> int: ...
```

- `maxsize < 1` or `ttl <= 0` → `ValueError`.
- An entry written at time `t` expires at `t + ttl`. It is expired when
  `clock() >= expires_at` (the expiry instant itself is already a miss).
- `get(key)`: a live entry is a hit and becomes the most recently used. A missing
  or expired key is a miss, returns `default`, and an expired entry is removed.
  Reading never extends an entry's TTL.
- `put(key, value)`: inserts or replaces. Replacing refreshes both the TTL and the
  recency. Expired entries are purged first, then least-recently-used live entries
  are evicted until `len <= maxsize`. Expired entries never count toward capacity.
- `key in cache`: `True` iff the key is live. It does **not** change recency or the
  hit/miss counters.
- `len(cache)`: number of live entries (expired ones excluded).
- `keys()`: live keys ordered least-recently-used first, most-recently-used last.
  Does not change recency.
- `hits` / `misses`: count `get` outcomes only.

## Part 2 — Sliding-window rate limiter

Per-client request limiter: at most `limit` requests in any trailing `window`
seconds. Denied requests are not recorded, so a client that keeps hammering the
limiter does not dig itself deeper.

```python
class SlidingWindowLimiter:
    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic) -> None: ...
    def allow(self, client: str) -> bool: ...
    def remaining(self, client: str) -> int: ...
    def retry_after(self, client: str) -> float: ...
    def prune(self) -> int: ...
```

- `limit < 1` or `window <= 0` → `ValueError`.
- An event at time `t` counts toward the window at time `now` iff `now - t < window`.
  An event exactly `window` seconds old has already left the window.
- `allow(client)`: if fewer than `limit` live events, record `now` and return `True`;
  otherwise return `False` and record nothing.
- `remaining(client)`: `max(0, limit - live_events)`.
- `retry_after(client)`: `0.0` if `remaining > 0`; otherwise the number of seconds
  until the oldest live event leaves the window.
- `prune()`: drop state for clients with no live events; return how many were dropped.
  Calling any method must not leak state for clients that were only queried.

## Part 3 — Top-K product ranking

Sales-line stream from the order pipeline: `(sku, units)` pairs. The input may be a
one-shot iterator with far more lines than distinct SKUs.

```python
def top_k_products(sales: Iterable[tuple[str, int]], k: int) -> list[tuple[str, int]]: ...
```

- Units are aggregated per SKU. A negative `units` value → `ValueError`.
- Returns at most `k` `(sku, total_units)` pairs, sorted by units **descending**,
  ties broken by SKU **ascending**.
- `k <= 0` → `[]`. `k` larger than the number of distinct SKUs → all of them.
- Consumes the iterable exactly once.

## Part 4 — Layered configuration

Config precedence, highest first: **CLI > environment > file > defaults**.

```python
def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]: ...
def parse_env(environ: Mapping[str, str], prefix: str) -> dict[str, Any]: ...

class Config:
    def __init__(
        self,
        *,
        defaults: Mapping[str, Any],
        file: Mapping[str, Any] | None = None,
        env: Mapping[str, Any] | None = None,
        cli: Mapping[str, Any] | None = None,
    ) -> None: ...
    def get(self, path: str, default: Any = None) -> Any: ...
    def source(self, path: str) -> str | None: ...
    def as_dict(self) -> dict[str, Any]: ...
```

- `deep_merge`: where both values are mappings, merge recursively. Otherwise the
  override value wins: lists are **replaced**, not concatenated; `None` overrides.
  Neither input is mutated, and the result shares no mutable containers with either
  input (mutating the result never changes an input).
- `parse_env(environ, prefix)`: considers only keys starting with `f"{prefix}_"`.
  The remainder is split on `__` into lowercased path segments, so
  `APP_DB__POOL__SIZE=10` → `{"db": {"pool": {"size": 10}}}`. Values are coerced:
  `true`/`false` (case-insensitive) → `bool`; an optional leading `-` followed by
  ASCII digits → `int`; anything else stays `str`. An empty segment (e.g. `APP_`
  or `APP_DB____HOST`) → `ValueError`.
- `Config`: layers are snapshotted (deep-copied) at construction, so later mutation
  of the inputs does not affect it. `None` layers count as empty.
- `get(path)`: dotted lookup (`"db.pool.size"`). The highest-precedence layer that
  contains the **full** path wins; a value of `None` still counts as present. A path
  that runs through a non-mapping is absent in that layer. Returns `default` if no
  layer has it.
- `source(path)`: the name of the winning layer (`"cli"`, `"env"`, `"file"`,
  `"defaults"`), or `None` if absent.
- `as_dict()`: the fully merged effective configuration as a fresh `dict`.

## Part 5 — Inventory reconciliation and pricing

Warehouse count vs. system of record, plus volume pricing and support SLA banding.

```python
class Reconciliation(NamedTuple):
    missing: frozenset[str]
    unexpected: frozenset[str]
    mismatched: dict[str, int]
    @property
    def is_clean(self) -> bool: ...

def reconcile(expected: Mapping[str, int], counted: Mapping[str, int]) -> Reconciliation: ...

class PriceTiers:
    def __init__(self, tiers: Iterable[tuple[int, Decimal]]) -> None: ...
    def unit_price(self, qty: int) -> Decimal: ...
    def line_total(self, qty: int) -> Decimal: ...

def sla_tier(response_minutes: float) -> str: ...
```

- `reconcile`:
  - `missing`: SKUs in `expected` absent from `counted`.
  - `unexpected`: SKUs in `counted` absent from `expected`.
  - `mismatched`: SKUs in both with differing quantities, mapped to `counted - expected`.
    A counted quantity of `0` against expected `3` is a mismatch of `-3`, **not** a missing SKU.
    Keys are in ascending SKU order.
  - `is_clean` is `True` iff all three collections are empty.
  - Any negative quantity → `ValueError`.
- `PriceTiers`: tiers are `(min_qty, unit_price)`. They may be given in any order and
  are sorted. Validation (all `ValueError` unless noted): no tiers; the first tier must
  start at `min_qty == 1`; duplicate `min_qty`; a price `<= 0`. A price that is not a
  `Decimal` (e.g. `float`) → `TypeError`.
  - `unit_price(qty)`: the tier whose `min_qty <= qty` is largest applies; a boundary
    quantity belongs to the new tier. `qty < 1` → `ValueError`.
  - `line_total(qty)`: `unit_price(qty) * qty`, exact `Decimal` arithmetic.
- `sla_tier(response_minutes)`: `<= 15` → `"critical"`, `<= 60` → `"high"`,
  `<= 240` → `"normal"`, otherwise `"low"` (including `inf`). Boundaries are inclusive
  on the upper side. Negative or NaN → `ValueError`.

## Part 6 — Records, grouping and hashability

Shipment manifests: immutable records, grouping, turning nested request params into
cache keys, and a defaulting dict for lazily built per-key state.

```python
class Shipment(NamedTuple):
    sku: str
    qty: int
    dest: str
    def with_qty(self, qty: int) -> Shipment: ...

def group_by[T, K](items: Iterable[T], key: Callable[[T], K]) -> dict[K, list[T]]: ...
def freeze(value: Any) -> Hashable: ...

class AutoVivifyDict[K, V](dict[K, V]):
    def __init__(self, factory: Callable[[], V]) -> None: ...
    def __missing__(self, key: K) -> V: ...
```

- `Shipment`: immutable (assignment raises `AttributeError`), hashable, unpacks like a
  tuple. `with_qty` returns a new record.
- `group_by`: returns a **plain** `dict` (no `defaultdict` behaviour leaks out, so
  `result["missing"]` raises `KeyError`). Keys appear in first-seen order; items within
  a group keep input order. Consumes the iterable once.
- `freeze(value)`: produces a hashable, structurally comparable key from nested
  request params.
  - Mappings become order-independent: `{"a": 1, "b": 2}` and `{"b": 2, "a": 1}` freeze equal.
  - Lists and tuples with the same contents are **not** equal (`freeze([1, 2]) != freeze((1, 2))`),
    and list order matters.
  - Sets are order-independent, and a mapping never collides with a set of pairs.
  - Unhashable leaves (e.g. `bytearray`) → `TypeError`.
- `AutoVivifyDict(factory)`: `d[key]` on a missing key calls `factory()`, stores the
  result, and returns it. `get`, `in` and `len` never create entries. `factory` must be
  callable (`TypeError` otherwise). `copy.copy(d)` keeps the factory.

---

## Concepts exercised

- `OrderedDict` as an LRU (`move_to_end`, `popitem(last=False)`), mutation during iteration
- Injected clocks, expiry boundaries, `dict` vs `__missing__` lookup paths
- `deque` as a sliding window, state pruning for per-key maps
- `Counter`, `heapq.nsmallest` with composite keys for stable tie-breaking
- Recursive merges that avoid aliasing, `ChainMap`-style precedence, `re`, env var conventions
- Set algebra on `dict.keys()` views, `frozenset` results, `NamedTuple` with methods
- `bisect_right` / `bisect_left` boundary semantics, `Decimal` vs `float` money
- `defaultdict` internally vs returning a plain `dict`, hashability of nested data, `copy` of dict subclasses

## Run

```
uv run pytest exercises/01_builtin_data_structures
uv run pytest exercises/01_builtin_data_structures -k part2
uv run pytest exercises/01_builtin_data_structures --solutions   # reference answers
```

## Stretch (untested)

- Make `TTLCache` O(1) for expiry by keeping a second ordered index on expiry time.
- Replace `SlidingWindowLimiter`'s per-client deques with a fixed-bucket counter and
  compare memory use under 1M distinct clients.
- Add `Config.watch(path)` that reports when a layer change alters the effective value.
- Give `freeze` a memo for shared sub-objects and check it survives self-referencing dicts.
