# 05 · Iterators and generators

Scenario: the ingestion service for a multi-region fleet. Logs arrive as huge
line streams, a vendor API pages through its results with opaque cursors, metrics
need rolling windows, a stats probe is driven by `send()`, and batched DB writes
must never materialise the whole input. Everything here must be **lazy**: no
function may read further ahead than the consumer asked for.

Implement everything in `main.py`. The tests in `test_iterators_generators.py`
are the full spec alongside this README.

Shared types/constants you must export from `main.py`: `LEVELS`, `LogRecord`,
`LogParseError`, `Page`, `PaginationLoopError`, `Stats`, `StatsReset`,
`Countdown`, `Dataset`, `DatasetReader`.

---

## Part 1 · Iterator protocol and re-iterable containers

### `Countdown(start: int)`
A **one-shot iterator** yielding `start, start-1, ..., 1`.

- `start < 0` raises `ValueError` (in `__init__`).
- `iter(c) is c`.
- Once exhausted it keeps raising `StopIteration` (never restarts).

### `Dataset[T](records: Iterable[T])`
A **re-iterable container** whose iterators are **one-shot**.

- `records` is snapshotted into a tuple at construction (so generator inputs work,
  and later mutation of the source list does not leak in).
- `iter(dataset)` returns a **new** `DatasetReader` every time; readers are independent.
- `len(dataset)` returns the number of records.

### `DatasetReader[T]`
One-shot iterator over a snapshot. `iter(reader) is reader`; exhausted readers stay exhausted.

**Concepts:** `__iter__`/`__next__`, `StopIteration`, iterable vs iterator distinction,
`__slots__`, snapshotting inputs.

---

## Part 2 · Generator functions: a lazy log pipeline

Log line format (fields separated by whitespace; the message is everything after the server):

```
2026-03-01T12:05:00+00:00 WARNING web-1 disk at 91% on /var
```

### `LEVELS: tuple[str, ...]`
`("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")`, ordered by severity.

### `LogParseError(ValueError)`
Attributes: `reason: str`, `lineno: int | None`. `str(err)` is `"line N: reason"` when
`lineno` is known, otherwise just the reason.

### `LogRecord` (frozen dataclass)
Fields: `ts: datetime`, `level: str`, `server: str`, `message: str`, `region: str | None = None`.

### `parse_line(line: str) -> LogRecord`
- Strips surrounding whitespace, then splits into exactly four parts: timestamp, level,
  server, message. The message keeps its internal spacing.
- Timestamp parsed with `datetime.fromisoformat`; it **must be timezone-aware**.
- Unknown level, wrong field count, bad or naive timestamp → `LogParseError` (no `lineno`).

### `parse_records(lines: Iterable[str]) -> Iterator[LogRecord]`
- Generator. Skips blank / whitespace-only lines.
- Malformed lines raise `LogParseError` with `lineno` set to the 1-based position in `lines`.
- Lazy: pulling one record consumes exactly the lines needed, nothing more.

### `filter_level(records, minimum: str) -> Iterator[LogRecord]`
Keeps records whose level is `>= minimum`. Unknown `minimum` raises `ValueError`
**immediately** on call, not on first `next()`. Must stay lazy.

### `enrich(records, regions: Mapping[str, str]) -> Iterator[LogRecord]`
Returns new records (via `dataclasses.replace`) with `region` set from `regions[server]`,
or `"unknown"` when the server is absent. Input records are not mutated. Lazy.

### `first_error(records: Iterable[LogRecord]) -> LogRecord | None`
First record with level `ERROR` or `CRITICAL`, else `None`. Must stop consuming as soon
as it matches (use a generator expression with `next(..., None)`).

**Concepts:** generator functions, generator expressions, laziness, eager argument
validation via wrapper functions, `dataclasses.replace`, exception chaining with `from`.

---

## Part 3 · Lazily paginated API client

The client is handed a `fetch_page(cursor: str | None) -> Page` callable; `None` means
"first page". It never talks to the network itself.

### `Page[T]` (frozen dataclass)
Fields: `items: tuple[T, ...]`, `next_cursor: str | None`. `next_cursor is None` marks the last page.
Empty `items` on a non-last page is legal.

### `PaginationLoopError(RuntimeError)`
Raised when the server hands back a cursor that was already requested.

### `iter_pages(fetch_page, start: str | None = None) -> Iterator[Page]`
Generator. Calls `fetch_page` **only when the consumer asks for the next page**. Yields every
page, including the final one. Before requesting any cursor, checks it has not been
requested before, else raises `PaginationLoopError`.

### `paginate(fetch_page, start: str | None = None) -> Iterator[T]`
Generator yielding items across all pages, built on `iter_pages` with `yield from`.
Calling `paginate(...)` performs **no** fetches; the first `next()` fetches exactly one page.

An exception from `fetch_page` propagates to the consumer; the generator is then finished.

**Concepts:** lazy paging, `yield from`, generator-based pipelines, generic functions (PEP 695),
error propagation and generator finalisation.

---

## Part 4 · `yield from`, return values, and resource-owning generators

### `counted(items: Iterable[T]) -> Generator[T, None, int]`
Yields every item and **returns** how many it yielded.

### `chain_counts(*sources: Iterable[T]) -> Generator[T, None, list[int]]`
Yields all items of every source in order via `yield from counted(source)`, and returns
the per-source counts (delegated return value captured from `yield from`).

### `drain(gen: Generator[T, Any, R]) -> tuple[list[T], R]`
Exhausts `gen` and returns `(items, return_value)`. Works for generators with no return value (`None`).

### `read_batches(open_source, size: int) -> Iterator[list[str]]`
- `open_source` is a zero-argument callable returning a context manager whose `__enter__`
  yields an iterable of lines (e.g. `lambda: open(path, encoding="utf-8")`).
- Yields lists of `size` lines; the final batch may be shorter. Lines are yielded raw
  (no stripping).
- The resource is opened **lazily** on the first `next()`, not when `read_batches` is called.
- The resource is released when the generator finishes, raises, or is `close()`d early.
- `size < 1` raises `ValueError` immediately on call.

**Concepts:** `yield from`, return value propagation via `StopIteration.value`, `with` inside
a generator, `GeneratorExit` and `close()`, eager validation.

---

## Part 5 · Coroutine-style generators: `send`, `throw`, `close`

### `Stats` (frozen dataclass)
Fields: `count: int`, `mean: float | None`, `minimum: float | None`, `maximum: float | None`.
The empty state is `Stats(0, None, None, None)`.

### `StatsReset(Exception)`
Thrown into the stats coroutine to clear its state.

### `running_stats(on_close: Callable[[Stats], None] | None = None) -> Generator[Stats, float, None]`
- Returns an already **primed** generator: it has run to its first `yield`, so `send(x)`
  works immediately. The primed value is discarded.
- Each `send(x)` folds `x` into the stats and returns the new `Stats`.
- The mean must be computed incrementally (Welford-style update), not by storing values.
- `throw(StatsReset())` resets the state; the `throw` call returns the empty `Stats`.
- Any other exception thrown in propagates out and finishes the generator.
- `close()` invokes `on_close(final_stats)` exactly once if provided, then the generator is finished.

**Concepts:** `send`/`throw`/`close`, `GeneratorExit`, `try/finally` cleanup, priming
coroutines, incremental statistics.

---

## Part 6 · The `itertools` toolkit

### `write_in_batches(rows: Iterable[T], size: int, write: Callable[[list[T]], int]) -> int`
Splits `rows` with `itertools.batched`, calls `write(batch_as_list)` per batch, and returns
the sum of the values `write` returned. Pulls only as many rows as the current batch needs.
`size < 1` raises `ValueError` immediately.

### `moving_average(values: Iterable[float], window: int) -> Iterator[float]`
Yields the mean of each **full** window of `window` consecutive values. Partial windows are
never emitted. `window < 1` raises `ValueError` immediately. Lazy.

### `group_by_server(records: Iterable[LogRecord]) -> Iterator[tuple[str, list[LogRecord]]]`
Groups **consecutive** records by server using `itertools.groupby`, yielding `(server, records)`.
Non-adjacent records of the same server form separate groups (this is intentional; document it).

### `gaps(timestamps: Iterable[datetime]) -> Iterator[timedelta]`
`later - earlier` for each adjacent pair (`itertools.pairwise`). Fewer than two items → nothing.

### `leading_below(values: Iterable[float], threshold: float) -> list[float]`
Values before the first value `>= threshold` (`itertools.takewhile`). Must not read past
the first breaching value.

### `running_peak(values: Iterable[float]) -> Iterator[float]`
Running maximum (`itertools.accumulate` with `max`). Empty input → empty output.

### `flatten(batches: Iterable[Iterable[T]]) -> Iterator[T]`
Lazily chains nested iterables (`itertools.chain.from_iterable`). Taking the first item must not
touch the second batch.

### `independent_views(source: Iterable[T], n: int) -> tuple[Iterator[T], ...]`
`n` independent iterators over one source (`itertools.tee`). `n < 1` raises `ValueError` immediately.

**Concepts:** `itertools.batched`, `groupby` pitfalls, `pairwise`, `takewhile` overreads,
`accumulate`, `chain.from_iterable`, `tee` buffering, `deque(maxlen=...)`.

---

## Part 7 · Merging time-ordered streams with `heapq`

### `merge_streams(*streams: Iterable[LogRecord]) -> Iterator[LogRecord]`
Lazily merges streams that are each already sorted by `ts`, using `heapq.merge`. On equal
timestamps the record from the earlier argument comes first. Must work on infinite streams
(consume with `islice`). Zero streams → empty.

### `top_servers(records: Iterable[LogRecord], k: int) -> list[tuple[str, int]]`
The `k` servers with the most records as `(server, count)`, highest count first, using
`heapq.nlargest`. `k == 0` → `[]`. `k < 0` raises `ValueError`. Tests only assert on
distinct counts.

**Concepts:** `heapq.merge` with `key=`, stability on ties, merging infinite iterators,
`heapq.nlargest` vs sorting, `Counter`.

---

## Run

```
uv run pytest exercises/05_iterators_and_generators
uv run pytest exercises/05_iterators_and_generators -k part2
uv run pytest exercises/05_iterators_and_generators --solutions
```

## Concepts exercised

- Iterator protocol: `__iter__`, `__next__`, one-shot vs re-iterable containers
- Generator functions, generator expressions, laziness and read-ahead
- `yield from` and `StopIteration.value` return delegation
- `send`, `throw`, `close`, `GeneratorExit`, `finally` cleanup of resources
- Eager argument validation wrapping lazy generators
- `itertools`: `batched`, `groupby`, `pairwise`, `takewhile`, `accumulate`, `chain.from_iterable`, `tee`
- `heapq.merge` and `heapq.nlargest`
- `deque(maxlen=)`, `Counter`, `dataclasses.replace`, PEP 695 generics

## Stretch (untested)

- Make `iter_pages` also expose the cursor for resumption after a crash.
- Replace `moving_average`'s `sum` with an O(1) running sum and reason about float drift.
- Implement `merge_streams` with a `key` that breaks ties by server name.
- Rewrite `running_stats` as a class-based iterator and compare ergonomics.
