# 15 · asyncio

Scenario: the internals of a service that talks to a fleet of flaky upstreams. You build an
aggregator that tolerates partial failure, a strict fan-out, cancellation-safe helpers, a bounded
fetcher, a queue-backed worker pool with graceful shutdown, an async paginator and resource pool,
and the resilience primitives (retry, circuit breaker, token bucket).

Everything lives in `main.py`. Tests never sleep for long: real delays are at most 50 ms, and
timing-sensitive checks use `Event`s, injected clocks, and `settle()`-style yielding. Each test
runs its coroutine through `asyncio.run` under a 5 s hard deadline, so a deadlock fails instead
of hanging.

Code must run on Python 3.13+.

---

## Part 1 · Fan-out with `gather(return_exceptions=True)` and per-call timeouts

```python
@dataclass(frozen=True, slots=True)
class SourceResult:
    name: str
    ok: bool
    value: object = None
    error: str | None = None
    timed_out: bool = False

async def aggregate(
    sources: Mapping[str, Callable[[], Awaitable[object]]],
    *,
    per_call_timeout: float,
) -> dict[str, SourceResult]: ...
```

- Every source factory is called exactly once; all calls run concurrently.
- Each call runs under `asyncio.timeout(per_call_timeout)`.
- Success: `ok=True`, `value` set, `error=None`.
- Exception `exc` raised by a source: `ok=False`, `error=f"{type(exc).__name__}: {exc}"`.
- Timeout: `ok=False`, `timed_out=True`, `error="TimeoutError: per-call timeout"`. Use the timeout's
  own `expired()` to tell our timeout apart from a `TimeoutError` a source raises itself.
- Returned dict keys follow the order of `sources`. Each `SourceResult.name` equals its key.
- `per_call_timeout <= 0` raises `ValueError`.
- Cancelling the caller of `aggregate` raises `CancelledError` to the caller and cancels every
  in-flight source. It is never converted into a result.

## Part 2 · Structured concurrency with `TaskGroup`

```python
async def fetch_all_strict[T](
    sources: Mapping[str, Callable[[], Awaitable[T]]],
) -> dict[str, T]: ...
```

- All-or-nothing. Success returns `{name: value}` in source order. An empty mapping returns `{}`.
- The first failing source cancels every sibling. The function raises an `ExceptionGroup`
  containing every exception that escaped a sibling task. Cancelled siblings contribute nothing.
- The group is raised only after every sibling has finished cancelling, so a sibling's `finally`
  has already run when the caller catches it.

## Part 3 · Cancellation, timeouts, cleanup, and shielding

```python
async def run_with_cleanup[T](
    body: Callable[[], Awaitable[T]],
    cleanup: Callable[[], Awaitable[None]],
    *,
    timeout: float,
) -> T: ...

async def shielded_commit[T](operation: Callable[[], Awaitable[T]]) -> T: ...
```

`run_with_cleanup`:
- Runs `body` under `asyncio.timeout(timeout)`. Returns its value on success.
- `cleanup()` is awaited exactly once on every exit path: success, body exception, timeout, and
  external cancellation. `cleanup` itself may await.
- On timeout, raises `TimeoutError` (the one from `asyncio.timeout`). Body exceptions propagate
  unchanged. External cancellation propagates as `CancelledError`, never as `TimeoutError`.

`shielded_commit`:
- Runs `operation` as its own task, shielded from the caller's cancellation.
- Normal completion returns the operation's value; a failure is raised to the caller.
- If the caller is cancelled, `CancelledError` is raised to the caller immediately. The operation
  keeps running to completion in the background.
- If that background operation later fails, log the exception at `ERROR` on logger
  `pyref.commit` (with `exc_info`) so it is not silently lost. A cancelled background task logs
  nothing.

## Part 4 · Bounded concurrency with `Semaphore`, plus `to_thread`

```python
class BoundedFetcher[T, R]:
    def __init__(self, limit: int) -> None: ...
    @property
    def peak_in_flight(self) -> int: ...
    async def run(self, items: Sequence[T], fetch: Callable[[T], Awaitable[R]]) -> list[R]: ...

async def read_files(paths: Sequence[Path], *, limit: int) -> dict[Path, bytes]: ...
```

`BoundedFetcher`:
- `limit < 1` raises `ValueError`.
- At most `limit` `fetch` calls are in flight at any moment. `peak_in_flight` is the highest
  concurrency observed over the fetcher's lifetime.
- Results come back in input order, regardless of completion order.
- The first failure cancels the remaining fetches and raises `ExceptionGroup` (use `TaskGroup`).
- An empty `items` returns `[]`.

`read_files`:
- Uses a `BoundedFetcher` and reads each file with `asyncio.to_thread(path.read_bytes)`, so the
  event loop is never blocked by disk IO.
- Returns `{path: contents}` in input order. A missing file raises `ExceptionGroup` containing a
  `FileNotFoundError`.

## Part 5 · Queue-backed worker pool with graceful shutdown

```python
class WorkerPool[T, R]:
    def __init__(self, handler: Callable[[T], Awaitable[R]], *, workers: int) -> None: ...
    async def __aenter__(self) -> Self: ...       # starts the workers
    async def __aexit__(self, *exc_info: object) -> None: ...  # aclose(drain=True)
    async def submit(self, item: T) -> R: ...
    async def aclose(self, *, drain: bool = True) -> None: ...
    @property
    def processed(self) -> int: ...
```

- `workers < 1` raises `ValueError`.
- `submit` outside `async with` raises `RuntimeError("pool is not running")`. After close it raises
  `RuntimeError("pool is closed")`.
- `submit` returns the handler's result. If the handler raises, that exception goes to that
  submitter only. The worker keeps running.
- `processed` counts items the handler completed successfully.
- Concurrency is bounded by `workers`.
- `aclose(drain=True)`: stop accepting new submissions, wait until every queued item is handled,
  then cancel the workers and await them.
- `aclose(drain=False)`: stop accepting submissions, cancel the workers, and fail every pending
  submission (in flight and queued) with `RuntimeError`.
- A submitter that is cancelled while its item is still queued must have its item skipped. The
  handler is never called for it.
- Calling `aclose` again returns immediately.
- After `async with` exits, no worker tasks remain.

## Part 6 · Async generators and async context managers

```python
@dataclass(frozen=True, slots=True)
class Page[T]:
    items: Sequence[T]
    next_cursor: str | None

async def paginate[T](
    fetch_page: Callable[[str | None], Awaitable[Page[T]]],
    *,
    max_pages: int | None = None,
) -> AsyncIterator[T]: ...

class ResourcePool[R]:
    def __init__(
        self,
        factory: Callable[[], Awaitable[R]],
        close: Callable[[R], Awaitable[None]],
        *,
        max_size: int,
    ) -> None: ...
    @property
    def created(self) -> int: ...
    def lease(self) -> AsyncContextManager[R]: ...   # @asynccontextmanager
    async def aclose(self) -> None: ...
```

`paginate`:
- Calls `fetch_page(None)` first, then follows `next_cursor` until it is `None`. Yields items in
  order. Pages are fetched lazily: a page is requested only when its first item is needed.
- An empty page that still has a `next_cursor` is followed.
- `max_pages` (when given) stops after that many fetched pages. `max_pages < 1` raises `ValueError`
  on the first iteration.
- Errors from `fetch_page` propagate to the consumer after the items already yielded.

`ResourcePool`:
- `lease()` yields a resource. It is returned to the pool when the block exits, even on exception.
- Idle resources are reused before the factory is called. `created` counts successful factory calls.
- At most `max_size` resources are leased at once. Extra `lease()` calls wait until one is released.
- If `factory` raises, the error propagates to the leasing caller and its slot is freed.
- `aclose()` closes all idle resources and rejects later leases with `RuntimeError("pool is closed")`.
  A resource still leased at close time is closed when its lease ends. A second `aclose()` is a
  no-op.
- A waiting `lease()` that is cancelled must not leak its slot.
- `max_size < 1` raises `ValueError`.

## Part 7 · Resilience: retries, circuit breaker, token bucket

```python
async def retry_async[T](
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    base_delay: float = 0.1,
    max_delay: float = 5.0,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> T: ...

class CircuitState(Enum):   # CLOSED = "closed", OPEN = "open", HALF_OPEN = "half_open"
    ...

class CircuitOpenError(Exception): ...

class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int,
        reset_timeout: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None: ...
    @property
    def state(self) -> CircuitState: ...
    async def call[T](self, operation: Callable[[], Awaitable[T]]) -> T: ...

class TokenBucket:
    def __init__(
        self,
        *,
        rate: float,
        capacity: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None: ...
    def try_acquire(self, tokens: float = 1.0) -> bool: ...
    async def acquire(self, tokens: float = 1.0) -> None: ...
```

`retry_async`:
- Calls `operation` up to `attempts` times. `attempts < 1` raises `ValueError`.
- Before retry *n* (n = 1 for the first retry), waits `min(max_delay, base_delay * 2 ** (n - 1))`
  via `sleep`. No waiting after the final attempt.
- Exceptions outside `retry_on` propagate immediately. When attempts are exhausted, the last
  exception propagates unchanged.
- Returns the first successful result.

`CircuitBreaker`:
- Starts `CLOSED`. Counts consecutive failures (any `Exception`); a success resets the count.
- After `failure_threshold` consecutive failures it goes `OPEN` and records the time from `clock()`.
- While `OPEN`, `call` raises `CircuitOpenError` without invoking the operation.
- Once `reset_timeout` seconds have passed, `state` reads `HALF_OPEN`. Exactly one trial call is
  admitted; while that trial is in flight, other calls raise `CircuitOpenError`.
- Trial success returns the breaker to `CLOSED`. Trial failure reopens it with a fresh timer.
- A cancelled trial counts as neither success nor failure: the breaker stays `HALF_OPEN` and the
  next call may try again.
- `failure_threshold < 1` or `reset_timeout < 0` raises `ValueError`.

`TokenBucket`:
- Starts full (`capacity` tokens). Refills continuously at `rate` tokens per second, capped at
  `capacity`. Refill is computed from `clock()`; no timers are used.
- `try_acquire(tokens)` takes tokens if available and returns `True`, otherwise `False`, never
  blocking.
- `acquire(tokens)` waits via `sleep` for exactly the deficit (`(tokens - available) / rate`),
  then takes the tokens.
- `tokens` must be in `(0, capacity]`; otherwise `ValueError`. `rate <= 0` or `capacity <= 0`
  raises `ValueError` at construction.

---

## Concepts exercised

- Coroutines vs tasks: calling a coroutine function does nothing until awaited or scheduled.
- `gather(return_exceptions=True)` vs `TaskGroup` (`ExceptionGroup`, sibling cancellation).
- `asyncio.timeout`, `Timeout.expired()`, and `TimeoutError` vs `CancelledError`.
- Cancellation semantics: `finally` cleanup, awaiting after cancel, `shield`, and detached tasks.
- `asyncio.Semaphore` for bounded concurrency and high-water marks.
- `asyncio.to_thread` for blocking calls.
- `asyncio.Queue` worker pools: `task_done`/`join`, cancelling workers, graceful vs forced shutdown.
- Async generators, `contextlib.aclosing`, `@asynccontextmanager`.
- `Event`, `Barrier`, `Future` as synchronization and result handoff.
- Retry backoff, circuit breaker state machines, token-bucket math with injected clocks and sleeps.

## Run

```
uv run pytest exercises/15_asyncio
uv run pytest exercises/15_asyncio -k part5
uv run pytest exercises/15_asyncio --solutions
```

## Stretch (untested)

- Make `WorkerPool` accept a `max_queue` and apply backpressure in `submit`.
- Give `shielded_commit` a grace period after cancel, then log a warning if the operation is still running.
- Add jitter to `retry_async` and check that the schedule stays within bounds.
- Make `TokenBucket.acquire` fair across waiters (FIFO) without a busy loop.
- Replace the circuit breaker's lock-free trial logic with an `asyncio.Lock` and compare.
