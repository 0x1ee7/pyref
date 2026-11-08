"""Tests for the iterators and generators exercise. The spec is README.md."""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class Recorder:
    """Iterator wrapper that counts how many items a consumer has pulled."""

    def __init__(self, items: Iterable[Any]) -> None:
        self._it = iter(items)
        self.pulled = 0

    def __iter__(self) -> Recorder:
        return self

    def __next__(self) -> Any:
        item = next(self._it)
        self.pulled += 1
        return item


def log_line(minute: int, level: str, server: str, message: str) -> str:
    ts = (datetime(2026, 3, 1, 12, tzinfo=UTC) + timedelta(minutes=minute)).isoformat()
    return f"{ts} {level} {server} {message}"


def make_record(main: ModuleType, minute: int = 0, level: str = "INFO",
                server: str = "web-1", message: str = "m") -> Any:
    return main.LogRecord(
        ts=datetime(2026, 3, 1, 12, minute, tzinfo=UTC),
        level=level,
        server=server,
        message=message,
    )


class FakeResource:
    """Context manager that records open/close and yields a fixed iterable of lines."""

    def __init__(self, lines: Iterable[str]) -> None:
        self._lines = lines
        self.opened = 0
        self.closed = False

    def __enter__(self) -> Iterable[str]:
        self.opened += 1
        return self._lines

    def __exit__(self, *exc_info: object) -> bool:
        self.closed = True
        return False


def scripted_api(main: ModuleType, pages: dict[Any, tuple[tuple[Any, ...], Any]],
                 calls: list[Any], failing: Any = object()) -> Any:
    """Fake fetch_page: serves pages keyed by cursor and records every request."""

    def fetch(cursor: Any) -> Any:
        calls.append(cursor)
        if cursor == failing:
            raise TimeoutError(f"timed out on {cursor!r}")
        items, next_cursor = pages[cursor]
        return main.Page(items=items, next_cursor=next_cursor)

    return fetch


# ---------------------------------------------------------------------------
# Part 1: iterator protocol and re-iterable containers
# ---------------------------------------------------------------------------


class TestPart1IteratorProtocol:
    def test_countdown_yields_descending_values(self, main: ModuleType) -> None:
        assert list(main.Countdown(3)) == [3, 2, 1], "Countdown(3) should yield 3, 2, 1"

    def test_countdown_zero_is_empty(self, main: ModuleType) -> None:
        assert list(main.Countdown(0)) == [], "Countdown(0) should yield nothing"

    def test_countdown_rejects_negative_start(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.Countdown(-1)

    def test_countdown_is_its_own_one_shot_iterator(self, main: ModuleType) -> None:
        c = main.Countdown(2)
        assert iter(c) is c, "iter() of an iterator must return the iterator itself"
        assert next(c) == 2
        assert list(c) == [1], "remaining items should be consumed from the same object"
        assert list(c) == [], "a one-shot iterator must not restart"

    def test_countdown_stays_exhausted(self, main: ModuleType) -> None:
        c = main.Countdown(1)
        assert next(c) == 1
        with pytest.raises(StopIteration):
            next(c)
        with pytest.raises(StopIteration):
            next(c)
        assert next(c, "done") == "done", "default should be returned once exhausted"

    def test_dataset_is_re_iterable(self, main: ModuleType) -> None:
        ds = main.Dataset([1, 2, 3])
        assert list(ds) == [1, 2, 3], "first pass should see every record"
        assert list(ds) == [1, 2, 3], "a Dataset must be re-iterable"

    def test_dataset_hands_out_independent_one_shot_readers(self, main: ModuleType) -> None:
        ds = main.Dataset(["a", "b"])
        r1 = iter(ds)
        r2 = iter(ds)
        assert r1 is not r2, "each iter(dataset) must return a fresh reader"
        assert next(r1) == "a"
        assert next(r2) == "a", "advancing one reader must not advance the other"
        assert list(r1) == ["b"]
        assert list(r2) == ["b"]

    def test_reader_is_one_shot(self, main: ModuleType) -> None:
        reader = iter(main.Dataset([1]))
        assert iter(reader) is reader, "a reader is an iterator; iter() returns itself"
        assert list(reader) == [1]
        assert list(reader) == [], "an exhausted reader must not restart"

    def test_dataset_len_and_generator_input(self, main: ModuleType) -> None:
        assert len(main.Dataset(range(4))) == 4
        assert len(main.Dataset([])) == 0
        ds = main.Dataset(x * x for x in range(3))
        assert list(ds) == [0, 1, 4], "generator input should be snapshotted"
        assert list(ds) == [0, 1, 4], "generator input must still be re-iterable"

    def test_dataset_snapshots_its_input(self, main: ModuleType) -> None:
        src = [1, 2]
        ds = main.Dataset(src)
        src.append(3)
        assert list(ds) == [1, 2], "later mutation of the source must not leak into the Dataset"


# ---------------------------------------------------------------------------
# Part 2: generator functions, lazy log pipeline
# ---------------------------------------------------------------------------


class TestPart2LazyLogPipeline:
    def test_levels_are_ordered_by_severity(self, main: ModuleType) -> None:
        assert tuple(main.LEVELS) == ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

    def test_parse_line_extracts_fields_and_keeps_message_spacing(self, main: ModuleType) -> None:
        rec = main.parse_line("  2026-03-01T12:05:00+00:00 WARNING web-1 disk  at 91%  \n")
        assert rec.ts == datetime(2026, 3, 1, 12, 5, tzinfo=UTC), "timestamp should be parsed tz-aware"
        assert rec.level == "WARNING"
        assert rec.server == "web-1"
        assert rec.message == "disk  at 91%", "message keeps internal spacing, loses outer whitespace"
        assert rec.region is None

    def test_parse_line_rejects_naive_timestamp(self, main: ModuleType) -> None:
        with pytest.raises(main.LogParseError):
            main.parse_line("2026-03-01T12:05:00 INFO web-1 no zone")

    def test_parse_line_rejects_unknown_level_and_missing_fields(self, main: ModuleType) -> None:
        with pytest.raises(main.LogParseError):
            main.parse_line(log_line(0, "LOUD", "web-1", "boom"))
        with pytest.raises(main.LogParseError):
            main.parse_line("2026-03-01T12:05:00+00:00 INFO web-1")
        assert issubclass(main.LogParseError, ValueError)

    def test_parse_records_skips_blanks_and_reports_line_numbers(self, main: ModuleType) -> None:
        lines = [log_line(0, "INFO", "web-1", "ok"), "", "   ", "garbage"]
        gen = main.parse_records(lines)
        first = next(gen)
        assert first.message == "ok", "the valid record should be yielded before the bad line is reached"
        with pytest.raises(main.LogParseError) as info:
            next(gen)
        assert info.value.lineno == 4, "lineno should be the 1-based position in the input"
        assert "4" in str(info.value), "the message should mention the line number"

    def test_parse_records_is_lazy(self, main: ModuleType) -> None:
        src = Recorder([log_line(i, "INFO", "web-1", "x") for i in range(5)])
        gen = main.parse_records(src)
        assert src.pulled == 0, "creating the generator must not read the source"
        next(gen)
        assert src.pulled == 1, "one record must cost exactly one line"

    def test_filter_level_keeps_at_or_above_minimum(self, main: ModuleType) -> None:
        records = [make_record(main, level=lvl) for lvl in main.LEVELS]
        kept = [r.level for r in main.filter_level(records, "WARNING")]
        assert kept == ["WARNING", "ERROR", "CRITICAL"], "filter should keep severities >= WARNING"

    def test_filter_level_validates_minimum_eagerly(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.filter_level([], "LOUD")

    def test_enrich_sets_region_without_mutating_input(self, main: ModuleType) -> None:
        records = [make_record(main, server="web-1"), make_record(main, server="db-9")]
        out = list(main.enrich(records, {"web-1": "eu-west"}))
        assert [r.region for r in out] == ["eu-west", "unknown"], "unknown servers get 'unknown'"
        assert records[0].region is None, "enrich must return new records, not mutate inputs"

    def test_first_error_stops_at_first_match(self, main: ModuleType) -> None:
        lines = [log_line(i, "ERROR" if i == 2 else "INFO", "web-1", f"m{i}") for i in range(100)]
        src = Recorder(lines)
        found = main.first_error(main.parse_records(src))
        assert found is not None and found.message == "m2", "first ERROR record should be returned"
        assert src.pulled == 3, "search must stop right after the first match"

    def test_first_error_returns_none_when_absent(self, main: ModuleType) -> None:
        assert main.first_error([make_record(main, level="WARNING")]) is None
        assert main.first_error([]) is None

    def test_pipeline_consumes_only_what_is_needed(self, main: ModuleType) -> None:
        lines = [log_line(i % 60, "ERROR" if i % 3 == 0 else "INFO", "web-1", f"m{i}")
                 for i in range(300)]
        src = Recorder(lines)
        pipeline = main.enrich(
            main.filter_level(main.parse_records(src), "ERROR"),
            {"web-1": "eu-west"},
        )
        first_two = list(itertools.islice(pipeline, 2))
        assert [r.message for r in first_two] == ["m0", "m3"], "the first two ERROR records should come out"
        assert all(r.region == "eu-west" for r in first_two), "enrichment should apply lazily too"
        assert src.pulled == 4, "only lines 0..3 should have been read from the source"


# ---------------------------------------------------------------------------
# Part 3: lazily paginated API client
# ---------------------------------------------------------------------------


class TestPart3LazyPagination:
    def test_paginate_yields_items_across_pages_in_order(self, main: ModuleType) -> None:
        pages = {None: ((1, 2), "p2"), "p2": ((3,), "p3"), "p3": ((4, 5), None)}
        calls: list[Any] = []
        items = list(main.paginate(scripted_api(main, pages, calls)))
        assert items == [1, 2, 3, 4, 5]
        assert calls == [None, "p2", "p3"], "each page should be fetched exactly once, in cursor order"

    def test_paginate_fetches_pages_only_on_demand(self, main: ModuleType) -> None:
        pages = {None: ((1, 2), "p2"), "p2": ((3,), None)}
        calls: list[Any] = []
        it = main.paginate(scripted_api(main, pages, calls))
        assert calls == [], "calling paginate() must not fetch anything"
        assert next(it) == 1
        assert calls == [None], "the first next() fetches exactly one page"
        assert next(it) == 2
        assert calls == [None], "items from the current page must not trigger a fetch"
        assert next(it) == 3
        assert calls == [None, "p2"], "the next page is fetched only when its items are needed"

    def test_paginate_skips_empty_intermediate_pages(self, main: ModuleType) -> None:
        pages = {None: ((), "p2"), "p2": ((5,), None)}
        assert list(main.paginate(scripted_api(main, pages, []))) == [5]

    def test_start_cursor_is_honoured(self, main: ModuleType) -> None:
        pages = {None: ((1,), "p2"), "p2": ((3,), None)}
        calls: list[Any] = []
        assert list(main.paginate(scripted_api(main, pages, calls), start="p2")) == [3]
        assert calls == ["p2"], "resuming from a cursor must not refetch earlier pages"

    def test_iter_pages_yields_page_objects_including_last(self, main: ModuleType) -> None:
        pages = {None: ((1,), "p2"), "p2": ((2,), None)}
        out = list(main.iter_pages(scripted_api(main, pages, [])))
        assert [p.items for p in out] == [(1,), (2,)], "Page.items should be the tuples returned by the API"
        assert [p.next_cursor for p in out] == ["p2", None]

    def test_fetch_error_propagates_after_earlier_items(self, main: ModuleType) -> None:
        pages = {None: ((1, 2), "p2"), "p2": ((3,), None)}
        it = main.paginate(scripted_api(main, pages, [], failing="p2"))
        assert next(it) == 1
        assert next(it) == 2
        with pytest.raises(TimeoutError):
            next(it)
        with pytest.raises(StopIteration):
            next(it)

    def test_cursor_loop_is_detected(self, main: ModuleType) -> None:
        pages = {None: ((1,), "a"), "a": ((2,), "b"), "b": ((3,), "a")}
        calls: list[Any] = []
        collected: list[Any] = []
        with pytest.raises(main.PaginationLoopError):
            for item in main.paginate(scripted_api(main, pages, calls)):
                collected.append(item)
        assert collected == [1, 2, 3], "items from pages before the loop should still be yielded"
        assert calls == [None, "a", "b"], "a repeated cursor must not be requested again"
        assert issubclass(main.PaginationLoopError, RuntimeError)


# ---------------------------------------------------------------------------
# Part 4: yield from, return values, resource-owning generators
# ---------------------------------------------------------------------------


class TestPart4YieldFromAndResources:
    def test_counted_yields_items_and_returns_count(self, main: ModuleType) -> None:
        assert main.drain(main.counted(["x", "y"])) == (["x", "y"], 2)
        assert main.drain(main.counted([])) == ([], 0)

    def test_chain_counts_delegates_return_values(self, main: ModuleType) -> None:
        items, counts = main.drain(main.chain_counts([1, 2], [], [3]))
        assert items == [1, 2, 3], "items should come out of every source in order"
        assert counts == [2, 0, 1], "each yield from must hand back that source's count"

    def test_drain_returns_none_for_plain_generators(self, main: ModuleType) -> None:
        def plain() -> Iterator[int]:
            yield 1

        assert main.drain(plain()) == ([1], None)

    def test_read_batches_groups_lines_and_closes_on_exhaustion(self, main: ModuleType) -> None:
        res = FakeResource(["a", "b", "c", "d", "e"])
        batches = list(main.read_batches(lambda: res, 2))
        assert batches == [["a", "b"], ["c", "d"], ["e"]], "final batch may be partial"
        assert res.closed, "the resource must be released after exhaustion"

    def test_read_batches_opens_resource_lazily(self, main: ModuleType) -> None:
        res = FakeResource(["a", "b"])
        gen = main.read_batches(lambda: res, 2)
        assert res.opened == 0, "calling read_batches() must not open the resource"
        assert next(gen) == ["a", "b"]
        assert res.opened == 1

    def test_read_batches_releases_resource_on_early_close(self, main: ModuleType) -> None:
        res = FakeResource(["a", "b", "c", "d"])
        with closing(main.read_batches(lambda: res, 2)) as batches:
            assert next(batches) == ["a", "b"]
            assert not res.closed, "resource should still be open mid-iteration"
        assert res.closed, "close() on the generator must release the resource"

    def test_read_batches_releases_resource_on_error(self, main: ModuleType) -> None:
        def broken() -> Iterator[str]:
            yield "a"
            yield "b"
            raise OSError("disk gone")

        res = FakeResource(broken())
        with pytest.raises(OSError, match="disk gone"):
            list(main.read_batches(lambda: res, 2))
        assert res.closed, "an error mid-read must still release the resource"

    def test_read_batches_rejects_bad_size_eagerly(self, main: ModuleType) -> None:
        res = FakeResource([])
        with pytest.raises(ValueError):
            main.read_batches(lambda: res, 0)
        assert res.opened == 0


# ---------------------------------------------------------------------------
# Part 5: send / throw / close
# ---------------------------------------------------------------------------


class TestPart5CoroutineStats:
    def test_generator_is_primed_so_send_works_immediately(self, main: ModuleType) -> None:
        stats = main.running_stats()
        assert stats.send(4.0) == main.Stats(count=1, mean=4.0, minimum=4.0, maximum=4.0)

    def test_send_accumulates_count_mean_min_max(self, main: ModuleType) -> None:
        stats = main.running_stats()
        stats.send(1.0)
        stats.send(2.0)
        final = stats.send(3.0)
        assert final.count == 3
        assert final.mean == pytest.approx(2.0)
        assert final.minimum == 1.0
        assert final.maximum == 3.0

    def test_mean_is_numerically_stable_for_large_offsets(self, main: ModuleType) -> None:
        stats = main.running_stats()
        base = 1e9
        for value in (base + 1, base + 2, base + 3):
            result = stats.send(value)
        assert result.mean == pytest.approx(base + 2, rel=1e-12)

    def test_throw_reset_clears_state_and_returns_empty_stats(self, main: ModuleType) -> None:
        stats = main.running_stats()
        stats.send(10.0)
        stats.send(20.0)
        cleared = stats.throw(main.StatsReset())
        assert cleared == main.Stats(0, None, None, None), "throw(StatsReset) should return the empty state"
        after = stats.send(5.0)
        assert after == main.Stats(count=1, mean=5.0, minimum=5.0, maximum=5.0), "state should restart from zero"

    def test_other_thrown_exceptions_propagate_and_finish(self, main: ModuleType) -> None:
        stats = main.running_stats()
        stats.send(1.0)
        with pytest.raises(ValueError, match="boom"):
            stats.throw(ValueError("boom"))
        with pytest.raises(StopIteration):
            stats.send(2.0)

    def test_close_runs_cleanup_with_final_stats(self, main: ModuleType) -> None:
        seen: list[Any] = []
        stats = main.running_stats(on_close=seen.append)
        stats.send(2.0)
        stats.send(4.0)
        stats.close()
        assert seen == [main.Stats(count=2, mean=3.0, minimum=2.0, maximum=4.0)], (
            "on_close should be called exactly once with the final stats"
        )
        with pytest.raises(StopIteration):
            next(stats)

    def test_close_without_callback_is_silent(self, main: ModuleType) -> None:
        stats = main.running_stats()
        stats.send(1.0)
        stats.close()


# ---------------------------------------------------------------------------
# Part 6: the itertools toolkit
# ---------------------------------------------------------------------------


class TestPart6ItertoolsToolkit:
    def test_write_in_batches_sizes_and_total(self, main: ModuleType) -> None:
        batches: list[list[int]] = []

        def write(batch: list[int]) -> int:
            batches.append(batch)
            return len(batch)

        total = main.write_in_batches(range(7), 3, write)
        assert batches == [[0, 1, 2], [3, 4, 5], [6]], "batches should be plain lists of at most size items"
        assert all(isinstance(b, list) for b in batches)
        assert total == 7, "the result should be the sum of write()'s return values"

    def test_write_in_batches_pulls_rows_lazily(self, main: ModuleType) -> None:
        src = Recorder(range(10))
        pulled_at_call: list[int] = []

        def write(batch: list[int]) -> int:
            pulled_at_call.append(src.pulled)
            return len(batch)

        main.write_in_batches(src, 3, write)
        assert pulled_at_call == [3, 6, 9, 10], "each batch should be the only thing read ahead"

    def test_write_in_batches_rejects_bad_size_and_skips_empty(self, main: ModuleType) -> None:
        calls: list[Any] = []

        def write(batch: list[Any]) -> int:
            calls.append(batch)
            return len(batch)

        with pytest.raises(ValueError):
            main.write_in_batches([1], 0, write)
        assert main.write_in_batches([], 2, write) == 0
        assert calls == [], "write() must not be called for an empty source"

    def test_moving_average_emits_only_full_windows(self, main: ModuleType) -> None:
        assert list(main.moving_average([1, 2, 3, 4, 5], 3)) == pytest.approx([2, 3, 4])
        assert list(main.moving_average([1, 2], 3)) == [], "no full window, no output"
        assert list(main.moving_average([7, 8], 1)) == pytest.approx([7, 8])

    def test_moving_average_is_lazy_and_validates_eagerly(self, main: ModuleType) -> None:
        src = Recorder(range(100))
        next(main.moving_average(src, 3))
        assert src.pulled == 3, "the first average needs exactly one window of input"
        with pytest.raises(ValueError):
            main.moving_average([1], 0)

    def test_group_by_server_groups_consecutive_runs_only(self, main: ModuleType) -> None:
        records = [make_record(main, server=s, message=f"m{i}")
                   for i, s in enumerate(["web-1", "web-1", "db-1", "web-1"])]
        groups = [(server, [r.message for r in group])
                  for server, group in main.group_by_server(records)]
        assert groups == [("web-1", ["m0", "m1"]), ("db-1", ["m2"]), ("web-1", ["m3"])], (
            "non-adjacent runs of the same server must form separate groups"
        )
        assert list(main.group_by_server([])) == []

    def test_gaps_between_adjacent_timestamps(self, main: ModuleType) -> None:
        t0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
        stamps = [t0, t0 + timedelta(seconds=5), t0 + timedelta(minutes=1)]
        assert list(main.gaps(stamps)) == [timedelta(seconds=5), timedelta(seconds=55)]
        assert list(main.gaps(stamps[:1])) == [], "a single timestamp has no gaps"

    def test_leading_below_stops_at_first_breach(self, main: ModuleType) -> None:
        src = Recorder([1, 2, 5, 1, 0])
        assert main.leading_below(src, 3) == [1, 2]
        assert src.pulled == 3, "the breaching value is read, but nothing after it"

    def test_running_peak_and_flatten(self, main: ModuleType) -> None:
        assert list(main.running_peak([1, 3, 2, 5, 4])) == [1, 3, 3, 5, 5]
        assert list(main.running_peak([])) == []
        assert list(main.flatten([[1, 2], [], [3]])) == [1, 2, 3]

    def test_flatten_is_lazy_in_the_outer_iterable(self, main: ModuleType) -> None:
        touched: list[int] = []

        def batches() -> Iterator[list[int]]:
            for i in range(3):
                touched.append(i)
                yield [i, i]

        assert next(main.flatten(batches())) == 0
        assert touched == [0], "the second batch must not be produced for the first item"

    def test_independent_views_are_full_copies_and_share_lazily(self, main: ModuleType) -> None:
        src = Recorder(range(5))
        v1, v2 = main.independent_views(src, 2)
        assert next(v1) == 0
        assert src.pulled == 1, "advancing one view should read just one item from the source"
        assert list(v1) == [1, 2, 3, 4]
        assert list(v2) == [0, 1, 2, 3, 4], "the other view must still see the full sequence"
        with pytest.raises(ValueError):
            main.independent_views([1], 0)


# ---------------------------------------------------------------------------
# Part 7: heapq merge of time-ordered streams
# ---------------------------------------------------------------------------


class TestPart7HeapqMerge:
    def test_merge_streams_interleaves_by_timestamp(self, main: ModuleType) -> None:
        a = [make_record(main, minute=m, server="a") for m in (0, 4, 8)]
        b = [make_record(main, minute=m, server="b") for m in (1, 2, 9)]
        c = [make_record(main, minute=m, server="c") for m in (3,)]
        merged = [r.server for r in main.merge_streams(a, b, c)]
        assert merged == ["a", "b", "b", "c", "a", "a", "b"], "output should be globally time-ordered"

    def test_merge_streams_is_stable_on_ties(self, main: ModuleType) -> None:
        first = [make_record(main, minute=0, server="a")]
        second = [make_record(main, minute=0, server="b")]
        assert [r.server for r in main.merge_streams(first, second)] == ["a", "b"], (
            "on equal timestamps the earlier stream argument must come first"
        )

    def test_merge_streams_works_on_infinite_inputs(self, main: ModuleType) -> None:
        evens = (make_record(main, minute=m, server="even") for m in itertools.count(0, 2))
        odds = (make_record(main, minute=m, server="odd") for m in itertools.count(1, 2))
        first_four = list(itertools.islice(main.merge_streams(evens, odds), 4))
        assert [r.ts.minute for r in first_four] == [0, 1, 2, 3], "merge must stay lazy"

    def test_merge_streams_with_no_streams_is_empty(self, main: ModuleType) -> None:
        assert list(main.merge_streams()) == []

    def test_top_servers_orders_by_count(self, main: ModuleType) -> None:
        records = (
            [make_record(main, server="web-1")] * 3
            + [make_record(main, server="db-1")]
            + [make_record(main, server="web-2")] * 2
        )
        assert main.top_servers(records, 2) == [("web-1", 3), ("web-2", 2)]
        assert main.top_servers(records, 0) == []
        assert main.top_servers(records, 10) == [("web-1", 3), ("web-2", 2), ("db-1", 1)]

    def test_top_servers_rejects_negative_k(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.top_servers([], -1)
