"""Reference implementations for the threads & processes exercises."""

from __future__ import annotations

import math
import queue
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Self

# --------------------------------------------------------------------------- Part 1


@dataclass(frozen=True, slots=True)
class LinkResult:
    """Outcome of checking one URL."""

    url: str
    status: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 400


def check_links(
    urls: Iterable[str],
    *,
    fetch: Callable[[Any, str], int],
    session_factory: Callable[[], Any] | None = None,
    max_workers: int = 8,
) -> list[LinkResult]:
    """Check URLs concurrently; results mirror the input order.

    Each distinct URL is fetched once. Exceptions are captured per URL. Each
    worker thread builds its session once, lazily, and reuses it: sessions are
    never shared between threads.
    """
    if max_workers < 1:
        raise ValueError(f"max_workers must be >= 1, got {max_workers}")
    targets = list(urls)
    distinct = list(dict.fromkeys(targets))
    if not distinct:
        return []

    local = threading.local()

    def session() -> Any:
        if not hasattr(local, "session"):
            local.session = session_factory() if session_factory is not None else None
        return local.session

    def check(url: str) -> LinkResult:
        try:
            status = fetch(session(), url)
        except Exception as exc:
            return LinkResult(url=url, error=f"{type(exc).__name__}: {exc}")
        return LinkResult(url=url, status=status)

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="linkcheck") as pool:
        by_url = dict(zip(distinct, pool.map(check, distinct), strict=True))
    return [by_url[url] for url in targets]


# --------------------------------------------------------------------------- Part 2


class BoundedDownloader:
    """Fetches URLs with a process-wide cap on concurrent fetches."""

    def __init__(self, fetch: Callable[[str], bytes], max_concurrent: int) -> None:
        if max_concurrent < 1:
            raise ValueError(f"max_concurrent must be >= 1, got {max_concurrent}")
        self._fetch = fetch
        self._slots = threading.Semaphore(max_concurrent)
        self._lock = threading.Lock()
        self._in_flight = 0
        self._peak = 0

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    @property
    def peak_concurrency(self) -> int:
        with self._lock:
            return self._peak

    def download(self, url: str) -> bytes:
        with self._slots:
            with self._lock:
                self._in_flight += 1
                self._peak = max(self._peak, self._in_flight)
            try:
                return self._fetch(url)
            finally:
                with self._lock:
                    self._in_flight -= 1

    def download_all(self, urls: Sequence[str]) -> list[bytes]:
        if not urls:
            return []
        with ThreadPoolExecutor(max_workers=min(len(urls), 16), thread_name_prefix="download") as pool:
            futures = [pool.submit(self.download, url) for url in urls]
            # Iterating in input order re-raises the earliest failure; the
            # executor's exit waits for every started fetch to finish.
            return [future.result() for future in futures]


class ResourcePool[T]:
    """Bounded pool of lazily created, reusable objects."""

    def __init__(self, factory: Callable[[], T], max_size: int) -> None:
        if max_size < 1:
            raise ValueError(f"max_size must be >= 1, got {max_size}")
        self._factory = factory
        self._max_size = max_size
        self._cond = threading.Condition()
        self._idle: list[T] = []
        self._checked_out: list[T] = []
        self._reserved = 0  # created + currently being created
        self._created = 0

    @property
    def created(self) -> int:
        with self._cond:
            return self._created

    def acquire(self, timeout: float | None = None) -> T:
        with self._cond:
            ready = self._cond.wait_for(
                lambda: bool(self._idle) or self._reserved < self._max_size, timeout
            )
            if not ready:
                raise TimeoutError(f"no resource free after {timeout}s")
            if self._idle:
                item = self._idle.pop()
                self._checked_out.append(item)
                return item
            self._reserved += 1  # claim a capacity slot before leaving the lock

        try:
            item = self._factory()
        except BaseException:
            with self._cond:
                self._reserved -= 1
                self._cond.notify()
            raise

        with self._cond:
            self._created += 1
            self._checked_out.append(item)
        return item

    def release(self, item: T) -> None:
        with self._cond:
            for index, held in enumerate(self._checked_out):
                if held is item:
                    del self._checked_out[index]
                    self._idle.append(item)
                    self._cond.notify()
                    return
        raise ValueError("item was not checked out from this pool")

    @contextmanager
    def lease(self, timeout: float | None = None) -> Iterator[T]:
        item = self.acquire(timeout)
        try:
            yield item
        finally:
            self.release(item)


# --------------------------------------------------------------------------- Part 3


class Counter:
    """Named, thread-safe, monotonically increasing counters."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, int] = {}

    def increment(self, name: str, by: int = 1) -> int:
        if by < 0:
            raise ValueError(f"by must be >= 0, got {by}")
        with self._lock:
            value = self._values.get(name, 0) + by
            self._values[name] = value
            return value

    def get(self, name: str) -> int:
        with self._lock:
            return self._values.get(name, 0)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._values)


class InsufficientFunds(Exception):
    """Raised when a withdrawal exceeds the available balance."""


def _require_positive(amount: int) -> None:
    if amount <= 0:
        raise ValueError(f"amount must be > 0, got {amount}")


class Account:
    """A balance guarded by a reentrant lock."""

    def __init__(self, account_id: str, balance: int = 0) -> None:
        if balance < 0:
            raise ValueError(f"balance must be >= 0, got {balance}")
        self._id = account_id
        self._balance = balance
        # RLock: transfer() holds this lock and calls withdraw()/deposit(),
        # which acquire it again on the same thread.
        self._lock = threading.RLock()

    @property
    def account_id(self) -> str:
        return self._id

    @property
    def balance(self) -> int:
        with self._lock:
            return self._balance

    def deposit(self, amount: int) -> None:
        _require_positive(amount)
        with self._lock:
            self._balance += amount

    def withdraw(self, amount: int) -> None:
        _require_positive(amount)
        with self._lock:
            if amount > self._balance:
                raise InsufficientFunds(f"{self._id}: balance {self._balance} < {amount}")
            self._balance -= amount


def transfer(src: Account, dst: Account, amount: int) -> None:
    """Move ``amount`` from ``src`` to ``dst`` atomically and deadlock-free."""
    _require_positive(amount)
    if src is dst:
        raise ValueError("cannot transfer to the same account")
    # One global order for the two locks, independent of transfer direction.
    first, second = sorted((src, dst), key=lambda acc: (acc.account_id, id(acc)))
    with first._lock, second._lock:
        src.withdraw(amount)
        dst.deposit(amount)


# --------------------------------------------------------------------------- Part 4

_MISSING: Any = object()


class BoundedCache[K, V]:
    """Thread-safe LRU cache with a fixed capacity."""

    def __init__(self, max_size: int) -> None:
        if max_size < 1:
            raise ValueError(f"max_size must be >= 1, got {max_size}")
        self._max_size = max_size
        self._data: OrderedDict[K, V] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: K, default: V | None = None) -> V | None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                self._hits += 1
                return self._data[key]
            self._misses += 1
            return default

    def put(self, key: K, value: V) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self._max_size:
                self._data.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._data

    @property
    def hits(self) -> int:
        with self._lock:
            return self._hits

    @property
    def misses(self) -> int:
        with self._lock:
            return self._misses


class _Flight[V]:
    """An in-progress load that other callers can wait on."""

    __slots__ = ("done", "value", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: V | None = None
        self.error: BaseException | None = None


class SingleFlightCache[K, V]:
    """Coalesces concurrent loads of the same key into one ``loader`` call."""

    def __init__(self, max_size: int = 128) -> None:
        self._values = BoundedCache[K, V](max_size)
        self._lock = threading.Lock()  # guards _values, _flights, _loads
        self._flights: dict[K, _Flight[V]] = {}
        self._loads = 0

    @property
    def load_count(self) -> int:
        with self._lock:
            return self._loads

    def get_or_load(self, key: K, loader: Callable[[K], V]) -> V:
        with self._lock:
            # _values is only mutated under self._lock, so membership is stable here.
            if key in self._values:
                return self._values.get(key)  # type: ignore[return-value]
            flight = self._flights.get(key)
            leader = flight is None
            if flight is None:
                flight = _Flight[V]()
                self._flights[key] = flight
                self._loads += 1

        if not leader:
            flight.done.wait()
            if flight.error is not None:
                raise flight.error
            return flight.value  # type: ignore[return-value]

        # The loader runs without holding any lock.
        try:
            value = loader(key)
        except BaseException as exc:
            flight.error = exc
            with self._lock:
                del self._flights[key]
            flight.done.set()
            raise

        with self._lock:
            self._values.put(key, value)
            del self._flights[key]
        flight.value = value
        flight.done.set()
        return value


# --------------------------------------------------------------------------- Part 5


@dataclass(frozen=True, slots=True)
class Failure[T]:
    item: T
    error: Exception


@dataclass(frozen=True, slots=True)
class GroupResult[T, R]:
    results: list[R]
    failures: list[Failure[T]]


class WorkerGroup[T, R]:
    """Fixed set of worker threads draining a queue; ``close`` drains gracefully."""

    def __init__(self, handler: Callable[[T], R], *, workers: int, queue_size: int = 0) -> None:
        if workers < 1:
            raise ValueError(f"workers must be >= 1, got {workers}")
        self._handler = handler
        self._worker_count = workers
        self._queue: queue.Queue[T] = queue.Queue(maxsize=queue_size)
        self._lock = threading.Lock()
        self._state = "new"  # new -> running -> closing -> closed
        self._threads: list[threading.Thread] = []
        self._results: list[R] = []
        self._failures: list[Failure[T]] = []
        self._result: GroupResult[T, R] | None = None

    def start(self) -> None:
        with self._lock:
            if self._state != "new":
                raise RuntimeError("worker group already started")
            self._state = "running"
            self._threads = [
                threading.Thread(target=self._run, name=f"worker-{i}")
                for i in range(self._worker_count)
            ]
            for thread in self._threads:
                thread.start()

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get()
            except queue.ShutDown:
                return
            try:
                value = self._handler(item)
            except Exception as exc:
                with self._lock:
                    self._failures.append(Failure(item=item, error=exc))
            else:
                with self._lock:
                    self._results.append(value)

    def submit(self, item: T) -> None:
        with self._lock:
            if self._state != "running":
                raise RuntimeError(f"cannot submit to a worker group that is {self._state}")
        try:
            self._queue.put(item)  # blocks when bounded and full
        except queue.ShutDown:
            raise RuntimeError("worker group is closing") from None

    def close(self, timeout: float | None = None) -> GroupResult[T, R]:
        with self._lock:
            if self._state == "new":
                raise RuntimeError("close() called before start()")
            if self._state == "closed":
                assert self._result is not None
                return self._result
            if self._state == "running":
                self._state = "closing"
                # Queued items remain gettable; workers exit once it is empty.
                self._queue.shutdown(immediate=False)
            threads = list(self._threads)

        deadline = None if timeout is None else time.monotonic() + timeout
        for thread in threads:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            thread.join(remaining)
            if thread.is_alive():
                raise TimeoutError(f"{thread.name} still busy after {timeout}s")

        with self._lock:
            self._state = "closed"
            self._result = GroupResult(results=list(self._results), failures=list(self._failures))
            return self._result

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def run_pipeline(
    items: Iterable[Any],
    stages: Sequence[Callable[[Any], Any]],
    *,
    workers_per_stage: int = 2,
    queue_size: int = 4,
) -> list[Any]:
    """Run items through chained stages concurrently; return results in input order."""
    if workers_per_stage < 1:
        raise ValueError(f"workers_per_stage must be >= 1, got {workers_per_stage}")
    if queue_size < 1:
        raise ValueError(f"queue_size must be >= 1, got {queue_size}")
    if not stages:
        return list(items)

    depth = len(stages)
    # queues[i] feeds stage i; queues[depth] collects the final results.
    queues: list[queue.Queue[tuple[int, Any]]] = [
        queue.Queue(maxsize=queue_size) for _ in range(depth + 1)
    ]
    lock = threading.Lock()
    first_error: list[BaseException] = []
    live_workers = [workers_per_stage] * depth

    def abort(exc: BaseException) -> None:
        with lock:
            if not first_error:
                first_error.append(exc)
        # Immediate shutdown wakes every blocked put/get so all threads exit.
        for q in queues:
            q.shutdown(immediate=True)

    def feed() -> None:
        try:
            for seq, item in enumerate(items):
                queues[0].put((seq, item))
        except queue.ShutDown:
            return  # aborting
        except BaseException as exc:
            abort(exc)
            return
        queues[0].shutdown()

    def work(stage: int) -> None:
        source, sink = queues[stage], queues[stage + 1]
        handler = stages[stage]
        try:
            while True:
                try:
                    seq, value = source.get()
                except queue.ShutDown:
                    break
                sink.put((seq, handler(value)))
        except queue.ShutDown:
            return  # aborting
        except BaseException as exc:
            abort(exc)
            return
        # Last worker of this stage to finish closes the next stage's input.
        with lock:
            live_workers[stage] -= 1
            last = live_workers[stage] == 0
        if last:
            sink.shutdown()

    threads = [threading.Thread(target=feed, name="pipeline-feed")]
    for stage in range(depth):
        for worker in range(workers_per_stage):
            threads.append(threading.Thread(target=work, args=(stage,), name=f"stage{stage}-{worker}"))
    for thread in threads:
        thread.start()

    collected: dict[int, Any] = {}
    while True:
        try:
            seq, value = queues[depth].get()
        except queue.ShutDown:
            break
        collected[seq] = value

    for thread in threads:
        thread.join()
    if first_error:
        raise first_error[0]
    return [collected[seq] for seq in range(len(collected))]


# --------------------------------------------------------------------------- Part 6


class Scheduler:
    """Runs named periodic jobs on dedicated threads with a clean, joined stop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._jobs: list[tuple[str, float, Callable[[], None]]] = []
        self._threads: list[threading.Thread] = []
        self._errors: list[tuple[str, BaseException]] = []
        self._state = "new"  # new -> running -> stopped

    def add(self, name: str, interval: float, job: Callable[[], None]) -> None:
        with self._lock:
            if self._state != "new":
                raise RuntimeError("cannot add jobs after start()")
            if interval <= 0:
                raise ValueError(f"interval must be > 0, got {interval}")
            if any(existing == name for existing, _, _ in self._jobs):
                raise ValueError(f"duplicate job name {name!r}")
            self._jobs.append((name, interval, job))

    def start(self) -> None:
        with self._lock:
            if self._state != "new":
                raise RuntimeError("scheduler already started or stopped")
            self._state = "running"
            for name, interval, job in self._jobs:
                thread = threading.Thread(
                    target=self._loop, args=(name, interval, job), name=f"job-{name}"
                )
                self._threads.append(thread)
                thread.start()

    def _loop(self, name: str, interval: float, job: Callable[[], None]) -> None:
        while not self._stop_event.is_set():
            try:
                job()
            except Exception as exc:
                with self._lock:
                    self._errors.append((name, exc))
            # Event.wait doubles as an interruptible sleep: stop() wakes it.
            if self._stop_event.wait(interval):
                return

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            was_new = self._state == "new"
            self._state = "stopped"
            threads = list(self._threads)
        if was_new:
            return
        self._stop_event.set()
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                raise TimeoutError(f"{thread.name} still running after {timeout}s")

    @property
    def errors(self) -> list[tuple[str, BaseException]]:
        with self._lock:
            return list(self._errors)

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


# --------------------------------------------------------------------------- Part 7


def is_prime(n: int) -> bool:
    if n < 2:
        return False
    if n < 4:
        return True
    if n % 2 == 0:
        return False
    return all(n % divisor for divisor in range(3, math.isqrt(n) + 1, 2))


def count_primes(candidates: range) -> int:
    return sum(1 for n in candidates if is_prime(n))


def chunk_ranges(start: int, stop: int, parts: int) -> list[range]:
    if parts < 1:
        raise ValueError(f"parts must be >= 1, got {parts}")
    total = max(0, stop - start)
    count = min(parts, total)
    if count == 0:
        return []
    base, extra = divmod(total, count)
    chunks: list[range] = []
    lo = start
    for index in range(count):
        size = base + (1 if index < extra else 0)
        chunks.append(range(lo, lo + size))
        lo += size
    return chunks


def parallel_map[T, R](fn: Callable[[T], R], items: Sequence[T], *, workers: int = 2) -> list[R]:
    if workers < 1:
        raise ValueError(f"workers must be >= 1, got {workers}")
    if not items:
        return []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))
