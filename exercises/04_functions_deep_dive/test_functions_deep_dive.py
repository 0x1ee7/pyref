"""Tests for the functions deep dive exercise. See README.md for the full spec."""

from __future__ import annotations

import dataclasses
import datetime as dt
import functools
import hashlib
import logging
from decimal import Decimal
from enum import Enum
from types import ModuleType

import pytest


class FakeClock:
    """Manually advanced clock; also usable as a sleep function that advances it."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class TestPart1Closures:
    def test_multiplier_closes_over_factor(self, main: ModuleType) -> None:
        triple = main.make_multiplier(3)
        double = main.make_multiplier(2)
        assert triple(4) == 12, "make_multiplier(3)(4) should be 12"
        assert double(4) == 8, "each multiplier must keep its own factor"

    def test_counter_yields_start_then_increments(self, main: ModuleType) -> None:
        counter = main.make_counter(5)
        assert [counter(), counter(), counter()] == [5, 6, 7], "counter should yield start, start+1, ..."
        assert main.make_counter()() == 0, "make_counter() should default to start=0"

    def test_compose_applies_left_to_right(self, main: ModuleType) -> None:
        pipeline = main.compose(str.strip, str.upper, lambda s: s + "!")
        assert pipeline("  hi ") == "HI!", "compose must apply the first function first"
        assert main.compose()(7) == 7, "compose() with no functions must be identity"

    def test_compose_rejects_non_callable_eagerly(self, main: ModuleType) -> None:
        with pytest.raises(TypeError):
            main.compose(len, 3)

    def test_make_adders_avoids_late_binding(self, main: ModuleType) -> None:
        adders = main.make_adders(4)
        assert [add(10) for add in adders] == [10, 11, 12, 13], (
            "adder i must add i; a bare loop closure would give [13, 13, 13, 13]"
        )


class TestPart2Decorators:
    def test_bare_and_called_forms_both_work(self, main: ModuleType, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)
        log = logging.getLogger("tests.timed.bare")
        clock = iter([0.0, 1.5, 2.0, 2.25])

        @main.timed(logger=log, clock=clock.__next__)
        def work(x: int) -> int:
            return x + 1

        assert work(1) == 2, "called form @timed(...) should return the wrapped result"
        assert work(2) == 3
        messages = [r.getMessage() for r in caplog.records if r.name == "tests.timed.bare"]
        assert len(messages) == 2, "one log record per call expected"
        assert messages[0].endswith("took 1.500s"), f"unexpected message {messages[0]!r}"
        assert messages[1].endswith("took 0.250s"), f"unexpected message {messages[1]!r}"

    def test_bare_decorator_form(self, main: ModuleType, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)

        @main.timed
        def ping() -> str:
            return "pong"

        assert ping() == "pong", "bare @timed must return the wrapped result"
        assert any("ping" in r.getMessage() for r in caplog.records), "bare @timed should log at DEBUG by default"

    def test_level_is_configurable(self, main: ModuleType, caplog: pytest.LogCaptureFixture) -> None:
        log = logging.getLogger("tests.timed.level")
        clock = iter([0.0, 0.1])

        @main.timed(logger=log, level=logging.INFO, clock=clock.__next__)
        def job() -> None:
            return None

        with caplog.at_level(logging.INFO, logger="tests.timed.level"):
            job()
        assert [r.levelno for r in caplog.records if r.name == "tests.timed.level"] == [logging.INFO]

    def test_failing_call_is_timed_and_exception_propagates(
        self, main: ModuleType, caplog: pytest.LogCaptureFixture
    ) -> None:
        log = logging.getLogger("tests.timed.fail")
        clock = iter([0.0, 0.5])

        @main.timed(logger=log, clock=clock.__next__)
        def explode() -> None:
            raise ValueError("kaboom")

        with caplog.at_level(logging.DEBUG, logger="tests.timed.fail"), pytest.raises(ValueError, match="kaboom"):
            explode()
        assert any(r.getMessage().endswith("took 0.500s") for r in caplog.records), (
            "the elapsed time must be logged even when the call raises"
        )

    def test_wrapper_preserves_metadata(self, main: ModuleType) -> None:
        def original(a: int) -> int:
            """Doubles a."""
            return a * 2

        wrapped = main.timed(logger=logging.getLogger("tests.meta"))(original)
        assert wrapped.__name__ == "original", "__name__ must be copied by functools.wraps"
        assert wrapped.__qualname__ == original.__qualname__
        assert wrapped.__doc__ == "Doubles a.", "__doc__ must be copied"
        assert wrapped.__wrapped__ is original, "__wrapped__ must point at the undecorated function"
        assert wrapped(4) == 8

    def test_decorator_rejects_non_callable_target(self, main: ModuleType) -> None:
        with pytest.raises(TypeError):
            main.timed(42)


class TestPart3Retry:
    def test_bare_and_keyword_forms(self, main: ModuleType) -> None:
        calls = {"n": 0}

        @main.retry
        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("try again")
            return "ok"

        assert flaky() == "ok", "bare @retry should use attempts=3 and succeed on the third call"

        many = {"n": 0}

        @main.retry(attempts=5, sleep=lambda _s: None)
        def flakier() -> str:
            many["n"] += 1
            if many["n"] < 5:
                raise ConnectionError("again")
            return "fine"

        assert flakier() == "fine"
        assert many["n"] == 5

    def test_gives_up_and_reraises_last_error(self, main: ModuleType) -> None:
        calls: list[int] = []

        @main.retry(attempts=3, sleep=lambda _s: None)
        def always_fails() -> None:
            calls.append(len(calls) + 1)
            raise ValueError(f"boom {len(calls)}")

        with pytest.raises(ValueError, match="boom 3"):
            always_fails()
        assert calls == [1, 2, 3], "attempts is the total number of calls, including the first"

    def test_only_listed_exceptions_are_retried(self, main: ModuleType) -> None:
        calls = {"n": 0}

        @main.retry(attempts=4, exceptions=(KeyError,), sleep=lambda _s: None)
        def wrong_error() -> None:
            calls["n"] += 1
            raise ValueError("not retryable")

        with pytest.raises(ValueError):
            wrong_error()
        assert calls["n"] == 1, "non-matching exceptions must propagate on the first attempt"

    def test_backoff_is_exponential_and_injected_sleep_is_used(self, main: ModuleType) -> None:
        sleeps: list[float] = []

        @main.retry(attempts=4, backoff=0.5, sleep=sleeps.append)
        def always_fails() -> None:
            raise OSError("down")

        with pytest.raises(OSError):
            always_fails()
        assert sleeps == [0.5, 1.0, 2.0], "delays should be backoff * 2**(attempt-1), one per gap"

    def test_no_sleep_after_success_or_final_failure(self, main: ModuleType) -> None:
        sleeps: list[float] = []

        @main.retry(attempts=3, backoff=1.0, sleep=sleeps.append)
        def fine() -> int:
            return 1

        assert fine() == 1
        assert sleeps == [], "a first-try success must not sleep"

    def test_invalid_configuration(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.retry(attempts=0)
        with pytest.raises(TypeError):
            main.retry(3)  # type: ignore[call-overload]

    def test_wrapper_preserves_name_and_arguments(self, main: ModuleType) -> None:
        @main.retry(attempts=2)
        def add(a: int, *, b: int = 0) -> int:
            return a + b

        assert add.__name__ == "add"
        assert add(1, b=2) == 3, "positional and keyword arguments must pass through"


class TestPart3Stacking:
    def test_timed_outside_retry_includes_sleeps(self, main: ModuleType, caplog: pytest.LogCaptureFixture) -> None:
        clock = FakeClock()
        log = logging.getLogger("tests.stack.outer")
        attempts = {"n": 0}

        @main.timed(logger=log, clock=clock)
        @main.retry(attempts=3, backoff=1.0, sleep=clock.sleep)
        def flaky() -> str:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ConnectionError("retry me")
            return "done"

        with caplog.at_level(logging.DEBUG, logger="tests.stack.outer"):
            assert flaky() == "done"
        messages = [r.getMessage() for r in caplog.records if r.name == "tests.stack.outer"]
        assert len(messages) == 1, "timed outermost logs once per decorated call"
        assert messages[0].endswith("took 3.000s"), f"sleeps of 1.0 + 2.0 should be inside the timing: {messages[0]!r}"

    def test_retry_outside_timed_logs_every_attempt(self, main: ModuleType, caplog: pytest.LogCaptureFixture) -> None:
        log = logging.getLogger("tests.stack.inner")
        attempts = {"n": 0}

        @main.retry(attempts=2, sleep=lambda _s: None)
        @main.timed(logger=log, clock=FakeClock())
        def flaky() -> str:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ConnectionError("first try fails")
            return "ok"

        with caplog.at_level(logging.DEBUG, logger="tests.stack.inner"):
            assert flaky() == "ok"
        assert len([r for r in caplog.records if r.name == "tests.stack.inner"]) == 2, (
            "retry outermost means timed sees each attempt"
        )


class TestPart4Introspection:
    def test_validate_args_accepts_and_rejects_by_annotation(self, main: ModuleType) -> None:
        @main.validate_args
        def scale(value: float, factor: int) -> float:
            return value * factor

        assert scale(1.5, 2) == 3.0
        with pytest.raises(TypeError, match="'factor'"):
            scale(1.5, "2")  # type: ignore[arg-type]

    def test_union_and_none(self, main: ModuleType) -> None:
        @main.validate_args
        def label(text: str | None = None) -> str:
            return text or "anon"

        assert label(None) == "anon"
        assert label("x") == "x"
        with pytest.raises(TypeError, match="'text'"):
            label(3)  # type: ignore[arg-type]

    def test_container_origin_and_varargs(self, main: ModuleType) -> None:
        @main.validate_args
        def tags(names: list[str], *nums: int) -> int:
            return len(names) + sum(nums)

        assert tags(["a"], 1, 2) == 4
        with pytest.raises(TypeError):
            tags(("a",), 1)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="'nums'"):
            tags(["a"], 1, "2")  # type: ignore[arg-type]

    def test_return_annotation_is_checked(self, main: ModuleType) -> None:
        @main.validate_args
        def broken() -> str:
            return 3  # type: ignore[return-value]

        with pytest.raises(TypeError, match="return"):
            broken()

    def test_unannotated_and_unsupplied_defaults_are_not_checked(self, main: ModuleType) -> None:
        @main.validate_args
        def f(x, y: int = None) -> int:  # type: ignore[assignment]
            return 1

        assert f("anything") == 1, "unannotated parameters are not enforced"
        assert f(None) == 1, "default values that the caller did not pass are not checked"

    def test_signature_summary_of_plain_function(self, main: ModuleType) -> None:
        def f(a, b=2, *, c=3):  # type: ignore[no-untyped-def]
            return a

        summary = main.signature_summary(f)
        assert summary["name"] == "f"
        assert summary["parameters"] == [
            ("a", "POSITIONAL_OR_KEYWORD", False),
            ("b", "POSITIONAL_OR_KEYWORD", True),
            ("c", "KEYWORD_ONLY", True),
        ]
        assert summary["defaults"] == (2,), "__defaults__ holds only positional defaults"
        assert summary["wrapped_depth"] == 0

    def test_wrapper_signature_follows_wrapped_by_default(self, main: ModuleType) -> None:
        def inner(a: int, b: int = 1) -> int:
            return a + b

        wrapper = functools.wraps(inner)(lambda *args, **kwargs: inner(*args, **kwargs))

        followed = main.signature_summary(wrapper)
        assert [p[0] for p in followed["parameters"]] == ["a", "b"], (
            "inspect.signature follows __wrapped__ by default"
        )
        raw = main.signature_summary(wrapper, follow_wrapped=False)
        assert raw["parameters"] == [("args", "VAR_POSITIONAL", False), ("kwargs", "VAR_KEYWORD", False)], (
            "without following __wrapped__ the wrapper's own *args/**kwargs show"
        )
        assert raw["defaults"] is None, "functools.wraps does not copy __defaults__"

    def test_wrapped_depth_counts_chain(self, main: ModuleType) -> None:
        def base(x: int) -> int:
            return x

        stacked = main.timed(logger=logging.getLogger("tests.depth"))(
            main.retry(base, attempts=2, sleep=lambda _s: None)
        )
        assert main.signature_summary(stacked)["wrapped_depth"] == 2


class TestPart5MethodsAndClasses:
    def test_call_counter_counts_and_resets(self, main: ModuleType) -> None:
        def add(a: int, b: int) -> int:
            """Add two numbers."""
            return a + b

        counter = main.CallCounter(add)
        assert counter(1, 2) == 3
        assert counter(3, 4) == 7
        assert counter.count == 2
        assert counter.__wrapped__ is add, "update_wrapper should set __wrapped__"
        assert counter.__name__ == "add"
        counter.reset()
        assert counter.count == 0

    def test_call_counter_binds_self_on_methods(self, main: ModuleType) -> None:
        class Greeter:
            def __init__(self, prefix: str) -> None:
                self.prefix = prefix

            @main.CallCounter
            def greet(self, name: str) -> str:
                return f"{self.prefix} {name}"

        assert Greeter("hi").greet("ada") == "hi ada", "__get__ must bind the instance as self"
        assert Greeter.greet.count == 1, "class-level access should return the counter itself"

    def test_call_counter_count_is_shared_across_instances(self, main: ModuleType) -> None:
        class Greeter:
            @main.CallCounter
            def greet(self) -> str:
                return "hi"

        Greeter().greet()
        Greeter().greet()
        assert Greeter.greet.count == 2, "the decorator object is shared, so counts are too"

    def test_rate_limited_allows_then_rejects(self, main: ModuleType) -> None:
        clock = FakeClock()

        class Client:
            @main.rate_limited(2, 10.0, clock=clock)
            def fetch(self) -> str:
                return "data"

        client = Client()
        assert client.fetch() == "data"
        assert client.fetch() == "data"
        with pytest.raises(main.RateLimitExceeded) as info:
            client.fetch()
        assert info.value.retry_after == pytest.approx(10.0)
        assert isinstance(info.value, RuntimeError)

    def test_rate_limited_window_slides(self, main: ModuleType) -> None:
        clock = FakeClock()

        class Client:
            @main.rate_limited(2, 10.0, clock=clock)
            def fetch(self) -> str:
                return "data"

        client = Client()
        client.fetch()
        clock.advance(1.0)
        client.fetch()
        clock.advance(8.0)  # now t=9: the t=0 call is still inside the window
        with pytest.raises(main.RateLimitExceeded) as info:
            client.fetch()
        assert info.value.retry_after == pytest.approx(1.0), "retry_after is when the oldest call expires"
        clock.advance(1.0)  # t=10: the t=0 call has expired, one slot is free
        assert client.fetch() == "data"

    def test_rejected_calls_are_not_recorded(self, main: ModuleType) -> None:
        clock = FakeClock()

        class Client:
            @main.rate_limited(2, 10.0, clock=clock)
            def fetch(self) -> str:
                return "data"

        client = Client()
        client.fetch()
        client.fetch()
        clock.advance(1.0)
        with pytest.raises(main.RateLimitExceeded):
            client.fetch()
        clock.advance(1.0)
        with pytest.raises(main.RateLimitExceeded) as info:
            client.fetch()
        assert info.value.retry_after == pytest.approx(8.0), "rejected attempts must not extend the window"

    def test_rate_limit_is_per_instance(self, main: ModuleType) -> None:
        clock = FakeClock()

        class Client:
            @main.rate_limited(1, 5.0, clock=clock)
            def fetch(self) -> str:
                return "data"

        a, b = Client(), Client()
        a.fetch()
        assert b.fetch() == "data", "instances must not share a budget"
        with pytest.raises(main.RateLimitExceeded):
            a.fetch()

    def test_rate_limited_validates_and_preserves_name(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.rate_limited(0, 1.0)
        with pytest.raises(ValueError):
            main.rate_limited(1, 0.0)

        class Client:
            @main.rate_limited(1, 1.0)
            def fetch(self) -> None:
                """Fetch things."""

        assert Client.fetch.__name__ == "fetch"
        assert Client.fetch.__doc__ == "Fetch things."


class TestPart6Functools:
    def test_ttl_cache_memoises_within_ttl(self, main: ModuleType) -> None:
        clock = FakeClock()
        calls: list[int] = []

        @main.ttl_cache(10.0, clock=clock)
        def square(x: int) -> int:
            calls.append(x)
            return x * x

        assert square(3) == 9
        assert square(3) == 9
        assert calls == [3], "second call inside the TTL must come from the cache"

    def test_ttl_cache_expires_entries(self, main: ModuleType) -> None:
        clock = FakeClock()
        calls: list[int] = []

        @main.ttl_cache(10.0, clock=clock)
        def square(x: int) -> int:
            calls.append(x)
            return x * x

        square(3)
        clock.advance(9.5)
        square(3)
        assert len(calls) == 1, "entry is still valid just before expiry"
        clock.advance(0.5)
        square(3)
        assert len(calls) == 2, "entry must be recomputed once clock() reaches stored + seconds"

    def test_cache_info_and_cache_clear(self, main: ModuleType) -> None:
        clock = FakeClock()

        @main.ttl_cache(60.0, clock=clock, maxsize=8)
        def double(x: int) -> int:
            return 2 * x

        double(1)
        double(1)
        double(2)
        info = double.cache_info()
        assert (info.hits, info.misses, info.maxsize, info.currsize) == (1, 2, 8, 2)
        double.cache_clear()
        assert tuple(double.cache_info()) == (0, 0, 8, 0), "cache_clear must reset entries and counters"

    def test_maxsize_evicts_least_recently_used(self, main: ModuleType) -> None:
        clock = FakeClock()
        calls: list[int] = []

        @main.ttl_cache(60.0, clock=clock, maxsize=2)
        def ident(x: int) -> int:
            calls.append(x)
            return x

        ident(1)
        ident(2)
        ident(1)  # hit: 1 becomes most recently used
        ident(3)  # evicts 2, the least recently used
        ident(1)  # still cached
        ident(2)  # was evicted: recomputed
        assert calls == [1, 2, 3, 2], f"unexpected recomputation sequence {calls}"

    def test_unhashable_arguments_raise_type_error(self, main: ModuleType) -> None:
        @main.ttl_cache(5.0, clock=FakeClock())
        def total(values: list[int]) -> int:
            return sum(values)

        with pytest.raises(TypeError):
            total([1, 2])

    def test_ttl_cache_validates_config_and_preserves_name(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.ttl_cache(0)
        with pytest.raises(ValueError):
            main.ttl_cache(1.0, maxsize=0)

        @main.ttl_cache(1.0)
        def named() -> None:
            """Named thing."""

        assert named.__name__ == "named"
        assert named.__doc__ == "Named thing."

    def test_version_parse_and_str(self, main: ModuleType) -> None:
        version = main.Version.parse("v1.2.3")
        assert (version.major, version.minor, version.patch) == (1, 2, 3)
        assert str(version) == "1.2.3"

    @pytest.mark.parametrize("text", ["1.2", "1.2.x", "-1.2.3", "", "1.2.3.4", "١.2.3"])
    def test_version_parse_rejects_malformed(self, main: ModuleType, text: str) -> None:
        with pytest.raises(ValueError):
            main.Version.parse(text)

    def test_version_ordering_is_numeric_and_total(self, main: ModuleType) -> None:
        v = main.Version.parse
        assert v("1.10.0") > v("1.9.9"), "comparison must be numeric, not lexical"
        assert v("1.0.0") <= v("1.0.0")
        assert v("2.0.0") >= v("1.99.99")
        assert sorted([v("1.10.0"), v("1.2.0"), v("0.9.1")]) == [v("0.9.1"), v("1.2.0"), v("1.10.0")]
        assert v("1.0.0") == main.Version(1, 0, 0)
        assert len({v("1.0.0"), main.Version(1, 0, 0)}) == 1, "equal versions must hash equal"

    def test_version_foreign_comparisons(self, main: ModuleType) -> None:
        version = main.Version(1, 0, 0)
        assert (version == "1.0.0") is False, "equality with other types is False, not an error"
        with pytest.raises(TypeError):
            _ = version < 3  # type: ignore[operator]

    def test_release_checksum_is_cached_sha256(self, main: ModuleType) -> None:
        release = main.Release(main.Version(1, 0, 0), ["b.whl", "a.tar.gz"])
        expected = hashlib.sha256("a.tar.gz\nb.whl".encode()).hexdigest()
        assert release.checksum == expected, "checksum is sha256 of the sorted, newline-joined artifacts"
        release.artifacts.append("c.zip")
        assert release.checksum == expected, "cached_property must not recompute after first access"
        assert "checksum" in vars(release), "cached value should live in the instance __dict__"
        assert isinstance(main.Release.checksum, functools.cached_property)

    def test_merge_settings_later_layers_win_and_deep_merge(self, main: ModuleType) -> None:
        base = {"debug": False, "db": {"host": "localhost", "port": 5432}}
        override = {"debug": True, "db": {"port": 6000}}
        merged = main.merge_settings(base, override)
        assert merged == {"debug": True, "db": {"host": "localhost", "port": 6000}}

    def test_merge_settings_does_not_mutate_inputs(self, main: ModuleType) -> None:
        base = {"db": {"port": 5432}}
        override = {"db": {"port": 6000}}
        main.merge_settings(base, override)
        assert base == {"db": {"port": 5432}}, "merge_settings must not mutate its inputs"
        assert main.merge_settings() == {}, "no layers yields an empty dict"

    def test_merge_settings_replaces_scalar_with_mapping(self, main: ModuleType) -> None:
        assert main.merge_settings({"a": 1}, {"a": {"b": 2}}) == {"a": {"b": 2}}

    def test_url_builder_joins_segments(self, main: ModuleType) -> None:
        build = main.make_url_builder("https://api.example.com/")
        assert build("v1", "/users/", "42") == "https://api.example.com/v1/users/42"
        assert build() == "https://api.example.com", "no segments returns the base without a trailing slash"

    def test_url_builder_query_is_sorted_and_encoded(self, main: ModuleType) -> None:
        build = main.make_url_builder("https://api.example.com")
        assert build("items", q="a b", page="2") == "https://api.example.com/items?page=2&q=a+b"

    def test_url_builder_is_partial_and_independent(self, main: ModuleType) -> None:
        a = main.make_url_builder("https://a.example")
        b = main.make_url_builder("https://b.example")
        assert isinstance(a, functools.partial), "make_url_builder should return a functools.partial"
        assert a("x") == "https://a.example/x"
        assert b("x") == "https://b.example/x"


class TestPart7Dispatch:
    @pytest.mark.parametrize("value", [None, True, 3, 2.5, "x"])
    def test_scalars_pass_through(self, main: ModuleType, value: object) -> None:
        result = main.to_jsonish(value)
        assert result == value and type(result) is type(value), "scalars must be returned unchanged"

    def test_decimal_and_datetime_serialisation(self, main: ModuleType) -> None:
        assert main.to_jsonish(Decimal("1.10")) == "1.10", "Decimal must keep its precision as a string"
        stamp = dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)
        assert main.to_jsonish(stamp) == "2024-01-02T03:04:05+00:00", (
            "datetime must win over its date base class"
        )
        assert main.to_jsonish(dt.date(2024, 1, 2)) == "2024-01-02"

    def test_enum_becomes_its_value(self, main: ModuleType) -> None:
        class Color(Enum):
            RED = "red"

        assert main.to_jsonish(Color.RED) == "red"

    def test_dataclass_is_serialised_recursively(self, main: ModuleType) -> None:
        @dataclasses.dataclass
        class Line:
            label: str
            price: Decimal
            tags: frozenset[str]

        @dataclasses.dataclass
        class Order:
            id: int
            lines: list[Line]
            placed: dt.date

        order = Order(
            id=7,
            lines=[Line("pen", Decimal("2.50"), frozenset({"b", "a"}))],
            placed=dt.date(2024, 5, 6),
        )
        assert main.to_jsonish(order) == {
            "id": 7,
            "lines": [{"label": "pen", "price": "2.50", "tags": ["a", "b"]}],
            "placed": "2024-05-06",
        }

    def test_sets_become_sorted_lists_even_when_mixed(self, main: ModuleType) -> None:
        assert main.to_jsonish({3, 1, 2}) == [1, 2, 3]
        mixed = main.to_jsonish({1, "a"})
        assert sorted(map(repr, mixed)) == sorted(map(repr, [1, "a"])), "mixed-type sets must still serialise"

    def test_dict_keys_must_be_strings(self, main: ModuleType) -> None:
        assert main.to_jsonish({"a": Decimal("1")}) == {"a": "1"}
        with pytest.raises(TypeError):
            main.to_jsonish({1: "a"})

    def test_unknown_type_raises(self, main: ModuleType) -> None:
        with pytest.raises(TypeError, match="object"):
            main.to_jsonish(object())

    def test_custom_type_can_be_registered(self, main: ModuleType) -> None:
        class Point:
            def __init__(self, x: int, y: int) -> None:
                self.x, self.y = x, y

        main.to_jsonish.register(Point, lambda p: [p.x, p.y])
        assert main.to_jsonish(Point(1, 2)) == [1, 2]

    def test_bare_registration_uses_dashed_name(self, main: ModuleType) -> None:
        registry = main.CommandRegistry()

        @registry.command
        def sync_users() -> str:
            return "synced"

        assert registry.names() == ["sync-users"]
        assert sync_users() == "synced", "the decorator must return the function unchanged"
        assert registry.dispatch("sync-users") == "synced"

    def test_named_registration_and_dispatch_arguments(self, main: ModuleType) -> None:
        registry = main.CommandRegistry()

        @registry.command("add")
        def _add(a: int, b: int = 0, *, scale: int = 1) -> int:
            return (a + b) * scale

        assert registry.dispatch("add", 2, 3, scale=10) == 50
        assert registry.dispatch("add", 4) == 4

    def test_unknown_command_and_duplicate_registration(self, main: ModuleType) -> None:
        registry = main.CommandRegistry()

        @registry.command()
        def ping() -> str:
            return "pong"

        with pytest.raises(main.UnknownCommand):
            registry.dispatch("nope")
        assert issubclass(main.UnknownCommand, KeyError)
        with pytest.raises(ValueError):
            registry.command("ping")(lambda: None)

    def test_help_and_names_and_isolation(self, main: ModuleType) -> None:
        registry = main.CommandRegistry()

        @registry.command
        def documented() -> None:
            """Do the thing.

            Longer explanation that help() must not include.
            """

        @registry.command
        def bare() -> None:
            pass

        assert registry.help("documented") == "Do the thing."
        assert registry.help("bare") == ""
        assert registry.names() == ["bare", "documented"], "names() must be sorted"
        assert main.CommandRegistry().names() == [], "registries must not share state"

    def test_command_rejects_non_string_name(self, main: ModuleType) -> None:
        with pytest.raises(TypeError):
            main.CommandRegistry().command(42)  # type: ignore[call-overload]
