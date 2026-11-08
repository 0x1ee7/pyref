"""Built-in data structures: caches, rate limits, ranking, config, reconciliation, records."""

from __future__ import annotations

import bisect
import copy
import heapq
import math
import re
import time
from collections import Counter, OrderedDict, defaultdict, deque
from collections.abc import Callable, Hashable, Iterable, Mapping
from decimal import Decimal
from typing import Any, NamedTuple

# ---------------------------------------------------------------------------
# Part 1: TTL + LRU cache
# ---------------------------------------------------------------------------


class TTLCache[K, V]:
    """Bounded LRU cache whose entries expire ``ttl`` seconds after being written.

    ``OrderedDict`` order is the recency order (least recent first). Expiry times
    are not monotonic in that order because ``get`` reorders entries without
    extending their TTL, so expiry is found by scanning.
    """

    def __init__(self, maxsize: int, ttl: float, clock: Callable[[], float] = time.monotonic) -> None:
        if maxsize < 1:
            raise ValueError(f"maxsize must be >= 1, got {maxsize}")
        if not ttl > 0:
            raise ValueError(f"ttl must be > 0, got {ttl}")
        self._maxsize = maxsize
        self._ttl = ttl
        self._clock = clock
        self._data: OrderedDict[K, tuple[V, float]] = OrderedDict()
        self._hits = 0
        self._misses = 0

    def _purge(self, now: float) -> None:
        # Collect first: we must not mutate the OrderedDict while iterating it.
        expired = [key for key, (_, expires_at) in self._data.items() if now >= expires_at]
        for key in expired:
            del self._data[key]

    def get(self, key: K, default: V | None = None) -> V | None:
        now = self._clock()
        entry = self._data.get(key)
        if entry is not None and now >= entry[1]:
            del self._data[key]
            entry = None
        if entry is None:
            self._misses += 1
            return default
        self._data.move_to_end(key)
        self._hits += 1
        return entry[0]

    def put(self, key: K, value: V) -> None:
        now = self._clock()
        self._purge(now)
        self._data[key] = (value, now + self._ttl)
        self._data.move_to_end(key)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def keys(self) -> list[K]:
        self._purge(self._clock())
        return list(self._data)

    def __contains__(self, key: object) -> bool:
        entry = self._data.get(key)
        return entry is not None and self._clock() < entry[1]

    def __len__(self) -> int:
        self._purge(self._clock())
        return len(self._data)

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses


# ---------------------------------------------------------------------------
# Part 2: Sliding-window rate limiter
# ---------------------------------------------------------------------------


class SlidingWindowLimiter:
    """Allow at most ``limit`` accepted requests per client in any trailing ``window``."""

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic) -> None:
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        if not window > 0:
            raise ValueError(f"window must be > 0, got {window}")
        self._limit = limit
        self._window = window
        self._clock = clock
        self._events: dict[str, deque[float]] = {}

    def _prune_client(self, client: str, now: float) -> deque[float]:
        """Drop expired timestamps for ``client`` and return its live events (no entry is created)."""
        events = self._events.get(client)
        if events is None:
            return deque()
        # An event at t is live iff now - t < window, so expired iff now - t >= window.
        while events and now - events[0] >= self._window:
            events.popleft()
        return events

    def allow(self, client: str) -> bool:
        now = self._clock()
        events = self._prune_client(client, now)
        if len(events) >= self._limit:
            return False
        self._events.setdefault(client, deque()).append(now)
        return True

    def remaining(self, client: str) -> int:
        events = self._prune_client(client, self._clock())
        return max(0, self._limit - len(events))

    def retry_after(self, client: str) -> float:
        now = self._clock()
        events = self._prune_client(client, now)
        if len(events) < self._limit:
            return 0.0
        return events[0] + self._window - now

    def prune(self) -> int:
        now = self._clock()
        idle = [client for client in self._events if not self._prune_client(client, now)]
        for client in idle:
            del self._events[client]
        return len(idle)


# ---------------------------------------------------------------------------
# Part 3: Top-K product ranking
# ---------------------------------------------------------------------------


def top_k_products(sales: Iterable[tuple[str, int]], k: int) -> list[tuple[str, int]]:
    """Return the ``k`` best-selling SKUs by total units, ties broken by SKU ascending.

    Aggregates in a single pass (memory is O(distinct SKUs)) and selects with
    ``heapq.nsmallest`` over ``(-units, sku)``, which is O(n log k).
    """
    totals: Counter[str] = Counter()
    for sku, units in sales:
        if units < 0:
            raise ValueError(f"negative units for {sku!r}: {units}")
        totals[sku] += units
    if k <= 0:
        return []
    return heapq.nsmallest(k, totals.items(), key=lambda item: (-item[1], item[0]))


# ---------------------------------------------------------------------------
# Part 4: Layered configuration
# ---------------------------------------------------------------------------


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto ``base`` without mutating or aliasing either."""
    result: dict[str, Any] = {key: copy.deepcopy(value) for key, value in base.items()}
    for key, value in override.items():
        existing = result.get(key)
        if isinstance(value, Mapping) and isinstance(existing, Mapping):
            result[key] = deep_merge(existing, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


_INT_RE = re.compile(r"-?[0-9]+")


def _coerce(raw: str) -> Any:
    lowered = raw.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if _INT_RE.fullmatch(raw):
        return int(raw)
    return raw


def parse_env(environ: Mapping[str, str], prefix: str) -> dict[str, Any]:
    """Turn ``PREFIX_A__B=value`` variables into a nested dict ``{"a": {"b": value}}``."""
    marker = f"{prefix}_"
    result: dict[str, Any] = {}
    for name, raw in environ.items():
        if not name.startswith(marker):
            continue
        segments = name[len(marker):].split("__")
        if any(not segment for segment in segments):
            raise ValueError(f"malformed environment variable name: {name!r}")
        node = result
        for segment in segments[:-1]:
            node = node.setdefault(segment.lower(), {})
        node[segments[-1].lower()] = _coerce(raw)
    return result


_PRECEDENCE = ("cli", "env", "file", "defaults")  # highest first


def _lookup(layer: Mapping[str, Any], parts: list[str]) -> tuple[bool, Any]:
    node: Any = layer
    for part in parts:
        if not isinstance(node, Mapping) or part not in node:
            return False, None
        node = node[part]
    return True, node


class Config:
    """Effective configuration: cli > env > file > defaults, resolved per dotted path."""

    def __init__(
        self,
        *,
        defaults: Mapping[str, Any],
        file: Mapping[str, Any] | None = None,
        env: Mapping[str, Any] | None = None,
        cli: Mapping[str, Any] | None = None,
    ) -> None:
        self._layers: dict[str, dict[str, Any]] = {
            "defaults": copy.deepcopy(dict(defaults)),
            "file": copy.deepcopy(dict(file)) if file is not None else {},
            "env": copy.deepcopy(dict(env)) if env is not None else {},
            "cli": copy.deepcopy(dict(cli)) if cli is not None else {},
        }

    def _find(self, path: str) -> tuple[str, Any] | None:
        parts = path.split(".")
        for name in _PRECEDENCE:
            found, value = _lookup(self._layers[name], parts)
            if found:
                return name, value
        return None

    def get(self, path: str, default: Any = None) -> Any:
        hit = self._find(path)
        return default if hit is None else hit[1]

    def source(self, path: str) -> str | None:
        hit = self._find(path)
        return None if hit is None else hit[0]

    def as_dict(self) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for name in reversed(_PRECEDENCE):
            merged = deep_merge(merged, self._layers[name])
        return merged


# ---------------------------------------------------------------------------
# Part 5: Reconciliation and pricing
# ---------------------------------------------------------------------------


class Reconciliation(NamedTuple):
    missing: frozenset[str]
    unexpected: frozenset[str]
    mismatched: dict[str, int]

    @property
    def is_clean(self) -> bool:
        return not (self.missing or self.unexpected or self.mismatched)


def reconcile(expected: Mapping[str, int], counted: Mapping[str, int]) -> Reconciliation:
    for source in (expected, counted):
        for sku, quantity in source.items():
            if quantity < 0:
                raise ValueError(f"negative quantity for {sku!r}: {quantity}")
    missing = frozenset(expected.keys() - counted.keys())
    unexpected = frozenset(counted.keys() - expected.keys())
    mismatched = {
        sku: counted[sku] - expected[sku]
        for sku in sorted(expected.keys() & counted.keys())
        if counted[sku] != expected[sku]
    }
    return Reconciliation(missing, unexpected, mismatched)


class PriceTiers:
    """Volume price breaks: the tier with the largest ``min_qty <= qty`` applies."""

    def __init__(self, tiers: Iterable[tuple[int, Decimal]]) -> None:
        ordered = sorted(tiers, key=lambda tier: tier[0])
        if not ordered:
            raise ValueError("at least one price tier is required")
        if ordered[0][0] != 1:
            raise ValueError(f"first tier must start at quantity 1, got {ordered[0][0]}")
        for min_qty, price in ordered:
            if not isinstance(price, Decimal):
                raise TypeError(f"price for tier {min_qty} must be Decimal, got {type(price).__name__}")
            if price <= 0:
                raise ValueError(f"price for tier {min_qty} must be > 0, got {price}")
        for (previous, _), (current, _) in zip(ordered, ordered[1:]):
            if previous == current:
                raise ValueError(f"duplicate tier at quantity {current}")
        self._min_qtys = [min_qty for min_qty, _ in ordered]
        self._prices = [price for _, price in ordered]

    def unit_price(self, qty: int) -> Decimal:
        if qty < 1:
            raise ValueError(f"quantity must be >= 1, got {qty}")
        index = bisect.bisect_right(self._min_qtys, qty) - 1
        return self._prices[index]

    def line_total(self, qty: int) -> Decimal:
        return self.unit_price(qty) * qty


_SLA_BOUNDS = (15.0, 60.0, 240.0)
_SLA_NAMES = ("critical", "high", "normal", "low")


def sla_tier(response_minutes: float) -> str:
    if math.isnan(response_minutes) or response_minutes < 0:
        raise ValueError(f"response_minutes must be a non-negative number, got {response_minutes}")
    # bisect_left: a value equal to a bound lands in the bucket that bound closes.
    return _SLA_NAMES[bisect.bisect_left(_SLA_BOUNDS, response_minutes)]


# ---------------------------------------------------------------------------
# Part 6: Records, grouping, hashability
# ---------------------------------------------------------------------------


class Shipment(NamedTuple):
    sku: str
    qty: int
    dest: str

    def with_qty(self, qty: int) -> Shipment:
        return self._replace(qty=qty)


def group_by[T, K](items: Iterable[T], key: Callable[[T], K]) -> dict[K, list[T]]:
    groups: defaultdict[K, list[T]] = defaultdict(list)
    for item in items:
        groups[key(item)].append(item)
    return dict(groups)


# Private sentinels tag containers so that different container kinds with the
# same contents never freeze to equal values (and user data can't forge a tag).
_MAP_TAG = object()
_LIST_TAG = object()
_SET_TAG = object()


def freeze(value: Any) -> Hashable:
    if isinstance(value, Mapping):
        return (_MAP_TAG, frozenset((k, freeze(v)) for k, v in value.items()))
    if isinstance(value, list):
        return (_LIST_TAG, tuple(freeze(v) for v in value))
    if isinstance(value, tuple):
        return tuple(freeze(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return (_SET_TAG, frozenset(freeze(v) for v in value))
    hash(value)  # raises TypeError for unhashable leaves
    return value


class AutoVivifyDict[K, V](dict[K, V]):
    """Dict that builds missing values on ``d[key]`` only; ``get``/``in`` never create."""

    def __init__(self, factory: Callable[[], V]) -> None:
        if not callable(factory):
            raise TypeError("factory must be callable")
        super().__init__()
        self.factory = factory

    def __missing__(self, key: K) -> V:
        value = self.factory()
        self[key] = value
        return value
