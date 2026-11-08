"""Tests for the asyncio exercise. Spec: README.md."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def run[T](coro: Awaitable[T], *, limit: float = 5.0) -> T:
    """Run ``coro`` under a hard deadline so a deadlock fails the test instead of hanging."""

    async def guarded() -> T:
        async with asyncio.timeout(limit):
            return await coro

    return asyncio.run(guarded())


async def settle(ticks: int = 10) -> None:
    """Yield to the loop so freshly created tasks get to run their first steps."""
    for _ in range(ticks):
        await asyncio.sleep(0)


async def wait_until(predicate: Callable[[], bool], *, ticks: int = 1000) -> None:
    for _ in range(ticks):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def pool_fns(made: list[dict[str, int]], closed: list[int]) -> tuple[Callable[[], Awaitable[dict[str, int]]], Callable[[dict[str, int]], Awaitable[None]]]:
    async def factory() -> dict[str, int]:
        conn = {"id": len(made)}
        made.append(conn)
        return conn

    async def close(conn: dict[str, int]) -> None:
        closed.append(conn["id"])

    return factory, close


class TestPart1Aggregate:
    def test_values_come_back_in_source_order(self, main: ModuleType) -> None:
        async def a() -> int:
            return 1

        async def b() -> int:
            await asyncio.sleep(0.02)  # finishes last, but must still come back second
            return 2

        async def c() -> int:
            return 3

        results = run(main.aggregate({"a": a, "b": b, "c": c}, per_call_timeout=1.0))
        assert list(results) == ["a", "b", "c"], "keys must follow the sources' order"
        assert [r.value for r in results.values()] == [1, 2, 3]
        for name, result in results.items():
            assert result.name == name, f"SourceResult.name should be {name!r}"
            assert result.ok and result.error is None and not result.timed_out, f"{name} should succeed cleanly"

    def test_failure_is_captured_not_raised(self, main: ModuleType) -> None:
        async def boom() -> None:
            raise ValueError("boom")

        async def fine() -> str:
            return "ok"

        results = run(main.aggregate({"bad": boom, "good": fine}, per_call_timeout=1.0))
        bad = results["bad"]
        assert bad.ok is False, "failed source must be ok=False"
        assert bad.error == "ValueError: boom", f"unexpected error text: {bad.error!r}"
        assert bad.timed_out is False, "an ordinary exception is not a timeout"
        assert results["good"].ok and results["good"].value == "ok", "other sources must be unaffected"

    def test_slow_source_times_out_while_others_finish(self, main: ModuleType) -> None:
        async def slow() -> None:
            await asyncio.sleep(5)  # cancelled by the per-call timeout almost immediately

        async def fast() -> str:
            return "fast"

        results = run(main.aggregate({"slow": slow, "fast": fast}, per_call_timeout=0.05))
        slow_result = results["slow"]
        assert slow_result.ok is False and slow_result.timed_out is True, "slow source must be marked timed out"
        assert slow_result.error == "TimeoutError: per-call timeout", f"unexpected error: {slow_result.error!r}"
        assert results["fast"].ok and results["fast"].value == "fast", "fast source must still succeed"

    def test_sources_run_concurrently_and_are_called_once(self, main: ModuleType) -> None:
        calls: list[str] = []

        async def scenario() -> dict[str, Any]:
            barrier = asyncio.Barrier(3)

            def make(name: str) -> Callable[[], Awaitable[str]]:
                async def member() -> str:
                    calls.append(name)
                    await barrier.wait()  # only completes if all three are in flight at once
                    return name

                return member

            return await main.aggregate(
                {"a": make("a"), "b": make("b"), "c": make("c")},
                per_call_timeout=2.0,
            )

        results = run(scenario(), limit=10.0)
        assert all(r.ok for r in results.values()), (
            "all three sources must be in flight together (barrier would time out if sequential)"
        )
        assert sorted(calls) == ["a", "b", "c"], "each source factory must be invoked exactly once"

    def test_empty_sources_and_bad_timeout(self, main: ModuleType) -> None:
        assert run(main.aggregate({}, per_call_timeout=1.0)) == {}, "no sources means an empty dict"
        with pytest.raises(ValueError):
            run(main.aggregate({}, per_call_timeout=0))

    def test_outer_cancellation_propagates_and_cancels_sources(self, main: ModuleType) -> None:
        async def scenario() -> list[str]:
            cancelled: list[str] = []

            async def forever() -> None:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append("forever")
                    raise

            task = asyncio.create_task(main.aggregate({"f": forever}, per_call_timeout=60.0))
            await settle()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return cancelled

        assert run(scenario()) == ["forever"], "cancelling aggregate must cancel in-flight sources"


class TestPart2FanOutStrict:
    def test_success_returns_values_in_source_order(self, main: ModuleType) -> None:
        async def one() -> int:
            await asyncio.sleep(0.01)
            return 1

        async def two() -> int:
            return 2

        result = run(main.fetch_all_strict({"one": one, "two": two}))
        assert result == {"one": 1, "two": 2}
        assert list(result) == ["one", "two"], "keys must keep source order"

    def test_single_failure_cancels_siblings_and_raises_group(self, main: ModuleType) -> None:
        async def scenario() -> tuple[ExceptionGroup[Any], list[str]]:
            cancelled: list[str] = []
            slow_started = asyncio.Event()

            async def slow() -> str:
                slow_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append("slow")
                    raise
                return "never"

            async def bad() -> str:
                await slow_started.wait()  # guarantee the sibling is running before we fail
                raise ValueError("nope")

            with pytest.raises(ExceptionGroup) as info:
                await main.fetch_all_strict({"slow": slow, "bad": bad})
            return info.value, cancelled

        group, cancelled = run(scenario())
        assert len(group.exceptions) == 1, f"expected exactly one exception, got {group.exceptions!r}"
        assert isinstance(group.exceptions[0], ValueError), "the group must contain the original ValueError"
        assert cancelled == ["slow"], "the sibling's finally/cancel handler must have run before the group was raised"

    def test_multiple_failures_are_all_collected(self, main: ModuleType) -> None:
        async def scenario() -> ExceptionGroup[Any]:
            barrier = asyncio.Barrier(2)

            def failing(msg: str) -> Callable[[], Awaitable[str]]:
                async def op() -> str:
                    await barrier.wait()  # both fail in the same loop iteration
                    raise ValueError(msg)

                return op

            with pytest.raises(ExceptionGroup) as info:
                await main.fetch_all_strict({"a": failing("a"), "b": failing("b")})
            return info.value

        group = run(scenario())
        messages = sorted(str(e) for e in group.exceptions)
        assert messages == ["a", "b"], f"both failures must be reported, got {messages!r}"

    def test_empty_mapping_returns_empty_dict(self, main: ModuleType) -> None:
        assert run(main.fetch_all_strict({})) == {}


class TestPart3Cancellation:
    def test_success_returns_value_and_runs_cleanup_once(self, main: ModuleType) -> None:
        journal: list[str] = []

        async def body() -> int:
            await asyncio.sleep(0)
            return 42

        async def cleanup() -> None:
            journal.append("cleanup")

        assert run(main.run_with_cleanup(body, cleanup, timeout=1.0)) == 42
        assert journal == ["cleanup"], "cleanup must run exactly once on success"

    def test_timeout_raises_and_cleanup_can_await(self, main: ModuleType) -> None:
        journal: list[str] = []

        async def body() -> None:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                journal.append("body-cancelled")
                raise

        async def cleanup() -> None:
            await asyncio.sleep(0.001)  # awaiting inside finally after a timeout must work
            journal.append("cleanup")

        with pytest.raises(TimeoutError):
            run(main.run_with_cleanup(body, cleanup, timeout=0.02))
        assert journal == ["body-cancelled", "cleanup"], "body must see cancellation, then cleanup must finish"

    def test_body_exception_propagates_and_cleanup_runs(self, main: ModuleType) -> None:
        journal: list[str] = []

        async def body() -> None:
            raise KeyError("missing")

        async def cleanup() -> None:
            journal.append("cleanup")

        with pytest.raises(KeyError):
            run(main.run_with_cleanup(body, cleanup, timeout=1.0))
        assert journal == ["cleanup"], "cleanup must run when the body raises"

    def test_outer_cancellation_is_not_turned_into_timeout(self, main: ModuleType) -> None:
        async def scenario() -> list[str]:
            journal: list[str] = []

            async def body() -> None:
                await asyncio.sleep(10)

            async def cleanup() -> None:
                journal.append("cleanup")

            task = asyncio.create_task(main.run_with_cleanup(body, cleanup, timeout=30.0))
            await settle()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return journal

        assert run(scenario()) == ["cleanup"], "external cancel must run cleanup and surface as CancelledError"

    def test_shielded_commit_returns_value(self, main: ModuleType) -> None:
        async def op() -> str:
            return "committed"

        assert run(main.shielded_commit(op)) == "committed"

    def test_caller_cancel_does_not_abort_the_commit(self, main: ModuleType) -> None:
        async def scenario() -> list[str]:
            gate = asyncio.Event()
            finished = asyncio.Event()
            journal: list[str] = []

            async def op() -> str:
                await gate.wait()
                journal.append("committed")
                finished.set()
                return "ok"

            task = asyncio.create_task(main.shielded_commit(op))
            await settle()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            gate.set()
            await asyncio.wait_for(finished.wait(), 1.0)
            await settle()
            return journal

        assert run(scenario()) == ["committed"], "the operation must finish even though the caller was cancelled"

    def test_failure_after_caller_cancel_is_logged(self, main: ModuleType, caplog: pytest.LogCaptureFixture) -> None:
        async def scenario() -> None:
            gate = asyncio.Event()
            finished = asyncio.Event()

            async def op() -> None:
                try:
                    await gate.wait()
                    raise RuntimeError("disk full")
                finally:
                    finished.set()

            task = asyncio.create_task(main.shielded_commit(op))
            await settle()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            gate.set()
            await asyncio.wait_for(finished.wait(), 1.0)
            await settle()

        with caplog.at_level(logging.ERROR, logger="pyref.commit"):
            run(scenario())
        errors = [r for r in caplog.records if r.name == "pyref.commit" and r.levelno == logging.ERROR]
        assert len(errors) == 1, f"expected one ERROR record from pyref.commit, got {len(errors)}"
        assert errors[0].exc_info is not None and isinstance(errors[0].exc_info[1], RuntimeError), (
            "the log record must carry the RuntimeError via exc_info"
        )


class TestPart4Bounded:
    def test_results_keep_input_order_despite_completion_order(self, main: ModuleType) -> None:
        async def scenario() -> list[float]:
            fetcher = main.BoundedFetcher(3)

            async def fetch(delay: float) -> float:
                await asyncio.sleep(delay)
                return delay

            return await fetcher.run([0.03, 0.0, 0.01], fetch)

        assert run(scenario()) == [0.03, 0.0, 0.01]

    def test_peak_in_flight_reaches_but_never_exceeds_limit(self, main: ModuleType) -> None:
        async def scenario() -> tuple[int, int, list[int]]:
            fetcher = main.BoundedFetcher(3)
            gate = asyncio.Event()
            active = 0
            seen_active: list[int] = []

            async def fetch(i: int) -> int:
                nonlocal active
                active += 1
                try:
                    await gate.wait()
                    return i
                finally:
                    active -= 1

            task = asyncio.create_task(fetcher.run(list(range(10)), fetch))
            await wait_until(lambda: active == 3)
            await settle()
            seen_active.append(active)
            gate.set()
            results = await task
            return seen_active[0], fetcher.peak_in_flight, results

        in_flight, peak, results = run(scenario())
        assert in_flight == 3, f"exactly `limit` fetches should be in flight while gated, saw {in_flight}"
        assert peak == 3, f"peak_in_flight should be the limit, got {peak}"
        assert results == list(range(10))

    def test_limit_one_serialises_work(self, main: ModuleType) -> None:
        async def scenario() -> tuple[int, list[int]]:
            fetcher = main.BoundedFetcher(1)

            async def fetch(i: int) -> int:
                await asyncio.sleep(0)
                return i * i

            results = await fetcher.run([1, 2, 3, 4, 5], fetch)
            return fetcher.peak_in_flight, results

        peak, results = run(scenario())
        assert peak == 1, "limit=1 must never overlap fetches"
        assert results == [1, 4, 9, 16, 25]

    def test_first_failure_cancels_remaining_fetches(self, main: ModuleType) -> None:
        async def scenario() -> tuple[ExceptionGroup[Any], list[str]]:
            fetcher = main.BoundedFetcher(2)
            cancelled: list[str] = []

            async def fetch(item: str) -> str:
                if item == "bad":
                    raise ValueError("bad item")
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(item)
                    raise
                return item

            with pytest.raises(ExceptionGroup) as info:
                await fetcher.run(["a", "bad", "b"], fetch)
            return info.value, cancelled

        group, cancelled = run(scenario())
        assert len(group.exceptions) == 1 and isinstance(group.exceptions[0], ValueError)
        assert "a" in cancelled, "the blocked sibling must be cancelled when another fetch fails"

    def test_invalid_limit_and_empty_input(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.BoundedFetcher(0)

        async def scenario() -> list[int]:
            async def fetch(i: int) -> int:
                return i

            return await main.BoundedFetcher(2).run([], fetch)

        assert run(scenario()) == []

    def test_read_files_returns_contents_keyed_by_path(self, main: ModuleType, tmp_path: Path) -> None:
        first = tmp_path / "a.bin"
        second = tmp_path / "b.bin"
        first.write_bytes(b"alpha")
        second.write_bytes(b"beta")
        assert run(main.read_files([first, second], limit=2)) == {first: b"alpha", second: b"beta"}

    def test_read_files_missing_file_raises_group(self, main: ModuleType, tmp_path: Path) -> None:
        with pytest.raises(ExceptionGroup) as info:
            run(main.read_files([tmp_path / "nope.bin"], limit=1))
        assert any(isinstance(e, FileNotFoundError) for e in info.value.exceptions), (
            "the group must contain the FileNotFoundError from the blocking read"
        )


class TestPart5WorkerPool:
    def test_results_and_processed_count(self, main: ModuleType) -> None:
        async def handler(x: int) -> int:
            return x * 2

        async def scenario() -> tuple[list[int], int]:
            async with main.WorkerPool(handler, workers=2) as pool:
                results = [await pool.submit(i) for i in range(4)]
                return results, pool.processed

        assert run(scenario()) == ([0, 2, 4, 6], 4)

    def test_handler_error_goes_only_to_its_submitter(self, main: ModuleType) -> None:
        async def handler(x: int) -> int:
            if x < 0:
                raise ValueError(f"negative: {x}")
            return x

        async def scenario() -> tuple[int, int]:
            async with main.WorkerPool(handler, workers=1) as pool:
                with pytest.raises(ValueError, match="negative: -1"):
                    await pool.submit(-1)
                value = await pool.submit(5)
                return value, pool.processed

        assert run(scenario()) == (5, 1), "the worker must survive a failing item; failures don't count as processed"

    def test_concurrency_bounded_by_worker_count(self, main: ModuleType) -> None:
        async def scenario() -> tuple[list[int], int]:
            gate = asyncio.Event()
            active = 0
            peak = 0

            async def handler(x: int) -> int:
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                try:
                    await gate.wait()
                    return x
                finally:
                    active -= 1

            async with main.WorkerPool(handler, workers=2) as pool:
                tasks = [asyncio.create_task(pool.submit(i)) for i in range(6)]
                await wait_until(lambda: active == 2)
                await settle()
                gate.set()
                results = await asyncio.gather(*tasks)
            return list(results), peak

        results, peak = run(scenario())
        assert results == list(range(6))
        assert peak == 2, f"at most `workers` handlers may run at once, saw peak {peak}"

    def test_aclose_drain_finishes_queued_work(self, main: ModuleType) -> None:
        async def scenario() -> tuple[list[int], int]:
            gate = asyncio.Event()

            async def handler(x: int) -> int:
                await gate.wait()
                return x + 100

            async with main.WorkerPool(handler, workers=1) as pool:
                tasks = [asyncio.create_task(pool.submit(i)) for i in range(3)]
                await settle()
                closer = asyncio.create_task(pool.aclose(drain=True))
                await settle()
                assert not closer.done(), "aclose(drain=True) must wait for queued items"
                gate.set()
                await closer
                results = [await t for t in tasks]
                return results, pool.processed

        assert run(scenario()) == ([100, 101, 102], 3)

    def test_aclose_without_drain_fails_pending_submissions(self, main: ModuleType) -> None:
        async def scenario() -> tuple[list[int], list[object]]:
            gate = asyncio.Event()
            calls: list[int] = []

            async def handler(x: int) -> int:
                calls.append(x)
                await gate.wait()
                return x

            async with main.WorkerPool(handler, workers=1) as pool:
                tasks = [asyncio.create_task(pool.submit(i)) for i in range(3)]
                await wait_until(lambda: calls == [0])
                await pool.aclose(drain=False)
                results = await asyncio.gather(*tasks, return_exceptions=True)
            return calls, list(results)

        calls, results = run(scenario())
        assert calls == [0], "queued items must never reach the handler after a non-draining close"
        assert all(isinstance(r, RuntimeError) for r in results), (
            f"every pending submission must fail with RuntimeError, got {results!r}"
        )

    def test_submit_before_start_and_after_close_raise(self, main: ModuleType) -> None:
        async def handler(x: int) -> int:
            return x

        async def scenario() -> None:
            pool = main.WorkerPool(handler, workers=1)
            with pytest.raises(RuntimeError):
                await pool.submit(1)
            async with pool:
                assert await pool.submit(1) == 1
            with pytest.raises(RuntimeError):
                await pool.submit(2)

        run(scenario())
        with pytest.raises(ValueError):
            main.WorkerPool(handler, workers=0)

    def test_cancelled_submitter_is_skipped(self, main: ModuleType) -> None:
        async def scenario() -> tuple[list[str], int]:
            gate = asyncio.Event()
            calls: list[str] = []

            async def handler(x: str) -> str:
                calls.append(x)
                await gate.wait()
                return x

            async with main.WorkerPool(handler, workers=1) as pool:
                first = asyncio.create_task(pool.submit("A"))
                await wait_until(lambda: calls == ["A"])
                second = asyncio.create_task(pool.submit("B"))
                await settle()
                second.cancel()
                await settle()
                gate.set()
                assert await first == "A"
                with pytest.raises(asyncio.CancelledError):
                    await second
            return calls, pool.processed

        calls, processed = run(scenario())
        assert calls == ["A"], "the cancelled item must be skipped, never handed to the handler"
        assert processed == 1

    def test_exiting_context_leaves_no_worker_tasks(self, main: ModuleType) -> None:
        async def handler(x: int) -> int:
            return x

        async def scenario() -> list[asyncio.Task[Any]]:
            async with main.WorkerPool(handler, workers=3) as pool:
                await pool.submit(1)
            await settle()
            return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

        assert run(scenario()) == [], "workers must be cancelled and awaited on exit"


class TestPart6Streams:
    @staticmethod
    def make_api(pages: dict[str | None, tuple[list[int], str | None]], calls: list[str | None]) -> Callable[[str | None], Awaitable[Any]]:
        async def fetch(cursor: str | None) -> Any:
            calls.append(cursor)
            items, next_cursor = pages[cursor]
            return _page(items, next_cursor)

        return fetch

    def test_collects_across_pages_following_cursors(self, main: ModuleType) -> None:
        pages = {None: ([1, 2], "c1"), "c1": ([], "c2"), "c2": ([3], None)}
        calls: list[str | None] = []
        fetch = self.make_api(pages, calls)

        async def collect() -> list[int]:
            return [item async for item in main.paginate(fetch)]

        assert run(collect()) == [1, 2, 3], "items must come back in order across all pages"
        assert calls == [None, "c1", "c2"], "cursors must be followed in order, including empty pages"

    def test_pages_are_fetched_lazily(self, main: ModuleType) -> None:
        pages = {None: ([1, 2], "c1"), "c1": ([3], None)}
        calls: list[str | None] = []
        fetch = self.make_api(pages, calls)

        async def first_item() -> int:
            async with aclosing(main.paginate(fetch)) as stream:
                async for item in stream:
                    return item
            raise AssertionError("stream was empty")

        assert run(first_item()) == 1
        assert calls == [None], "breaking after the first item must not fetch the next page"

    def test_max_pages_stops_early(self, main: ModuleType) -> None:
        pages = {None: ([1, 2], "c1"), "c1": ([3], "c2"), "c2": ([4], None)}
        calls: list[str | None] = []
        fetch = self.make_api(pages, calls)

        async def collect() -> list[int]:
            return [item async for item in main.paginate(fetch, max_pages=2)]

        assert run(collect()) == [1, 2, 3]
        assert calls == [None, "c1"], "max_pages=2 must fetch exactly two pages"

    def test_max_pages_must_be_positive(self, main: ModuleType) -> None:
        calls: list[str | None] = []
        fetch = self.make_api({None: ([1], None)}, calls)

        async def collect() -> list[int]:
            return [item async for item in main.paginate(fetch, max_pages=0)]

        with pytest.raises(ValueError):
            run(collect())

    def test_fetch_error_surfaces_after_earlier_items(self, main: ModuleType) -> None:
        async def fetch(cursor: str | None) -> Any:
            if cursor is None:
                return _page([1, 2], "c1")
            raise OSError("upstream went away")

        async def scenario() -> list[int]:
            got: list[int] = []
            async with aclosing(main.paginate(fetch)) as stream:
                with pytest.raises(OSError):
                    async for item in stream:
                        got.append(item)
            return got

        assert run(scenario()) == [1, 2]

    def test_reuses_idle_resources_and_returns_on_exception(self, main: ModuleType) -> None:
        made: list[dict[str, int]] = []
        closed: list[int] = []
        factory, close = pool_fns(made, closed)

        async def scenario() -> tuple[int, int, int]:
            pool = main.ResourcePool(factory, close, max_size=1)
            async with pool.lease() as first:
                pass
            with pytest.raises(KeyError):
                async with pool.lease():
                    raise KeyError("boom")
            async with pool.lease() as third:
                return first["id"], third["id"], pool.created

        first_id, third_id, created = run(scenario())
        assert first_id == third_id == 0, "the released resource must be reused, including after a body exception"
        assert created == 1

    def test_max_size_bounds_leases_and_waiter_wakes(self, main: ModuleType) -> None:
        made: list[dict[str, int]] = []
        closed: list[int] = []
        factory, close = pool_fns(made, closed)

        async def scenario() -> tuple[bool, int, int]:
            pool = main.ResourcePool(factory, close, max_size=2)
            gate = asyncio.Event()

            async def hold() -> None:
                async with pool.lease():
                    await gate.wait()

            async def quick() -> int:
                async with pool.lease() as conn:
                    return conn["id"]

            holders = [asyncio.create_task(hold()), asyncio.create_task(hold())]
            await settle()
            waiter = asyncio.create_task(quick())
            await settle()
            blocked = not waiter.done()
            gate.set()
            await asyncio.gather(*holders)
            quick_id = await waiter
            return blocked, pool.created, quick_id

        blocked, created, quick_id = run(scenario())
        assert blocked, "a third lease must wait while max_size leases are out"
        assert created == 2, "no more than max_size resources may be created"
        assert quick_id in (0, 1), "the waiter should receive one of the existing resources"

    def test_factory_failure_frees_the_slot(self, main: ModuleType) -> None:
        attempts = 0

        async def flaky() -> dict[str, int]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionError("down")
            return {"id": attempts}

        async def close(conn: dict[str, int]) -> None:
            return None

        async def scenario() -> int:
            pool = main.ResourcePool(flaky, close, max_size=1)
            with pytest.raises(ConnectionError):
                async with pool.lease():
                    pass
            async with pool.lease() as conn:  # would hang forever if the slot leaked
                return conn["id"]

        assert run(scenario()) == 2

    def test_aclose_closes_idle_defers_leased_and_rejects_new_leases(self, main: ModuleType) -> None:
        made: list[dict[str, int]] = []
        closed: list[int] = []
        factory, close = pool_fns(made, closed)

        async def scenario() -> tuple[list[int], list[int]]:
            pool = main.ResourcePool(factory, close, max_size=2)
            async with pool.lease():
                async with pool.lease():  # second resource, id 1, goes idle when this exits
                    pass
                await pool.aclose()
                during = list(closed)
            await pool.aclose()  # idempotent
            with pytest.raises(RuntimeError):
                async with pool.lease():
                    pass
            return during, list(closed)

        during, after = run(scenario())
        assert during == [1], "aclose must close idle resources immediately"
        assert after == [1, 0], "the leased resource must be closed when its lease ends"

    def test_cancelled_waiter_does_not_leak_its_slot(self, main: ModuleType) -> None:
        made: list[dict[str, int]] = []
        closed: list[int] = []
        factory, close = pool_fns(made, closed)

        async def scenario() -> int:
            pool = main.ResourcePool(factory, close, max_size=1)
            gate = asyncio.Event()

            async def hold() -> None:
                async with pool.lease():
                    await gate.wait()

            async def quick() -> int:
                async with pool.lease() as conn:
                    return conn["id"]

            holder = asyncio.create_task(hold())
            await settle()
            waiter = asyncio.create_task(quick())
            await settle()
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            gate.set()
            await holder
            async with pool.lease() as conn:
                return conn["id"]

        assert run(scenario()) == 0, "a cancelled waiter must release its place in line"


def _page(items: list[int], next_cursor: str | None) -> Any:
    """Build a ``Page`` via the student's class (looked up lazily through the test module)."""
    import sys

    return sys.modules["main"].Page(items=items, next_cursor=next_cursor)


class TestPart7Resilience:
    def test_no_sleep_when_first_attempt_succeeds(self, main: ModuleType) -> None:
        delays: list[float] = []

        async def record(delay: float) -> None:
            delays.append(delay)

        async def op() -> str:
            return "ok"

        assert run(main.retry_async(op, attempts=3, sleep=record)) == "ok"
        assert delays == []

    def test_retries_until_success_with_exponential_backoff(self, main: ModuleType) -> None:
        delays: list[float] = []
        attempts: list[int] = []

        async def record(delay: float) -> None:
            delays.append(delay)

        async def op() -> str:
            attempts.append(len(attempts) + 1)
            if len(attempts) < 3:
                raise OSError("transient")
            return "ok"

        result = run(main.retry_async(op, attempts=5, base_delay=0.5, max_delay=10.0, sleep=record))
        assert result == "ok"
        assert delays == [0.5, 1.0], f"backoff should double from base_delay, got {delays!r}"

    def test_exhaustion_raises_last_error(self, main: ModuleType) -> None:
        delays: list[float] = []
        attempts: list[int] = []

        async def record(delay: float) -> None:
            delays.append(delay)

        async def op() -> str:
            attempts.append(1)
            raise RuntimeError(f"fail-{len(attempts)}")

        with pytest.raises(RuntimeError, match="fail-3"):
            run(main.retry_async(op, attempts=3, base_delay=0.5, max_delay=10.0, sleep=record))
        assert len(attempts) == 3, "exactly `attempts` calls must be made"
        assert delays == [0.5, 1.0], "no sleep after the final attempt"

    def test_backoff_is_capped_by_max_delay(self, main: ModuleType) -> None:
        delays: list[float] = []

        async def record(delay: float) -> None:
            delays.append(delay)

        async def op() -> None:
            raise OSError("down")

        with pytest.raises(OSError):
            run(main.retry_async(op, attempts=5, base_delay=0.5, max_delay=1.5, sleep=record))
        assert delays == [0.5, 1.0, 1.5, 1.5], f"delays must be capped at max_delay, got {delays!r}"

    def test_non_retryable_error_propagates_immediately(self, main: ModuleType) -> None:
        delays: list[float] = []
        attempts: list[int] = []

        async def record(delay: float) -> None:
            delays.append(delay)

        async def op() -> None:
            attempts.append(1)
            raise ValueError("bad input")

        with pytest.raises(ValueError):
            run(main.retry_async(op, attempts=4, retry_on=(OSError,), sleep=record))
        assert len(attempts) == 1 and delays == [], "errors outside retry_on must not be retried"

    def test_attempts_must_be_positive(self, main: ModuleType) -> None:
        async def op() -> None:
            return None

        with pytest.raises(ValueError):
            run(main.retry_async(op, attempts=0))

    def test_breaker_success_resets_failure_count(self, main: ModuleType) -> None:
        breaker = main.CircuitBreaker(failure_threshold=2, reset_timeout=10.0, clock=FakeClock())

        async def failing() -> None:
            raise OSError("x")

        async def ok() -> int:
            return 1

        async def scenario() -> Any:
            with pytest.raises(OSError):
                await breaker.call(failing)
            assert await breaker.call(ok) == 1
            with pytest.raises(OSError):
                await breaker.call(failing)
            return breaker.state

        assert run(scenario()) is main.CircuitState.CLOSED, "a success must reset the consecutive-failure count"

    def test_opens_after_threshold_and_short_circuits(self, main: ModuleType) -> None:
        breaker = main.CircuitBreaker(failure_threshold=2, reset_timeout=10.0, clock=FakeClock())
        calls: list[int] = []

        async def counted() -> None:
            calls.append(1)
            raise OSError("x")

        async def scenario() -> Any:
            for _ in range(2):
                with pytest.raises(OSError):
                    await breaker.call(counted)
            state = breaker.state
            with pytest.raises(main.CircuitOpenError):
                await breaker.call(counted)
            return state

        assert run(scenario()) is main.CircuitState.OPEN
        assert len(calls) == 2, "an open breaker must not invoke the operation"

    def test_half_open_trial_success_closes(self, main: ModuleType) -> None:
        clock = FakeClock()
        breaker = main.CircuitBreaker(failure_threshold=1, reset_timeout=10.0, clock=clock)

        async def failing() -> None:
            raise OSError("x")

        async def ok() -> int:
            return 7

        async def scenario() -> tuple[Any, Any, int]:
            with pytest.raises(OSError):
                await breaker.call(failing)
            clock.advance(10.0)
            half = breaker.state
            value = await breaker.call(ok)
            return half, breaker.state, value

        half, after, value = run(scenario())
        assert half is main.CircuitState.HALF_OPEN
        assert after is main.CircuitState.CLOSED and value == 7

    def test_half_open_failure_reopens_with_fresh_timer(self, main: ModuleType) -> None:
        clock = FakeClock()
        breaker = main.CircuitBreaker(failure_threshold=1, reset_timeout=10.0, clock=clock)

        async def failing() -> None:
            raise OSError("x")

        async def scenario() -> list[Any]:
            with pytest.raises(OSError):
                await breaker.call(failing)
            clock.advance(10.0)
            with pytest.raises(OSError):
                await breaker.call(failing)
            states = [breaker.state]
            clock.advance(9.0)
            states.append(breaker.state)
            clock.advance(1.0)
            states.append(breaker.state)
            return states

        opened, still_open, half_open = run(scenario())
        assert opened is main.CircuitState.OPEN
        assert still_open is main.CircuitState.OPEN, "the cooldown must restart after a failed trial"
        assert half_open is main.CircuitState.HALF_OPEN

    def test_only_one_trial_is_admitted_at_a_time(self, main: ModuleType) -> None:
        clock = FakeClock()
        breaker = main.CircuitBreaker(failure_threshold=1, reset_timeout=10.0, clock=clock)

        async def failing() -> None:
            raise OSError("x")

        async def ok() -> int:
            return 1

        async def scenario() -> tuple[Any, Any]:
            with pytest.raises(OSError):
                await breaker.call(failing)
            clock.advance(10.0)
            gate = asyncio.Event()

            async def slow_trial() -> str:
                await gate.wait()
                return "recovered"

            trial = asyncio.create_task(breaker.call(slow_trial))
            await settle()
            half = breaker.state
            with pytest.raises(main.CircuitOpenError):
                await breaker.call(ok)
            gate.set()
            assert await trial == "recovered"
            return half, breaker.state

        half, after = run(scenario())
        assert half is main.CircuitState.HALF_OPEN
        assert after is main.CircuitState.CLOSED

    def test_cancelled_trial_frees_the_probe_without_counting_failure(self, main: ModuleType) -> None:
        clock = FakeClock()
        breaker = main.CircuitBreaker(failure_threshold=1, reset_timeout=10.0, clock=clock)

        async def failing() -> None:
            raise OSError("x")

        async def ok() -> int:
            return 1

        async def scenario() -> tuple[Any, int]:
            with pytest.raises(OSError):
                await breaker.call(failing)
            clock.advance(10.0)

            async def hang() -> None:
                await asyncio.Event().wait()

            trial = asyncio.create_task(breaker.call(hang))
            await settle()
            trial.cancel()
            with pytest.raises(asyncio.CancelledError):
                await trial
            state = breaker.state
            return state, await breaker.call(ok)

        state, value = run(scenario())
        assert state is main.CircuitState.HALF_OPEN, "a cancelled trial must leave the breaker half-open"
        assert value == 1, "the next call must be admitted as a fresh trial"

    def test_token_bucket_starts_full(self, main: ModuleType) -> None:
        bucket = main.TokenBucket(rate=1.0, capacity=3.0, clock=FakeClock())
        assert [bucket.try_acquire() for _ in range(4)] == [True, True, True, False]

    def test_token_bucket_refills_proportionally_and_caps(self, main: ModuleType) -> None:
        clock = FakeClock()
        bucket = main.TokenBucket(rate=2.0, capacity=3.0, clock=clock)
        for _ in range(3):
            assert bucket.try_acquire()
        clock.advance(0.5)  # 0.5 s at 2 tokens/s is exactly one token
        assert bucket.try_acquire() is True
        assert bucket.try_acquire() is False
        clock.advance(100.0)  # refill never exceeds capacity
        assert [bucket.try_acquire() for _ in range(4)] == [True, True, True, False]

    def test_token_bucket_acquire_sleeps_exact_deficit(self, main: ModuleType) -> None:
        clock = FakeClock()
        sleeps: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)
            clock.advance(delay)

        bucket = main.TokenBucket(rate=4.0, capacity=1.0, clock=clock, sleep=fake_sleep)
        assert bucket.try_acquire()
        run(bucket.acquire())
        assert sleeps == [0.25], f"one token at 4/s needs exactly 0.25 s, slept {sleeps!r}"

    def test_token_bucket_argument_validation(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.TokenBucket(rate=0.0, capacity=1.0)
        with pytest.raises(ValueError):
            main.TokenBucket(rate=1.0, capacity=0.0)
        bucket = main.TokenBucket(rate=1.0, capacity=2.0, clock=FakeClock())
        with pytest.raises(ValueError):
            bucket.try_acquire(3.0)
        with pytest.raises(ValueError):
            run(bucket.acquire(3.0))
