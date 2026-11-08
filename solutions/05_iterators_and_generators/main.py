"""Iterators and generators: reference implementation.

Design rule for this module: every pipeline stage is lazy. Functions that
validate arguments do so eagerly in a plain function and delegate to a
generator, so errors surface at the call site rather than on first ``next()``.
"""

from __future__ import annotations

import heapq
import itertools
from collections import Counter, deque
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from operator import attrgetter, itemgetter
from typing import Any

# ---------------------------------------------------------------------------
# Part 1: iterator protocol and re-iterable containers
# ---------------------------------------------------------------------------


class Countdown:
    """One-shot iterator yielding ``start, start-1, ..., 1``."""

    __slots__ = ("_next",)

    def __init__(self, start: int) -> None:
        if start < 0:
            raise ValueError(f"start must be >= 0, got {start}")
        self._next = start

    def __iter__(self) -> Countdown:
        return self

    def __next__(self) -> int:
        if self._next <= 0:
            raise StopIteration
        value = self._next
        self._next -= 1
        return value


class DatasetReader[T]:
    """One-shot cursor over a ``Dataset`` snapshot."""

    __slots__ = ("_items", "_index")

    def __init__(self, items: tuple[T, ...]) -> None:
        self._items = items
        self._index = 0

    def __iter__(self) -> DatasetReader[T]:
        return self

    def __next__(self) -> T:
        if self._index >= len(self._items):
            raise StopIteration
        item = self._items[self._index]
        self._index += 1
        return item


class Dataset[T]:
    """Re-iterable collection: each ``iter()`` starts a fresh one-shot reader."""

    __slots__ = ("_items",)

    def __init__(self, records: Iterable[T]) -> None:
        self._items: tuple[T, ...] = tuple(records)

    def __iter__(self) -> DatasetReader[T]:
        return DatasetReader(self._items)

    def __len__(self) -> int:
        return len(self._items)


# ---------------------------------------------------------------------------
# Part 2: generator functions, lazy log pipeline
# ---------------------------------------------------------------------------

LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_RANK: dict[str, int] = {level: rank for rank, level in enumerate(LEVELS)}


class LogParseError(ValueError):
    """A log line could not be parsed."""

    def __init__(self, reason: str, lineno: int | None = None) -> None:
        self.reason = reason
        self.lineno = lineno
        text = f"line {lineno}: {reason}" if lineno is not None else reason
        super().__init__(text)


@dataclass(frozen=True, slots=True)
class LogRecord:
    ts: datetime
    level: str
    server: str
    message: str
    region: str | None = None


def _rank(level: str) -> int:
    try:
        return _RANK[level]
    except KeyError:
        raise ValueError(f"unknown level {level!r}") from None


def parse_line(line: str) -> LogRecord:
    parts = line.strip().split(maxsplit=3)
    if len(parts) != 4:
        raise LogParseError(f"expected 4 fields, got {len(parts)}")
    raw_ts, level, server, message = parts
    try:
        ts = datetime.fromisoformat(raw_ts)
    except ValueError as exc:
        raise LogParseError(f"bad timestamp {raw_ts!r}") from exc
    if ts.tzinfo is None:
        raise LogParseError(f"timestamp must be timezone-aware: {raw_ts!r}")
    if level not in _RANK:
        raise LogParseError(f"unknown level {level!r}")
    return LogRecord(ts=ts, level=level, server=server, message=message)


def parse_records(lines: Iterable[str]) -> Iterator[LogRecord]:
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = parse_line(line)
        except LogParseError as exc:
            raise LogParseError(exc.reason, lineno) from exc
        yield record


def filter_level(records: Iterable[LogRecord], minimum: str) -> Iterator[LogRecord]:
    threshold = _rank(minimum)
    return (r for r in records if _RANK[r.level] >= threshold)


def enrich(records: Iterable[LogRecord], regions: Mapping[str, str]) -> Iterator[LogRecord]:
    return (replace(r, region=regions.get(r.server, "unknown")) for r in records)


def first_error(records: Iterable[LogRecord]) -> LogRecord | None:
    error_rank = _RANK["ERROR"]
    return next((r for r in records if _RANK[r.level] >= error_rank), None)


# ---------------------------------------------------------------------------
# Part 3: lazily paginated API client
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: tuple[T, ...]
    next_cursor: str | None


class PaginationLoopError(RuntimeError):
    """The server returned a cursor that was already requested."""


def iter_pages[T](
    fetch_page: Callable[[str | None], Page[T]],
    start: str | None = None,
) -> Iterator[Page[T]]:
    requested: set[str | None] = set()
    cursor = start
    while True:
        if cursor in requested:
            raise PaginationLoopError(f"cursor {cursor!r} was already requested")
        requested.add(cursor)
        page = fetch_page(cursor)
        yield page
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def paginate[T](
    fetch_page: Callable[[str | None], Page[T]],
    start: str | None = None,
) -> Iterator[T]:
    for page in iter_pages(fetch_page, start):
        yield from page.items


# ---------------------------------------------------------------------------
# Part 4: yield from, return values, resource-owning generators
# ---------------------------------------------------------------------------


def counted[T](items: Iterable[T]) -> Generator[T, None, int]:
    count = 0
    for item in items:
        yield item
        count += 1
    return count


def chain_counts[T](*sources: Iterable[T]) -> Generator[T, None, list[int]]:
    counts: list[int] = []
    for source in sources:
        counts.append((yield from counted(source)))
    return counts


def drain[T, R](gen: Generator[T, Any, R]) -> tuple[list[T], R]:
    items: list[T] = []
    while True:
        try:
            items.append(next(gen))
        except StopIteration as stop:
            return items, stop.value


def _read_batches(
    open_source: Callable[[], AbstractContextManager[Iterable[str]]],
    size: int,
) -> Iterator[list[str]]:
    with open_source() as lines:
        batch: list[str] = []
        for line in lines:
            batch.append(line)
            if len(batch) == size:
                yield batch
                batch = []
        if batch:
            yield batch


def read_batches(
    open_source: Callable[[], AbstractContextManager[Iterable[str]]],
    size: int,
) -> Iterator[list[str]]:
    if size < 1:
        raise ValueError(f"size must be >= 1, got {size}")
    return _read_batches(open_source, size)


# ---------------------------------------------------------------------------
# Part 5: coroutine-style generators
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Stats:
    count: int
    mean: float | None
    minimum: float | None
    maximum: float | None


class StatsReset(Exception):
    """Thrown into ``running_stats`` to clear its accumulated state."""


_EMPTY_STATS = Stats(0, None, None, None)


def _running_stats(on_close: Callable[[Stats], None] | None) -> Generator[Stats, float, None]:
    count = 0
    mean = 0.0
    low: float | None = None
    high: float | None = None
    stats = _EMPTY_STATS
    try:
        while True:
            try:
                value = yield stats
            except StatsReset:
                count, mean, low, high = 0, 0.0, None, None
                stats = _EMPTY_STATS
                continue
            count += 1
            mean += (value - mean) / count  # Welford-style incremental mean
            low = value if low is None else min(low, value)
            high = value if high is None else max(high, value)
            stats = Stats(count, mean, low, high)
    finally:
        if on_close is not None:
            on_close(stats)


def running_stats(
    on_close: Callable[[Stats], None] | None = None,
) -> Generator[Stats, float, None]:
    gen = _running_stats(on_close)
    next(gen)  # prime to the first yield so callers can send() immediately
    return gen


# ---------------------------------------------------------------------------
# Part 6: the itertools toolkit
# ---------------------------------------------------------------------------


def write_in_batches[T](
    rows: Iterable[T],
    size: int,
    write: Callable[[list[T]], int],
) -> int:
    if size < 1:
        raise ValueError(f"size must be >= 1, got {size}")
    total = 0
    for batch in itertools.batched(rows, size):
        total += write(list(batch))
    return total


def _moving_average(values: Iterable[float], window: int) -> Iterator[float]:
    buf: deque[float] = deque(maxlen=window)
    for value in values:
        buf.append(value)
        if len(buf) == window:
            yield sum(buf) / window


def moving_average(values: Iterable[float], window: int) -> Iterator[float]:
    if window < 1:
        raise ValueError(f"window must be >= 1, got {window}")
    return _moving_average(values, window)


def group_by_server(
    records: Iterable[LogRecord],
) -> Iterator[tuple[str, list[LogRecord]]]:
    for server, group in itertools.groupby(records, key=attrgetter("server")):
        yield server, list(group)


def gaps(timestamps: Iterable[datetime]) -> Iterator[timedelta]:
    return (later - earlier for earlier, later in itertools.pairwise(timestamps))


def leading_below(values: Iterable[float], threshold: float) -> list[float]:
    return list(itertools.takewhile(lambda v: v < threshold, values))


def running_peak(values: Iterable[float]) -> Iterator[float]:
    return itertools.accumulate(values, max)


def flatten[T](batches: Iterable[Iterable[T]]) -> Iterator[T]:
    return itertools.chain.from_iterable(batches)


def independent_views[T](source: Iterable[T], n: int) -> tuple[Iterator[T], ...]:
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    return itertools.tee(source, n)


# ---------------------------------------------------------------------------
# Part 7: heapq merge of time-ordered streams
# ---------------------------------------------------------------------------


def merge_streams(*streams: Iterable[LogRecord]) -> Iterator[LogRecord]:
    return heapq.merge(*streams, key=attrgetter("ts"))


def top_servers(records: Iterable[LogRecord], k: int) -> list[tuple[str, int]]:
    if k < 0:
        raise ValueError(f"k must be >= 0, got {k}")
    counts = Counter(r.server for r in records)
    return heapq.nlargest(k, counts.items(), key=itemgetter(1))
