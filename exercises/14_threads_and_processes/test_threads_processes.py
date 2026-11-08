"""Threads & processes exercises: link checking, bounded concurrency, shared state,
caches, queues and pipelines, scheduling, and process pools."""

from __future__ import annotations

import math
import random
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType
from typing import Any

import pytest

JOIN = 10.0  # generous bound: a deadlock becomes a failure, not a stuck run


class Recorder:
    """Thread-safe append-only log used to observe concurrent calls."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[Any] = []

    def add(self, item: Any) -> None:
        with self._lock:
            self._items.append(item)

    @property
    def items(self) -> list[Any]:
        with self._lock:
            return list(self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def run_concurrently(*targets: Callable[[], object]) -> None:
    """Run each target on its own thread. Fail on exception or deadlock; never hang."""
    errors = Recorder()

    def runner(fn: Callable[[], object]) -> Callable[[], None]:
        def run() -> None:
            try:
                fn()
            except BaseException as exc:  # surface worker failures in the test thread
                errors.add(exc)

        return run

    threads = [
        threading.Thread(target=runner(fn), name=f"test-worker-{i}")
        for i, fn in enumerate(targets)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(JOIN)
        assert not thread.is_alive(), f"{thread.name} still running after {JOIN}s (deadlock?)"
    if errors.items:
        raise errors.items[0]


@pytest.fixture(autouse=True)
def no_thread_leaks() -> Iterator[None]:
    """Every thread a test starts must be finished when the test ends."""
    before = set(threading.enumerate())
    yield
    leaked = [t for t in threading.enumerate() if t not in before and t.is_alive()]
    for thread in leaked:
        thread.join(5.0)
    still_alive = [t.name for t in leaked if t.is_alive()]
    assert not still_alive, f"threads leaked by test: {still_alive}"


# ---------------------------------------------------------------- Part 1


class TestPart1LinkChecker:
    def test_results_follow_input_order_and_fetch_each_url_once(self, main: ModuleType) -> None:
        statuses = {"https://a": 200, "https://b": 404, "https://c": 301}
        calls = Recorder()

        def fetch(_session: Any, url: str) -> int:
            calls.add(url)
            return statuses[url]

        urls = ["https://a", "https://b", "https://a", "https://c"]
        results = main.check_links(urls, fetch=fetch, max_workers=4)

        assert [r.url for r in results] == urls, "results must mirror the input order"
        assert [r.status for r in results] == [200, 404, 200, 301]
        assert sorted(calls.items) == ["https://a", "https://b", "https://c"], (
            "each distinct URL should be fetched exactly once"
        )

    def test_fetch_errors_are_captured_per_url(self, main: ModuleType) -> None:
        def fetch(_session: Any, url: str) -> int:
            if url == "https://slow":
                raise TimeoutError("slow")
            return 200

        results = main.check_links(["https://ok", "https://slow"], fetch=fetch, max_workers=2)

        ok, slow = results
        assert ok.ok and ok.error is None
        assert slow.status is None
        assert slow.error == "TimeoutError: slow", "error must be 'ExcType: message'"
        assert not slow.ok

    def test_ok_property_accepts_2xx_and_3xx_only(self, main: ModuleType) -> None:
        assert main.LinkResult(url="u", status=200).ok, "200 is ok"
        assert main.LinkResult(url="u", status=301).ok, "3xx redirects count as ok"
        assert not main.LinkResult(url="u", status=404).ok, "404 is not ok"
        assert not main.LinkResult(url="u", status=500).ok, "500 is not ok"
        assert not main.LinkResult(url="u", error="boom").ok, "no status means not ok"

    def test_urls_are_fetched_concurrently(self, main: ModuleType) -> None:
        # Three fetches must all be in flight at once to pass the barrier.
        barrier = threading.Barrier(3, timeout=JOIN)

        def fetch(_session: Any, _url: str) -> int:
            barrier.wait()
            return 200

        results = main.check_links(["u1", "u2", "u3"], fetch=fetch, max_workers=3)

        assert all(r.ok for r in results), (
            f"fetches did not overlap; results: {[(r.url, r.error) for r in results]}"
        )

    def test_empty_input_and_invalid_worker_count(self, main: ModuleType) -> None:
        def fetch(_session: Any, _url: str) -> int:
            return 200

        assert main.check_links([], fetch=fetch) == []
        with pytest.raises(ValueError):
            main.check_links(["u"], fetch=fetch, max_workers=0)

    def test_session_is_created_once_per_worker_thread(self, main: ModuleType) -> None:
        made = Recorder()
        uses = Recorder()

        def factory() -> object:
            session = object()
            made.add(session)
            return session

        def fetch(session: Any, _url: str) -> int:
            uses.add((id(session), threading.get_ident()))
            return 200

        urls = [f"https://site/{i}" for i in range(30)]
        results = main.check_links(urls, fetch=fetch, session_factory=factory, max_workers=3)

        assert all(r.ok for r in results)
        assert 1 <= len(made) <= 3, f"factory must run at most once per worker, ran {len(made)}x"
        threads_by_session: dict[int, set[int]] = {}
        for session_id, thread_id in uses.items:
            threads_by_session.setdefault(session_id, set()).add(thread_id)
        assert all(len(threads) == 1 for threads in threads_by_session.values()), (
            "a session must only ever be used by the thread that created it"
        )


# ---------------------------------------------------------------- Part 2


class TestPart2BoundedConcurrency:
    def test_download_all_returns_bytes_in_input_order(self, main: ModuleType) -> None:
        downloader = main.BoundedDownloader(lambda url: url.encode(), max_concurrent=4)

        assert downloader.download_all(["a", "b", "c"]) == [b"a", b"b", b"c"]
        assert downloader.download_all([]) == []
        assert downloader.download("x") == b"x"

    def test_peak_concurrency_reaches_but_never_exceeds_the_limit(self, main: ModuleType) -> None:
        barrier = threading.Barrier(3, timeout=JOIN)

        def fetch(_url: str) -> bytes:
            barrier.wait()  # forces exactly 3 fetches to be in flight together
            return b""

        downloader = main.BoundedDownloader(fetch, max_concurrent=3)
        downloader.download_all([f"u{i}" for i in range(9)])

        assert downloader.peak_concurrency == 3, "limit should be reached but not exceeded"
        assert downloader.in_flight == 0, "nothing should still be in flight"

    def test_limit_is_shared_across_concurrent_callers(self, main: ModuleType) -> None:
        barrier = threading.Barrier(3, timeout=JOIN)

        def fetch(url: str) -> bytes:
            barrier.wait()
            return url.encode()

        downloader = main.BoundedDownloader(fetch, max_concurrent=3)
        batch_a = [f"a{i}" for i in range(6)]
        batch_b = [f"b{i}" for i in range(6)]
        out = Recorder()
        run_concurrently(
            lambda: out.add(downloader.download_all(batch_a)),
            lambda: out.add(downloader.download_all(batch_b)),
        )

        assert downloader.peak_concurrency == 3, "two callers must share the same 3 slots"
        assert sorted(out.items) == sorted([[u.encode() for u in batch_a], [u.encode() for u in batch_b]])

    def test_download_all_raises_earliest_failure_after_all_fetches_finish(self, main: ModuleType) -> None:
        attempted = Recorder()

        def fetch(url: str) -> bytes:
            attempted.add(url)
            if url in ("x", "y"):
                raise ValueError(url)
            return b""

        downloader = main.BoundedDownloader(fetch, max_concurrent=2)
        with pytest.raises(ValueError, match="^x$"):
            downloader.download_all(["ok1", "x", "ok2", "y"])

        assert sorted(attempted.items) == ["ok1", "ok2", "x", "y"], (
            "every URL should be attempted before the error is re-raised"
        )

    def test_invalid_limit_is_rejected(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.BoundedDownloader(lambda url: b"", max_concurrent=0)

    def test_pool_reuses_idle_items(self, main: ModuleType) -> None:
        pool = main.ResourcePool(factory=object, max_size=2)

        with pool.lease() as first:
            pass
        with pool.lease() as second:
            assert second is first, "an idle item should be reused"
        assert pool.created == 1

    def test_pool_bounds_checked_out_items(self, main: ModuleType) -> None:
        pool = main.ResourcePool(factory=object, max_size=2)
        a = pool.acquire()
        b = pool.acquire()
        assert a is not b
        with pytest.raises(TimeoutError):
            pool.acquire(timeout=0)

        pool.release(a)
        assert pool.acquire(timeout=0) is a
        assert pool.created == 2

    def test_blocked_acquire_wakes_when_item_is_released(self, main: ModuleType) -> None:
        pool = main.ResourcePool(factory=object, max_size=1)
        held = pool.acquire()
        with ThreadPoolExecutor(max_workers=1) as executor:
            waiter = executor.submit(pool.acquire)
            pool.release(held)
            got = waiter.result(timeout=JOIN)
        assert got is held

    def test_release_rejects_foreign_and_double_release(self, main: ModuleType) -> None:
        pool = main.ResourcePool(factory=object, max_size=1)
        item = pool.acquire()
        pool.release(item)
        with pytest.raises(ValueError):
            pool.release(item)
        with pytest.raises(ValueError):
            pool.release(object())

    def test_lease_releases_item_even_when_body_raises(self, main: ModuleType) -> None:
        pool = main.ResourcePool(factory=object, max_size=1)
        with pytest.raises(KeyError):
            with pool.lease(timeout=0):
                raise KeyError("body failed")
        assert pool.acquire(timeout=0) is not None, "the item must be back in the pool"

    def test_factory_failure_does_not_consume_capacity(self, main: ModuleType) -> None:
        attempts = Recorder()

        def flaky_factory() -> object:
            attempts.add(1)
            if len(attempts) == 1:
                raise RuntimeError("cannot connect")
            return object()

        pool = main.ResourcePool(factory=flaky_factory, max_size=1)
        with pytest.raises(RuntimeError, match="cannot connect"):
            pool.acquire(timeout=0)
        assert pool.acquire(timeout=0) is not None, "capacity must be given back"
        assert pool.created == 1


# ---------------------------------------------------------------- Part 3


class TestPart3SharedState:
    def test_increment_returns_new_value_and_defaults_to_zero(self, main: ModuleType) -> None:
        counter = main.Counter()
        assert counter.get("x") == 0
        assert counter.increment("x") == 1
        assert counter.increment("x", by=4) == 5
        assert counter.get("x") == 5

    def test_concurrent_increments_lose_no_updates(self, main: ModuleType) -> None:
        counter = main.Counter()
        start = threading.Barrier(8, timeout=JOIN)

        def worker() -> None:
            start.wait()
            for _ in range(5_000):
                counter.increment("hits")

        run_concurrently(*[worker] * 8)
        assert counter.get("hits") == 40_000, "lost updates: increment is not atomic"

    def test_snapshot_is_an_independent_copy(self, main: ModuleType) -> None:
        counter = main.Counter()
        counter.increment("a", by=2)
        snap = counter.snapshot()
        snap["a"] = 999
        assert counter.get("a") == 2
        with pytest.raises(ValueError):
            counter.increment("a", by=-1)

    def test_transfer_moves_funds(self, main: ModuleType) -> None:
        a = main.Account("a", 100)
        b = main.Account("b", 0)
        main.transfer(a, b, 30)
        assert (a.balance, b.balance) == (70, 30)

    def test_insufficient_funds_leaves_both_accounts_unchanged(self, main: ModuleType) -> None:
        a = main.Account("a", 100)
        b = main.Account("b", 5)
        with pytest.raises(main.InsufficientFunds):
            main.transfer(a, b, 101)
        assert (a.balance, b.balance) == (100, 5)

    def test_invalid_amounts_and_self_transfer_are_rejected(self, main: ModuleType) -> None:
        a = main.Account("a", 10)
        b = main.Account("b", 10)
        with pytest.raises(ValueError):
            main.transfer(a, b, 0)
        with pytest.raises(ValueError):
            main.transfer(a, a, 1)
        with pytest.raises(ValueError):
            a.withdraw(-1)
        with pytest.raises(ValueError):
            b.deposit(0)
        with pytest.raises(ValueError):
            main.Account("neg", -1)

    def test_opposite_direction_transfers_do_not_deadlock(self, main: ModuleType) -> None:
        a = main.Account("acc-a", 1_000)
        b = main.Account("acc-b", 1_000)
        start = threading.Barrier(2, timeout=JOIN)

        def a_to_b() -> None:
            start.wait()
            for _ in range(1_000):
                main.transfer(a, b, 1)

        def b_to_a() -> None:
            start.wait()
            for _ in range(1_000):
                main.transfer(b, a, 1)

        run_concurrently(a_to_b, b_to_a)
        assert (a.balance, b.balance) == (1_000, 1_000), "money must be conserved"

    def test_random_transfers_conserve_total(self, main: ModuleType) -> None:
        accounts = [main.Account(f"acc-{i}", 500) for i in range(4)]

        def worker(seed: int) -> Callable[[], None]:
            def run() -> None:
                rng = random.Random(seed)
                for _ in range(1_000):
                    src, dst = rng.sample(accounts, 2)
                    try:
                        main.transfer(src, dst, rng.randint(1, 50))
                    except main.InsufficientFunds:
                        pass

            return run

        run_concurrently(*[worker(seed) for seed in range(4)])

        assert sum(acc.balance for acc in accounts) == 2_000, "total money must be conserved"
        assert all(acc.balance >= 0 for acc in accounts)


# ---------------------------------------------------------------- Part 4


class TestPart4Caches:
    def test_evicts_least_recently_used(self, main: ModuleType) -> None:
        cache = main.BoundedCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        assert cache.get("a") == 1  # refresh a
        cache.put("c", 3)  # evicts b, the LRU entry
        assert cache.get("b") is None
        assert cache.get("a") == 1
        assert len(cache) == 2

    def test_membership_does_not_refresh_recency(self, main: ModuleType) -> None:
        cache = main.BoundedCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        assert "a" in cache
        cache.put("c", 3)
        assert "a" not in cache, "'in' must not count as a use"
        assert "b" in cache and "c" in cache

    def test_put_updates_existing_key_without_growing(self, main: ModuleType) -> None:
        cache = main.BoundedCache(3)
        cache.put("a", 1)
        cache.put("a", 2)
        assert len(cache) == 1
        assert cache.get("a") == 2

    def test_hits_misses_and_default(self, main: ModuleType) -> None:
        cache = main.BoundedCache(4)
        assert cache.get("missing", default="fallback") == "fallback"
        cache.put("k", "v")
        cache.get("k")
        cache.get("k")
        assert (cache.hits, cache.misses) == (2, 1)

    def test_rejects_non_positive_size(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.BoundedCache(0)

    def test_concurrent_puts_respect_capacity(self, main: ModuleType) -> None:
        cache = main.BoundedCache(50)

        def writer(tid: int) -> Callable[[], None]:
            def run() -> None:
                for i in range(500):
                    cache.put(f"{tid}-{i}", i)

            return run

        run_concurrently(*[writer(tid) for tid in range(8)])
        assert len(cache) == 50, "capacity must hold under concurrent writers"

    def test_cached_value_skips_loader_on_second_call(self, main: ModuleType) -> None:
        cache = main.SingleFlightCache()
        assert cache.get_or_load("a", str.upper) == "A"
        assert cache.get_or_load("a", lambda key: pytest.fail(f"loader ran for {key}")) == "A"
        assert cache.load_count == 1

    def test_concurrent_callers_trigger_exactly_one_load(self, main: ModuleType) -> None:
        cache = main.SingleFlightCache()
        started = threading.Event()
        release = threading.Event()
        loads = Recorder()
        results = Recorder()
        barrier = threading.Barrier(8, timeout=JOIN)

        def loader(key: str) -> object:
            loads.add(key)
            started.set()
            release.wait(JOIN)  # keep the load in flight while others arrive
            return object()

        def caller() -> None:
            barrier.wait()
            results.add(cache.get_or_load("config", loader))

        def releaser() -> None:
            assert started.wait(JOIN), "loader never started"
            release.set()

        run_concurrently(*[caller] * 8, releaser)

        assert len(loads) == 1, f"loader ran {len(loads)} times; expected exactly one"
        assert cache.load_count == 1
        first = results.items[0]
        assert len(results) == 8 and all(item is first for item in results.items), (
            "every caller must receive the same loaded value"
        )

    def test_loader_failure_reaches_all_callers_and_is_not_cached(self, main: ModuleType) -> None:
        cache = main.SingleFlightCache()
        errors = Recorder()
        barrier = threading.Barrier(6, timeout=JOIN)

        def failing_loader(_key: str) -> str:
            raise ValueError("upstream down")

        def caller() -> None:
            barrier.wait()
            try:
                cache.get_or_load("k", failing_loader)
            except ValueError as exc:
                errors.add(str(exc))

        run_concurrently(*[caller] * 6)
        assert errors.items == ["upstream down"] * 6, "every caller must see the failure"
        assert cache.get_or_load("k", lambda key: "recovered") == "recovered", (
            "a failed load must not be cached"
        )

    def test_slow_load_does_not_block_other_keys(self, main: ModuleType) -> None:
        cache = main.SingleFlightCache()
        started = threading.Event()
        release = threading.Event()
        fast_result = Recorder()

        def slow_loader(_key: str) -> str:
            started.set()
            assert release.wait(JOIN)
            return "slow-value"

        def fast_caller() -> None:
            assert started.wait(JOIN)
            fast_result.add(cache.get_or_load("fast", lambda key: "fast-value"))
            release.set()

        run_concurrently(lambda: cache.get_or_load("slow", slow_loader), fast_caller)
        assert fast_result.items == ["fast-value"], "a slow key must not serialise other keys"

    def test_eviction_forces_reload(self, main: ModuleType) -> None:
        cache = main.SingleFlightCache(max_size=1)
        loader = str.upper
        cache.get_or_load("a", loader)
        cache.get_or_load("b", loader)  # evicts a
        cache.get_or_load("a", loader)
        assert cache.load_count == 3


# ---------------------------------------------------------------- Part 5


class TestPart5QueuesAndPipelines:
    def test_worker_group_processes_every_submitted_item(self, main: ModuleType) -> None:
        group = main.WorkerGroup(lambda x: x * 2, workers=3, queue_size=4)
        group.start()
        for i in range(50):
            group.submit(i)
        result = group.close()

        assert sorted(result.results) == [i * 2 for i in range(50)]
        assert result.failures == []

    def test_handler_errors_are_captured_per_item(self, main: ModuleType) -> None:
        def handler(x: int) -> int:
            if x == 3:
                raise ValueError("three is bad")
            return x

        group = main.WorkerGroup(handler, workers=2)
        group.start()
        for i in range(6):
            group.submit(i)
        result = group.close()

        assert sorted(result.results) == [0, 1, 2, 4, 5]
        assert len(result.failures) == 1
        failure = result.failures[0]
        assert failure.item == 3
        assert isinstance(failure.error, ValueError)

    def test_close_drains_queued_work_and_timeout_is_recoverable(self, main: ModuleType) -> None:
        release = threading.Event()

        def handler(x: int) -> int:
            assert release.wait(JOIN)
            return x

        group = main.WorkerGroup(handler, workers=1)
        group.start()
        for i in range(5):
            group.submit(i)

        with pytest.raises(TimeoutError):
            group.close(timeout=0.05)  # worker is blocked, so close cannot finish yet
        with pytest.raises(RuntimeError):
            group.submit(99)  # closing: no new work accepted

        release.set()
        result = group.close()
        assert sorted(result.results) == [0, 1, 2, 3, 4], "queued items must still be processed"

    def test_lifecycle_errors_and_idempotent_close(self, main: ModuleType) -> None:
        group = main.WorkerGroup(lambda x: x, workers=1)
        with pytest.raises(RuntimeError):
            group.submit(1)
        with pytest.raises(RuntimeError):
            group.close()
        group.start()
        with pytest.raises(RuntimeError):
            group.start()
        first = group.close()
        assert first.results == [] and first.failures == []
        assert group.close() == first, "a second close must return the same result"

    def test_invalid_worker_count_is_rejected(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.WorkerGroup(lambda x: x, workers=0)

    def test_pipeline_preserves_input_order_across_workers(self, main: ModuleType) -> None:
        results = main.run_pipeline(
            range(100),
            [lambda x: x + 1, lambda x: x * 10],
            workers_per_stage=3,
        )
        assert results == [(i + 1) * 10 for i in range(100)]

    def test_pipeline_orders_output_when_items_finish_out_of_order(self, main: ModuleType) -> None:
        release = threading.Event()

        def stage(x: int) -> int:
            if x == 0:
                assert release.wait(JOIN)  # item 0 finishes last
            if x == 1:
                release.set()
            return x

        assert main.run_pipeline(range(4), [stage], workers_per_stage=2) == [0, 1, 2, 3]

    def test_pipeline_failure_tears_down_and_raises(self, main: ModuleType) -> None:
        def stage_a(x: int) -> int:
            if x == 7:
                raise ValueError("bad 7")
            return x

        with pytest.raises(ValueError, match="bad 7"):
            main.run_pipeline(range(100), [stage_a, lambda x: x], workers_per_stage=2, queue_size=1)

    def test_pipeline_edge_cases(self, main: ModuleType) -> None:
        assert main.run_pipeline([], [lambda x: x]) == []
        assert main.run_pipeline([1, 2], []) == [1, 2]
        with pytest.raises(ValueError):
            main.run_pipeline([1], [lambda x: x], workers_per_stage=0)
        with pytest.raises(ValueError):
            main.run_pipeline([1], [lambda x: x], queue_size=0)


# ---------------------------------------------------------------- Part 6


class TestPart6Scheduler:
    def test_job_runs_immediately_then_repeatedly_and_stops_cleanly(self, main: ModuleType) -> None:
        runs = Recorder()
        three_runs = threading.Event()

        def job() -> None:
            runs.add(1)
            if len(runs) >= 3:
                three_runs.set()

        scheduler = main.Scheduler()
        scheduler.add("tick", 0.01, job)
        scheduler.start()
        assert three_runs.wait(JOIN), "job should run repeatedly"
        scheduler.stop()

        count = len(runs)
        assert count >= 3
        assert len(runs) == count, "no job may run after stop() returns"

    def test_stop_wakes_sleeping_jobs_promptly(self, main: ModuleType) -> None:
        ran = threading.Event()
        scheduler = main.Scheduler()
        scheduler.add("slow", 3600, ran.set)
        scheduler.start()
        assert ran.wait(JOIN)
        # Would raise TimeoutError if stop() waited out the hour-long interval.
        scheduler.stop(timeout=JOIN)

    def test_job_errors_are_recorded_and_job_keeps_running(self, main: ModuleType) -> None:
        attempts = Recorder()
        recovered = threading.Event()

        def flaky() -> None:
            attempts.add(1)
            if len(attempts) == 1:
                raise ValueError("first run fails")
            recovered.set()

        scheduler = main.Scheduler()
        scheduler.add("flaky", 0.01, flaky)
        scheduler.start()
        assert recovered.wait(JOIN), "job must keep running after an error"
        scheduler.stop()

        errors = scheduler.errors
        assert len(errors) == 1
        name, error = errors[0]
        assert name == "flaky"
        assert isinstance(error, ValueError)

    def test_add_and_start_validation(self, main: ModuleType) -> None:
        scheduler = main.Scheduler()
        with pytest.raises(ValueError):
            scheduler.add("zero", 0, lambda: None)
        with pytest.raises(ValueError):
            scheduler.add("neg", -1.0, lambda: None)
        scheduler.add("dup", 1.0, lambda: None)
        with pytest.raises(ValueError):
            scheduler.add("dup", 1.0, lambda: None)

        scheduler.start()
        with pytest.raises(RuntimeError):
            scheduler.add("late", 1.0, lambda: None)
        with pytest.raises(RuntimeError):
            scheduler.start()
        scheduler.stop()
        with pytest.raises(RuntimeError):
            scheduler.start()

    def test_stop_times_out_on_stuck_job_then_completes(self, main: ModuleType) -> None:
        started = threading.Event()
        release = threading.Event()

        def stuck() -> None:
            started.set()
            assert release.wait(JOIN)

        scheduler = main.Scheduler()
        scheduler.add("stuck", 0.01, stuck)
        scheduler.start()
        assert started.wait(JOIN)

        with pytest.raises(TimeoutError):
            scheduler.stop(timeout=0.05)
        release.set()
        scheduler.stop()  # now completes and joins the job thread

    def test_context_manager_starts_and_stops_jobs(self, main: ModuleType) -> None:
        ran = threading.Event()
        scheduler = main.Scheduler()
        scheduler.add("ctx", 0.01, ran.set)
        with scheduler:
            assert ran.wait(JOIN)
        # Leaving the block must have joined the job thread (checked by the leak fixture).


# ---------------------------------------------------------------- Part 7


class TestPart7ProcessPool:
    def test_is_prime_and_count_primes_on_known_values(self, main: ModuleType) -> None:
        assert [main.is_prime(n) for n in (0, 1, 2, 3, 4, 9, 91, 97)] == [
            False, False, True, True, False, False, False, True,
        ]
        assert main.count_primes(range(10, 20)) == 4
        assert main.count_primes(range(0, 10_000)) == 1229

    def test_chunk_ranges_split_exactly(self, main: ModuleType) -> None:
        assert main.chunk_ranges(0, 10, 3) == [range(0, 4), range(4, 7), range(7, 10)]
        assert main.chunk_ranges(0, 2, 5) == [range(0, 1), range(1, 2)], "no empty chunks"
        assert main.chunk_ranges(5, 5, 3) == []
        with pytest.raises(ValueError):
            main.chunk_ranges(0, 10, 0)

        for parts in range(1, 8):
            for stop in range(0, 31):
                chunks = main.chunk_ranges(0, stop, parts)
                flat = [n for chunk in chunks for n in chunk]
                assert flat == list(range(stop)), f"coverage broken for parts={parts} stop={stop}"
                sizes = [len(chunk) for chunk in chunks]
                assert all(sizes), "chunks must be non-empty"
                assert not sizes or max(sizes) - min(sizes) <= 1, "sizes must differ by at most 1"
                assert len(chunks) <= parts

    def test_parallel_map_matches_sequential_across_processes(self, main: ModuleType) -> None:
        chunks = main.chunk_ranges(0, 10_000, 4)
        expected = [main.count_primes(chunk) for chunk in chunks]

        counts = main.parallel_map(main.count_primes, chunks, workers=2)

        assert counts == expected, "results must match sequential and keep input order"
        assert sum(counts) == 1229

    def test_worker_exception_propagates_from_process_pool(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.parallel_map(math.factorial, [3, -1], workers=2)

    def test_parallel_map_argument_validation(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.parallel_map(math.factorial, [3], workers=0)
        assert main.parallel_map(math.factorial, []) == []
