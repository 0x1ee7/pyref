# 14 · Threads & Processes

You are maintaining **`linkwatch`**, an internal service that monitors the health of
documentation links, mirrors release artifacts, and runs housekeeping jobs. It is
mostly I/O (HTTP calls, disk, queues) with a little CPU work. Every network call is
injected as a plain callable, so nothing here touches the network: the tests pass in
fakes that block on `Event`/`Barrier` objects to force exactly the interleavings they
need.

Write everything in `main.py`. Only the standard library is available.

Background you should keep in mind while implementing:

- The GIL serialises Python bytecode, so threads buy you overlap for **I/O-bound** work
  (sockets, disk, `time.sleep`-like waits) but not parallel **CPU-bound** work. Use
  `ProcessPoolExecutor` (Part 7) for the latter. Process workers pay a spawn cost on
  Windows, so keep them for work that is actually CPU-heavy.
- `x += 1` on shared state is not atomic: it is several bytecodes, and a thread switch
  can land in the middle. Every shared mutable structure below must be guarded.
- Never hold a lock while calling out to user code (fetchers, loaders, handlers) unless
  the part explicitly says so. That is the usual route to deadlock.

---

## Part 1: Concurrent link checker

```python
@dataclass(frozen=True, slots=True)
class LinkResult:
    url: str
    status: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool: ...   # True iff status is not None and 200 <= status < 400


def check_links(
    urls: Iterable[str],
    *,
    fetch: Callable[[Any, str], int],
    session_factory: Callable[[], Any] | None = None,
    max_workers: int = 8,
) -> list[LinkResult]: ...
```

- Runs `fetch(session, url)` for each URL on a `ThreadPoolExecutor(max_workers=...)`.
  `fetch` returns an HTTP-style status code.
- Results are returned in **input order**, one `LinkResult` per input element.
  Duplicate URLs are fetched **once** per call and the result is repeated in place.
- Any `Exception` raised by `fetch` is captured per URL: `status=None`,
  `error="<ExceptionClassName>: <message>"`. Other URLs are unaffected.
- `session` is per worker thread, not per URL. Each worker thread calls
  `session_factory()` at most once, lazily, on its first URL, and reuses the result.
  If `session_factory` is `None`, `fetch` receives `None`. Sessions are not shared across
  threads.
- `max_workers < 1` raises `ValueError`. An empty input returns `[]`.

**Concepts:** `ThreadPoolExecutor`, ordering results from `map`/futures, per-item error
capture, `threading.local` for thread-confined resources, dedupe.

---

## Part 2: Bounded concurrency: downloader and resource pool

### 2a. `BoundedDownloader`

```python
class BoundedDownloader:
    def __init__(self, fetch: Callable[[str], bytes], max_concurrent: int) -> None: ...
    def download(self, url: str) -> bytes: ...
    def download_all(self, urls: Sequence[str]) -> list[bytes]: ...
    @property
    def in_flight(self) -> int: ...
    @property
    def peak_concurrency(self) -> int: ...
```

- `max_concurrent < 1` raises `ValueError`.
- `download(url)` blocks until one of `max_concurrent` slots is free, then calls
  `fetch(url)`. The slots are **shared by every caller on the same instance**: two threads
  each calling `download_all` together still never have more than `max_concurrent`
  fetches in flight. Use a `threading.Semaphore`.
- `in_flight` is the number of fetches running right now. `peak_concurrency` is the
  highest value `in_flight` has ever reached. It is 0 before any call. Measure it
  **inside** the slot, under a lock, so it can never exceed `max_concurrent`.
- `download_all(urls)` returns bytes in input order. It uses a thread pool of
  `min(len(urls), 16)` threads, and `[]` for empty input.
  If one or more fetches raise, `download_all` waits for every started fetch to finish,
  then re-raises the exception of the **earliest URL in input order** that failed.

### 2b. `ResourcePool[T]`

```python
class ResourcePool[T]:
    def __init__(self, factory: Callable[[], T], max_size: int) -> None: ...
    def acquire(self, timeout: float | None = None) -> T: ...
    def release(self, item: T) -> None: ...
    @contextmanager
    def lease(self, timeout: float | None = None) -> Iterator[T]: ...
    @property
    def created(self) -> int: ...
```

- A bounded pool of expensive objects (connections, parsers). Items are created lazily by
  `factory()`, at most `max_size` ever checked out or idle at once. Idle items are reused
  before a new one is created.
- `max_size < 1` raises `ValueError`.
- `acquire(timeout)` blocks while all `max_size` items are checked out. It raises
  `TimeoutError` if none is free within `timeout` seconds. `timeout=None` waits
  forever. `timeout=0` never blocks.
- `release(item)` returns the item to the pool and wakes one waiter. Releasing an item
  that is not currently checked out from this pool (foreign or already released) raises
  `ValueError`. Compare by identity.
- `lease(timeout)` is a context manager: acquire, yield, always release (even if the body
  raises).
- If `factory()` raises, the exception propagates from `acquire`, `created` is not
  incremented, and the capacity slot is given back so a later `acquire` can try again.
- `created` is the number of times `factory()` has returned successfully.
- Use a `threading.Condition`. Call `factory()` **without** holding the pool lock.

**Concepts:** `threading.Semaphore`, `Condition` (`wait_for`/`notify`), lock-protected
counters, measuring high-water marks, context managers for release-on-error, invariants
under failure.

---

## Part 3: Shared state and deadlock avoidance

### 3a. `Counter`

```python
class Counter:
    def increment(self, name: str, by: int = 1) -> int: ...   # returns the new value
    def get(self, name: str) -> int: ...                       # 0 for unknown names
    def snapshot(self) -> dict[str, int]: ...                 # an independent copy
```

- `by < 0` raises `ValueError`.
- Must be exact under heavy contention: this is the classic lost-update race. Eight
  threads each incrementing one name 5 000 times must end at exactly 40 000.

### 3b. `Account`, `transfer`, `InsufficientFunds`

```python
class InsufficientFunds(Exception): ...

class Account:
    def __init__(self, account_id: str, balance: int = 0) -> None: ...
    @property
    def account_id(self) -> str: ...
    @property
    def balance(self) -> int: ...
    def deposit(self, amount: int) -> None: ...
    def withdraw(self, amount: int) -> None: ...

def transfer(src: Account, dst: Account, amount: int) -> None: ...
```

- `Account(balance < 0)` raises `ValueError`. `deposit`/`withdraw` with `amount <= 0`
  raise `ValueError`.
- `withdraw` raises `InsufficientFunds` when `amount` exceeds the balance, and leaves
  the balance unchanged.
- `transfer` moves `amount` atomically: no other thread can observe the money in
  flight. It raises `ValueError` for `amount <= 0` or `src is dst`. If the source lacks
  funds it raises `InsufficientFunds` and neither account changes.
- Each `Account` owns a `threading.RLock`. `transfer` holds **both** locks while it calls
  `withdraw` and `deposit`, which re-acquire the same lock. The reentrancy is why you need an `RLock`.
- **Deadlock avoidance:** if thread 1 locks A then B while thread 2 locks B then A, they
  deadlock. Always acquire the two locks in one global order, for example by
  `(account_id, id(account))`, regardless of transfer direction.

**Concepts:** `Lock` vs `RLock`, lock ordering, atomic multi-object updates, invariant
preservation (money is conserved), race-free counters.

---

## Part 4: Caches: thread-safe LRU and single-flight loading

### 4a. `BoundedCache[K, V]`

```python
class BoundedCache[K, V]:
    def __init__(self, max_size: int) -> None: ...
    def get(self, key: K, default: V | None = None) -> V | None: ...
    def put(self, key: K, value: V) -> None: ...
    def __len__(self) -> int: ...
    def __contains__(self, key: object) -> bool: ...
    @property
    def hits(self) -> int: ...
    @property
    def misses(self) -> int: ...
```

- A least-recently-used cache. `get` on a present key counts a hit and makes the key
  most-recently-used. A missing key counts a miss and returns `default`.
- `put` inserts or updates a key and makes it most-recently-used. When the size would
  exceed `max_size`, evict the least-recently-used entry.
- `in` (`__contains__`) does **not** change recency and does not count as a hit or miss.
- `max_size < 1` raises `ValueError`. Every operation is thread-safe.

### 4b. `SingleFlightCache[K, V]`

```python
class SingleFlightCache[K, V]:
    def __init__(self, max_size: int = 128) -> None: ...
    def get_or_load(self, key: K, loader: Callable[[K], V]) -> V: ...
    @property
    def load_count(self) -> int: ...
```

This is request coalescing. Thousands of concurrent requests for the same expensive key
(a config document, a schema, a DNS result) must trigger one upstream load.

- If `key` is cached, return the cached value; `loader` is not called.
- Otherwise, if no load for `key` is in flight, the calling thread becomes the **leader**
  and calls `loader(key)`. **Do not hold the cache lock while calling `loader`.**
- If a load for `key` is already in flight, the caller **waits** for it and receives the
  same value. Several callers can wait on one load.
- If `loader` raises, the leader and every caller waiting on that load receive the same
  exception. Nothing is cached, so the next `get_or_load` for that key calls `loader`
  again.
- Loads for **different** keys proceed in parallel. One slow key must not block another.
- `load_count` counts how many times `loader` was invoked, across all keys.
- Store successful values in a `BoundedCache(max_size)`.

**Concepts:** LRU with `OrderedDict`, check-then-act races, per-key in-flight futures
(`threading.Event`), exception fan-out, not holding locks across callbacks.

---

## Part 5: Queues: graceful worker group and ordered pipeline

### 5a. `WorkerGroup[T, R]`

```python
@dataclass(frozen=True, slots=True)
class Failure[T]:
    item: T
    error: Exception

@dataclass(frozen=True, slots=True)
class GroupResult[T, R]:
    results: list[R]           # in completion order (not guaranteed to match submission)
    failures: list[Failure[T]]

class WorkerGroup[T, R]:
    def __init__(self, handler: Callable[[T], R], *, workers: int, queue_size: int = 0) -> None: ...
    def start(self) -> None: ...
    def submit(self, item: T) -> None: ...
    def close(self, timeout: float | None = None) -> GroupResult[T, R]: ...
    def __enter__(self) -> Self: ...      # calls start()
    def __exit__(self, *exc_info: object) -> None: ...   # calls close()
```

- `workers < 1` raises `ValueError`. `queue_size=0` means unbounded. A positive value
  bounds the queue and `submit` blocks when it is full (back-pressure).
- `start()` launches the threads. Calling it twice raises `RuntimeError`.
- `submit(item)` before `start()`, or after `close()` has begun, raises `RuntimeError`.
  A `submit` that was blocked on a full queue when `close()` began also raises
  `RuntimeError`.
- Handler exceptions (`Exception`) are recorded as `Failure(item, error)`. Workers keep
  going.
- `close()` is **graceful**: every item submitted before `close()` is still processed,
  then the workers exit and are joined. Use `queue.Queue.shutdown(immediate=False)`.
  Workers drain the queue, then `get()` raises `queue.ShutDown` and they return.
  Sentinel objects are the older alternative you should know.
- `close(timeout)` raises `TimeoutError` if workers are still running after `timeout`
  seconds. The group stays in the closing state, and calling `close()` again waits for the
  remaining work and returns the same result.
- `close()` before `start()` raises `RuntimeError`. A second `close()` after completion
  returns the same `GroupResult`.

### 5b. `run_pipeline`

```python
def run_pipeline(
    items: Iterable[Any],
    stages: Sequence[Callable[[Any], Any]],
    *,
    workers_per_stage: int = 2,
    queue_size: int = 4,
) -> list[Any]: ...
```

- Stages are chained with bounded `queue.Queue`s. Each stage runs `workers_per_stage`
  threads, and items flow through the stages concurrently. A feeder thread consumes
  `items`, so the calling thread can collect results while the queues are full.
- The output list is in **input order**, even though workers finish out of order.
  Tag each item with its sequence number as it enters the pipeline.
- `stages == []` returns `list(items)`. An empty input returns `[]`.
- `workers_per_stage < 1` or `queue_size < 1` raises `ValueError`.
- If any stage (or the iterable) raises, `run_pipeline` raises that exception and first
  tears everything down. Shut the queues down with `immediate=True` so blocked producers
  and consumers wake up, then join every thread. It must never hang and must leave no
  threads running.

**Concepts:** `queue.Queue` with bounded back-pressure, `shutdown()` vs sentinels,
drain-then-stop, per-stage completion tracking, fail-fast teardown, reordering by sequence
number.

---

## Part 6: Periodic scheduler with clean stop

```python
class Scheduler:
    def add(self, name: str, interval: float, job: Callable[[], None]) -> None: ...
    def start(self) -> None: ...
    def stop(self, timeout: float = 5.0) -> None: ...
    @property
    def errors(self) -> list[tuple[str, BaseException]]: ...
    def __enter__(self) -> Self: ...      # calls start()
    def __exit__(self, *exc_info: object) -> None: ...   # calls stop()
```

- Each job gets its own thread. It runs **immediately** on `start()`, then waits
  `interval` seconds after each run finishes, then runs again.
- `add` raises `ValueError` for `interval <= 0` or a duplicate `name`. It raises
  `RuntimeError` once the scheduler has started.
- `start()` a second time, or after `stop()`, raises `RuntimeError`.
- A job that raises an `Exception` is recorded as `(name, exc)` in `errors`, and the job
  keeps running on schedule.
- `stop()` wakes sleeping jobs immediately: a job with `interval=3600` must not delay
  `stop()`. It sets the stop flag, joins every job thread, and raises `TimeoutError` if a
  job is still running after `timeout` seconds in total. Calling `stop()` again after a
  timeout keeps waiting. Once `stop()` returns, no job runs again. `stop()` before
  `start()` is a no-op.
- Do not use `time.sleep` for the waiting. Use an `Event` so `stop()` can interrupt it.

**Concepts:** `threading.Event` as an interruptible sleep, thread lifecycle and joining,
graceful vs forceful stop with timeouts, error isolation per job, avoiding unbounded
threads.

---

## Part 7: CPU-bound parallel map with processes

```python
def is_prime(n: int) -> bool: ...
def count_primes(candidates: range) -> int: ...
def chunk_ranges(start: int, stop: int, parts: int) -> list[range]: ...
def parallel_map[T, R](fn: Callable[[T], R], items: Sequence[T], *, workers: int = 2) -> list[R]: ...
```

- `is_prime`: `False` for `n < 2`. Use trial division up to `math.isqrt(n)`.
- `count_primes(candidates)`: number of primes among the range's values. This is pure,
  CPU-heavy work, which is why it lives in a process pool.
- `chunk_ranges(start, stop, parts)`: split `[start, stop)` into contiguous, non-empty
  `range`s. Chunk sizes differ by at most 1, and the first chunks get the extra element.
  Empty input (`stop <= start`) returns `[]`. The result never contains more than
  `min(parts, stop - start)` chunks. `parts < 1` raises `ValueError`.
- `parallel_map(fn, items, workers)`: `ProcessPoolExecutor` map. Results are in input
  order. `workers < 1` raises `ValueError`. Empty input returns `[]` without starting a
  pool. If `fn` raises for an item, the exception propagates to the caller.
- `fn` and everything it touches must be picklable. Module-level functions in `main.py`
  are. Lambdas and locals are not. Spawn is the default on Windows, so each worker
  re-imports your module.

**Concepts:** GIL vs CPU-bound work, `ProcessPoolExecutor` and picklability, the spawn
start method, chunking to amortise IPC overhead, the cost of process startup.

---

## Concepts exercised

- GIL implications: threads for I/O, processes for CPU
- `threading`: `Lock`, `RLock`, `Condition`, `Event`, `Semaphore`, `Barrier`, `local`
- `queue.Queue`: bounded back-pressure, `shutdown()` draining, sentinels
- `concurrent.futures`: `ThreadPoolExecutor`, `ProcessPoolExecutor`, exception propagation
- Race conditions and their fixes; deadlock avoidance via lock ordering
- Thread-safe caches, LRU eviction, single-flight request coalescing
- Clean shutdown: draining, timeouts, joining, no leaked threads
- Pipelines with ordered results and fail-fast teardown

## Run

```bash
uv run pytest exercises/14_threads_and_processes
uv run pytest exercises/14_threads_and_processes -k part3
uv run pytest exercises/14_threads_and_processes --solutions     # reference implementation
```

## Stretch (untested)

- Benchmark the link checker's `max_workers` against a fake fetch that releases the GIL
  (`time.sleep`), then against a CPU-bound fetch. Find where threads stop helping.
- Replace the `Condition` in `ResourcePool` with a `Semaphore` plus a `deque` and compare
  the code and failure modes.
- Add a `Scheduler` backoff: double the interval after each failure, cap it, reset on
  success.
- Port `WorkerGroup` to `asyncio` tasks and a `asyncio.Queue`. Compare cancellation
  semantics with threads.
- Use `concurrent.futures.as_completed` to stream `check_links` results as they finish,
  and measure the latency difference against ordered collection.
