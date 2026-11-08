"""Reference solution: asyncio fan-out, cancellation, bounded work, pools, resilience."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Self

log = logging.getLogger("pyref.commit")


# ---------------------------------------------------------------------------
# Part 1: fan-out with gather(return_exceptions=True) and per-call timeouts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceResult:
    """Outcome of one source call. ``value`` is set iff ``ok``."""

    name: str
    ok: bool
    value: object = None
    error: str | None = None
    timed_out: bool = False


async def aggregate(
    sources: Mapping[str, Callable[[], Awaitable[object]]],
    *,
    per_call_timeout: float,
) -> dict[str, SourceResult]:
    """Call every source concurrently; never let one failure hide the others."""
    if per_call_timeout <= 0:
        raise ValueError("per_call_timeout must be positive")

    async def call_one(name: str, factory: Callable[[], Awaitable[object]]) -> SourceResult:
        try:
            async with asyncio.timeout(per_call_timeout) as deadline:
                value = await factory()
        except Exception as exc:
            # ``deadline.expired()`` distinguishes our timeout from a TimeoutError
            # the source raised on its own.
            if deadline.expired():
                return SourceResult(name, ok=False, error="TimeoutError: per-call timeout", timed_out=True)
            return SourceResult(name, ok=False, error=f"{type(exc).__name__}: {exc}")
        return SourceResult(name, ok=True, value=value)

    outcomes = await asyncio.gather(
        *(call_one(name, factory) for name, factory in sources.items()),
        return_exceptions=True,
    )
    results: dict[str, SourceResult] = {}
    for name, outcome in zip(sources, outcomes, strict=True):
        if isinstance(outcome, SourceResult):
            results[name] = outcome
        else:  # a BaseException escaped a source (e.g. its own CancelledError)
            results[name] = SourceResult(name, ok=False, error=f"{type(outcome).__name__}: {outcome}")
    return results


# ---------------------------------------------------------------------------
# Part 2: structured concurrency with TaskGroup
# ---------------------------------------------------------------------------


async def fetch_all_strict[T](sources: Mapping[str, Callable[[], Awaitable[T]]]) -> dict[str, T]:
    """All-or-nothing fan-out. Any failure cancels siblings and raises ExceptionGroup."""
    tasks: dict[str, asyncio.Task[T]] = {}
    async with asyncio.TaskGroup() as tg:
        for name, factory in sources.items():
            tasks[name] = tg.create_task(factory())
    return {name: task.result() for name, task in tasks.items()}


# ---------------------------------------------------------------------------
# Part 3: cancellation, timeouts, cleanup, and shielding
# ---------------------------------------------------------------------------


async def run_with_cleanup[T](
    body: Callable[[], Awaitable[T]],
    cleanup: Callable[[], Awaitable[None]],
    *,
    timeout: float,
) -> T:
    """Run ``body`` under a deadline; ``cleanup`` always runs, even when cancelled."""
    try:
        async with asyncio.timeout(timeout):
            return await body()
    finally:
        # Awaiting here is safe: asyncio.timeout consumed its own cancellation
        # before this line, and an external cancel only arrives once.
        await cleanup()


def _report_abandoned_failure(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("shielded operation failed after its caller was cancelled", exc_info=exc)


async def shielded_commit[T](operation: Callable[[], Awaitable[T]]) -> T:
    """Run ``operation`` to completion even if the caller is cancelled.

    The caller still receives CancelledError right away; the operation keeps
    running in its own task and any late failure is logged, not lost.
    """
    task = asyncio.create_task(operation())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(_report_abandoned_failure)
        raise


# ---------------------------------------------------------------------------
# Part 4: bounded concurrency with Semaphore, plus to_thread for blocking IO
# ---------------------------------------------------------------------------


class BoundedFetcher[T, R]:
    """Run an async ``fetch`` over many items with at most ``limit`` in flight."""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self._slots = asyncio.Semaphore(limit)
        self._in_flight = 0
        self._peak = 0

    @property
    def peak_in_flight(self) -> int:
        """Highest number of simultaneous ``fetch`` calls observed so far."""
        return self._peak

    async def run(self, items: Sequence[T], fetch: Callable[[T], Awaitable[R]]) -> list[R]:
        async def one(item: T) -> R:
            async with self._slots:
                self._in_flight += 1
                self._peak = max(self._peak, self._in_flight)
                try:
                    return await fetch(item)
                finally:
                    self._in_flight -= 1

        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(one(item)) for item in items]
        return [task.result() for task in tasks]


async def read_files(paths: Sequence[Path], *, limit: int) -> dict[Path, bytes]:
    """Read files off the event loop with bounded parallelism."""
    fetcher = BoundedFetcher[Path, bytes](limit)
    blobs = await fetcher.run(paths, lambda path: asyncio.to_thread(path.read_bytes))
    return dict(zip(paths, blobs, strict=True))


# ---------------------------------------------------------------------------
# Part 5: queue-backed worker pool with graceful shutdown
# ---------------------------------------------------------------------------

_CLOSED_BEFORE_DONE = "pool closed before the item completed"


class WorkerPool[T, R]:
    """Fixed set of worker tasks consuming a queue; ``submit`` awaits the result."""

    def __init__(self, handler: Callable[[T], Awaitable[R]], *, workers: int) -> None:
        if workers < 1:
            raise ValueError("workers must be >= 1")
        self._handler = handler
        self._worker_count = workers
        self._queue: asyncio.Queue[tuple[T, asyncio.Future[R]]] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []
        self._closing = False
        self._processed = 0

    @property
    def processed(self) -> int:
        """Items the handler completed successfully."""
        return self._processed

    async def __aenter__(self) -> Self:
        if self._tasks:
            raise RuntimeError("pool already started")
        self._tasks = [asyncio.create_task(self._worker(), name=f"pool-worker-{i}") for i in range(self._worker_count)]
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose(drain=True)

    async def submit(self, item: T) -> R:
        if not self._tasks:
            raise RuntimeError("pool is not running")
        if self._closing:
            raise RuntimeError("pool is closed")
        future: asyncio.Future[R] = asyncio.get_running_loop().create_future()
        self._queue.put_nowait((item, future))
        # If the submitter is cancelled, this cancels ``future`` and the worker skips it.
        return await future

    async def aclose(self, *, drain: bool = True) -> None:
        """Stop accepting work. ``drain=True`` finishes queued items first; ``False`` fails them."""
        if self._closing:
            return
        self._closing = True
        if drain:
            await self._queue.join()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        while not self._queue.empty():
            _, future = self._queue.get_nowait()
            if not future.done():
                future.set_exception(RuntimeError(_CLOSED_BEFORE_DONE))
            self._queue.task_done()

    async def _worker(self) -> None:
        while True:
            item, future = await self._queue.get()
            try:
                if future.done():  # submitter gave up while the item was queued
                    continue
                try:
                    result = await self._handler(item)
                except asyncio.CancelledError:
                    if not future.done():
                        future.set_exception(RuntimeError(_CLOSED_BEFORE_DONE))
                    raise
                except Exception as exc:
                    if not future.done():
                        future.set_exception(exc)
                else:
                    self._processed += 1
                    if not future.done():
                        future.set_result(result)
            finally:
                self._queue.task_done()


# ---------------------------------------------------------------------------
# Part 6: async generators and async context managers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: Sequence[T]
    next_cursor: str | None


async def paginate[T](
    fetch_page: Callable[[str | None], Awaitable[Page[T]]],
    *,
    max_pages: int | None = None,
) -> AsyncIterator[T]:
    """Lazily yield items across cursor-linked pages."""
    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages must be >= 1 when given")
    cursor: str | None = None
    fetched = 0
    while True:
        page = await fetch_page(cursor)
        fetched += 1
        for item in page.items:
            yield item
        cursor = page.next_cursor
        if cursor is None or (max_pages is not None and fetched >= max_pages):
            return


class ResourcePool[R]:
    """Bounded pool of reusable async resources handed out via ``lease()``."""

    def __init__(
        self,
        factory: Callable[[], Awaitable[R]],
        close: Callable[[R], Awaitable[None]],
        *,
        max_size: int,
    ) -> None:
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self._factory = factory
        self._close = close
        self._slots = asyncio.Semaphore(max_size)
        self._idle: list[R] = []
        self._created = 0
        self._closed = False

    @property
    def created(self) -> int:
        """Number of successful factory calls."""
        return self._created

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[R]:
        resource = await self._acquire()
        try:
            yield resource
        finally:
            await self._release(resource)

    async def aclose(self) -> None:
        """Reject new leases and close idle resources. Leased ones close on release."""
        if self._closed:
            return
        self._closed = True
        idle, self._idle = self._idle, []
        for resource in idle:
            await self._close(resource)

    async def _acquire(self) -> R:
        if self._closed:
            raise RuntimeError("pool is closed")
        await self._slots.acquire()
        try:
            if self._closed:
                raise RuntimeError("pool is closed")
            if self._idle:
                return self._idle.pop()
            resource = await self._factory()
        except BaseException:
            self._slots.release()
            raise
        self._created += 1
        return resource

    async def _release(self, resource: R) -> None:
        try:
            if self._closed:
                await self._close(resource)
            else:
                self._idle.append(resource)
        finally:
            self._slots.release()


# ---------------------------------------------------------------------------
# Part 7: retries with backoff, circuit breaker, token bucket
# ---------------------------------------------------------------------------


async def retry_async[T](
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    base_delay: float = 0.1,
    max_delay: float = 5.0,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> T:
    """Call ``operation`` up to ``attempts`` times with capped exponential backoff."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    for attempt in range(1, attempts):
        try:
            return await operation()
        except retry_on:
            await sleep(min(max_delay, base_delay * 2 ** (attempt - 1)))
    return await operation()  # final attempt: let its exception propagate


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised instead of calling the operation while the breaker rejects traffic."""


class CircuitBreaker:
    """Closed -> open after N consecutive failures; half-open trial after a cooldown."""

    def __init__(
        self,
        *,
        failure_threshold: int,
        reset_timeout: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if reset_timeout < 0:
            raise ValueError("reset_timeout must be >= 0")
        self._threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def state(self) -> CircuitState:
        if self._opened_at is None:
            return CircuitState.CLOSED
        if self._clock() - self._opened_at >= self._reset_timeout:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    async def call[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        state = self.state
        if state is CircuitState.OPEN or (state is CircuitState.HALF_OPEN and self._probing):
            raise CircuitOpenError(f"circuit is {state.value}")
        trial = state is CircuitState.HALF_OPEN
        if trial:
            self._probing = True
        try:
            result = await operation()
        except Exception:
            self._on_failure(trial)
            raise
        else:
            self._on_success(trial)
            return result
        finally:
            # Covers cancellation too: a cancelled probe frees the slot without
            # counting as a failure.
            if trial:
                self._probing = False

    def _on_failure(self, trial: bool) -> None:
        if trial:
            self._opened_at = self._clock()
            return
        if self._opened_at is None:
            self._failures += 1
            if self._failures >= self._threshold:
                self._opened_at = self._clock()

    def _on_success(self, trial: bool) -> None:
        # A late success from a call admitted before the breaker opened must not close it.
        if trial or self._opened_at is None:
            self._failures = 0
            self._opened_at = None


class TokenBucket:
    """Token-bucket rate limiter: refills at ``rate`` tokens/s up to ``capacity``."""

    def __init__(
        self,
        *,
        rate: float,
        capacity: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        if rate <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self._rate = rate
        self._capacity = capacity
        self._clock = clock
        self._sleep = sleep
        self._tokens = capacity
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
        self._last = now

    def _check_tokens(self, tokens: float) -> None:
        if not 0 < tokens <= self._capacity:
            raise ValueError(f"tokens must be in (0, capacity={self._capacity}]")

    def try_acquire(self, tokens: float = 1.0) -> bool:
        self._check_tokens(tokens)
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False

    async def acquire(self, tokens: float = 1.0) -> None:
        self._check_tokens(tokens)
        while not self.try_acquire(tokens):
            await self._sleep((tokens - self._tokens) / self._rate)
