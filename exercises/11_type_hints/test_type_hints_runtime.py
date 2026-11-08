"""Runtime behaviour of the typed service building blocks (Parts 1-6)."""

from collections.abc import Callable, Iterator
from dataclasses import FrozenInstanceError, dataclass
from types import ModuleType
from typing import Any, get_type_hints

import pytest


@dataclass(frozen=True)
class Item:
    id: int
    name: str


def _settings_env(**overrides: str) -> dict[str, str]:
    env = {
        "SERVICE_NAME": "orders",
        "DB_HOST": "db.internal",
        "DB_PORT": "5432",
        "DB_NAME": "orders",
    }
    env.update(overrides)
    return env


# --------------------------------------------------------------------------- #
# Part 1: generic repository and structural protocols
# --------------------------------------------------------------------------- #


class TestPart1RepoAndProtocols:
    def test_add_then_get_returns_stored_object(self, main: ModuleType) -> None:
        repo = main.Repo[Item]()
        item = Item(1, "widget")
        assert repo.add(item) is item, "add() must return the stored item"
        assert repo.get(1) is item, "get() must return the same object that was added"

    def test_get_missing_returns_none(self, main: ModuleType) -> None:
        assert main.Repo[Item]().get(99) is None, "get() of a missing id must return None"

    def test_add_duplicate_id_raises_duplicate_error(self, main: ModuleType) -> None:
        repo = main.Repo[Item]()
        repo.add(Item(1, "a"))
        assert issubclass(main.DuplicateIdError, ValueError), "DuplicateIdError must subclass ValueError"
        with pytest.raises(main.DuplicateIdError):
            repo.add(Item(1, "b"))
        assert repo.require(1).name == "a", "a rejected duplicate must not overwrite the original"

    def test_require_missing_raises_not_found_error(self, main: ModuleType) -> None:
        assert issubclass(main.NotFoundError, LookupError), "NotFoundError must subclass LookupError"
        with pytest.raises(main.NotFoundError, match="42"):
            main.Repo[Item]().require(42)

    def test_update_replaces_item_and_keeps_position(self, main: ModuleType) -> None:
        repo = main.Repo[Item]()
        for i in (1, 2, 3):
            repo.add(Item(i, f"n{i}"))
        repo.update(Item(2, "changed"))
        assert [item.name for item in repo.all()] == ["n1", "changed", "n3"], (
            "update() must replace in place, preserving insertion order"
        )

    def test_update_missing_raises_not_found_error(self, main: ModuleType) -> None:
        with pytest.raises(main.NotFoundError):
            main.Repo[Item]().update(Item(5, "ghost"))

    def test_remove_returns_item_and_forgets_it(self, main: ModuleType) -> None:
        repo = main.Repo[Item]()
        item = repo.add(Item(1, "a"))
        assert repo.remove(1) is item, "remove() must return the removed item"
        assert repo.get(1) is None and len(repo) == 0, "removed item must be gone"
        with pytest.raises(main.NotFoundError):
            repo.remove(1)

    def test_all_returns_insertion_order_copy(self, main: ModuleType) -> None:
        repo = main.Repo[Item]()
        repo.add(Item(3, "c"))
        repo.add(Item(1, "a"))
        snapshot = repo.all()
        assert [item.id for item in snapshot] == [3, 1], "all() must follow insertion order"
        snapshot.clear()
        assert len(repo) == 2, "mutating the list returned by all() must not affect the repository"

    def test_where_filters_by_predicate(self, main: ModuleType) -> None:
        repo = main.Repo[Item]()
        for i in range(1, 6):
            repo.add(Item(i, "odd" if i % 2 else "even"))
        odd = repo.where(lambda item: item.name == "odd")
        assert [item.id for item in odd] == [1, 3, 5], "where() must keep matching items in order"

    def test_len_counts_items(self, main: ModuleType) -> None:
        repo = main.Repo[Item]()
        assert len(repo) == 0, "a new repository must be empty"
        repo.add(Item(1, "a"))
        assert len(repo) == 1, "__len__ must reflect stored items"

    def test_first_consumes_only_one_element(self, main: ModuleType) -> None:
        pulled: list[int] = []

        def gen() -> Iterator[int]:
            for i in range(10):
                pulled.append(i)
                yield i

        assert main.first(gen()) == 0, "first() must return the first element"
        assert pulled == [0], f"first() must stop after one element, but pulled {pulled}"

    def test_first_of_empty_raises_lookup_error(self, main: ModuleType) -> None:
        with pytest.raises(LookupError):
            main.first([])

    def test_first_or_returns_default_when_empty(self, main: ModuleType) -> None:
        assert main.first_or([], "fallback") == "fallback", "first_or() must return default when empty"
        assert main.first_or(iter([7, 8]), "fallback") == 7, "first_or() must prefer the first item"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("  Api-Key  ", "api-key"), (b"  Api-Key ", b"api-key")],
    )
    def test_normalize_key_preserves_str_or_bytes(self, main: ModuleType, raw: object, expected: object) -> None:
        assert main.normalize_key(raw) == expected, "normalize_key() must strip and lowercase, keeping the type"

    def test_normalize_key_rejects_other_types(self, main: ModuleType) -> None:
        with pytest.raises(TypeError):
            main.normalize_key(3)  # type: ignore[arg-type]

    def test_structural_sink_needs_no_inheritance(self, main: ModuleType) -> None:
        class Printer:
            def write(self, item: str) -> None:
                pass

        assert isinstance(Printer(), main.Sink), "runtime isinstance must accept any object with write()"
        assert isinstance(main.MemorySink(), main.Sink), "MemorySink must satisfy Sink structurally"
        assert not isinstance(object(), main.Sink), "objects without write() must not satisfy Sink"

    def test_fan_out_writes_in_order_and_counts(self, main: ModuleType) -> None:
        first_sink: Any = main.MemorySink()
        second_sink: Any = main.MemorySink()
        written = main.fan_out("event", [first_sink, second_sink])
        assert written == 2, "fan_out() must report how many sinks were written"
        assert first_sink.items == ("event",) and second_sink.items == ("event",), (
            "every sink must receive the item"
        )

    def test_memory_sink_items_is_snapshot(self, main: ModuleType) -> None:
        sink = main.MemorySink[int]()
        sink.write(1)
        snapshot = sink.items
        sink.write(2)
        assert snapshot == (1,), "items must be an immutable snapshot"
        assert sink.items == (1, 2), "items must include every write, in order"


# --------------------------------------------------------------------------- #
# Part 2: typed event bus
# --------------------------------------------------------------------------- #


class TestPart2EventBus:
    def test_user_topic_formats_newtype_value(self, main: ModuleType) -> None:
        assert main.user_topic(main.UserId(42)) == "user.42.events", "user_topic() format changed"

    def test_publish_delivers_only_matching_kind(self, main: ModuleType) -> None:
        bus = main.EventBus()
        users: list[Any] = []
        orders: list[Any] = []
        bus.subscribe("user.created", users.append)
        bus.subscribe("order.placed", orders.append)
        event = {"kind": "order.placed", "order_id": 7, "total_cents": 500}
        delivered = bus.publish(event)
        assert delivered == 1, "publish() must return the number of handlers invoked"
        assert orders == [event], "the order handler must receive the event"
        assert users == [], "handlers for other kinds must not be invoked"

    def test_publish_without_subscribers_returns_zero(self, main: ModuleType) -> None:
        bus = main.EventBus()
        assert bus.publish({"kind": "user.created", "user_id": main.UserId(1), "email": "a@b.c"}) == 0

    def test_handlers_run_in_subscription_order(self, main: ModuleType) -> None:
        bus = main.EventBus()
        calls: list[str] = []
        bus.subscribe("order.placed", lambda _event: calls.append("first"))
        bus.subscribe("order.placed", lambda _event: calls.append("second"))
        bus.publish({"kind": "order.placed", "order_id": 1, "total_cents": 1})
        assert calls == ["first", "second"], "handlers must run in subscription order"

    def test_unsubscribe_stops_delivery_and_is_idempotent(self, main: ModuleType) -> None:
        bus = main.EventBus()
        seen: list[Any] = []
        unsubscribe = bus.subscribe("order.placed", seen.append)
        unsubscribe()
        unsubscribe()
        assert bus.publish({"kind": "order.placed", "order_id": 1, "total_cents": 1}) == 0
        assert seen == [], "an unsubscribed handler must not receive events"

    def test_same_handler_twice_unsubscribes_independently(self, main: ModuleType) -> None:
        bus = main.EventBus()
        seen: list[Any] = []
        first_token = bus.subscribe("order.placed", seen.append)
        bus.subscribe("order.placed", seen.append)
        first_token()
        first_token()  # a second call must not remove the other registration
        assert bus.publish({"kind": "order.placed", "order_id": 1, "total_cents": 1}) == 1
        assert len(seen) == 1, "the second registration must survive the first unsubscribe"

    def test_subscribe_during_publish_does_not_receive_current_event(self, main: ModuleType) -> None:
        bus = main.EventBus()
        late: list[Any] = []

        def subscribe_late(_event: Any) -> None:
            bus.subscribe("order.placed", late.append)

        bus.subscribe("order.placed", subscribe_late)
        event = {"kind": "order.placed", "order_id": 1, "total_cents": 1}
        assert bus.publish(event) == 1, "only the original handler must run for this event"
        assert late == [], "handlers added during dispatch must miss the in-flight event"

    def test_unsubscribe_during_publish_skips_cancelled_handler(self, main: ModuleType) -> None:
        bus = main.EventBus()
        seen: list[str] = []
        unsubscribe_b: Callable[[], None] | None = None

        def a(_event: Any) -> None:
            seen.append("a")
            assert unsubscribe_b is not None
            unsubscribe_b()

        def b(_event: Any) -> None:
            seen.append("b")

        bus.subscribe("order.placed", a)
        unsubscribe_b = bus.subscribe("order.placed", b)
        delivered = bus.publish({"kind": "order.placed", "order_id": 1, "total_cents": 1})
        assert seen == ["a"], "a handler cancelled mid-dispatch must be skipped"
        assert delivered == 1, "the returned count must only include handlers actually invoked"

    def test_describe_event_user(self, main: ModuleType) -> None:
        event = {"kind": "user.created", "user_id": main.UserId(9), "email": "ada@example.com"}
        assert main.describe_event(event) == "user 9 registered <ada@example.com>"

    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            ({"kind": "order.placed", "order_id": 7, "total_cents": 1250}, "order 7 placed: 1250 cents"),
            (
                {"kind": "order.placed", "order_id": 7, "total_cents": 1250, "coupon": "SPRING"},
                "order 7 placed: 1250 cents (coupon SPRING)",
            ),
        ],
    )
    def test_describe_event_order_with_and_without_coupon(
        self, main: ModuleType, event: dict[str, Any], expected: str
    ) -> None:
        assert main.describe_event(event) == expected


# --------------------------------------------------------------------------- #
# Part 3: typed settings
# --------------------------------------------------------------------------- #


class TestPart3Settings:
    def test_minimal_env_applies_defaults(self, main: ModuleType) -> None:
        settings = main.load_settings(_settings_env())
        assert settings["service_name"] == "orders"
        assert settings["database"] == {"host": "db.internal", "port": 5432, "name": "orders"}, (
            "database block must hold host, port (as int) and name; pool_size is absent"
        )
        assert settings["http_timeout"] == main.DEFAULT_HTTP_TIMEOUT, "http_timeout must default"
        assert settings["debug"] is False, "debug must default to False"

    def test_full_env_is_parsed(self, main: ModuleType) -> None:
        env = _settings_env(DB_POOL_SIZE="20", HTTP_TIMEOUT="2.5", DEBUG="Yes")
        settings = main.load_settings(env)
        assert settings["database"]["pool_size"] == 20, "DB_POOL_SIZE must be parsed to int"
        assert settings["http_timeout"] == 2.5, "HTTP_TIMEOUT must be parsed to float"
        assert settings["debug"] is True, "DEBUG must accept yes case-insensitively"

    def test_blank_values_are_treated_as_unset(self, main: ModuleType) -> None:
        settings = main.load_settings(_settings_env(DB_POOL_SIZE="   ", DEBUG=""))
        assert "pool_size" not in settings["database"], "a blank optional value must be treated as unset"
        assert settings["debug"] is False

    @pytest.mark.parametrize("key", ["SERVICE_NAME", "DB_HOST", "DB_PORT", "DB_NAME"])
    def test_missing_required_setting_raises_with_key_name(self, main: ModuleType, key: str) -> None:
        env = _settings_env()
        del env[key]
        with pytest.raises(main.SettingsError, match=key):
            main.load_settings(env)

    def test_settings_error_is_value_error(self, main: ModuleType) -> None:
        assert issubclass(main.SettingsError, ValueError), "SettingsError must subclass ValueError"

    @pytest.mark.parametrize("port", ["0", "65536", "abc"])
    def test_invalid_port_is_rejected(self, main: ModuleType, port: str) -> None:
        with pytest.raises(main.SettingsError, match="DB_PORT"):
            main.load_settings(_settings_env(DB_PORT=port))

    @pytest.mark.parametrize("size", ["0", "1001"])
    def test_pool_size_out_of_bounds_is_rejected(self, main: ModuleType, size: str) -> None:
        with pytest.raises(main.SettingsError, match="DB_POOL_SIZE"):
            main.load_settings(_settings_env(DB_POOL_SIZE=size))

    @pytest.mark.parametrize("timeout", ["nan", "inf", "0", "-1", "soon"])
    def test_http_timeout_must_be_finite_and_positive(self, main: ModuleType, timeout: str) -> None:
        with pytest.raises(main.SettingsError, match="HTTP_TIMEOUT"):
            main.load_settings(_settings_env(HTTP_TIMEOUT=timeout))

    def test_invalid_boolean_is_rejected(self, main: ModuleType) -> None:
        with pytest.raises(main.SettingsError, match="DEBUG"):
            main.load_settings(_settings_env(DEBUG="maybe"))

    @pytest.mark.parametrize(("raw", "expected"), [("1", True), ("On", True), ("0", False), ("NO", False)])
    def test_boolean_spellings(self, main: ModuleType, raw: str, expected: bool) -> None:
        assert main.load_settings(_settings_env(DEBUG=raw))["debug"] is expected

    def test_port_annotation_carries_range_metadata(self, main: ModuleType) -> None:
        hints = get_type_hints(main.DatabaseSettings, include_extras=True)
        assert main.field_range(hints["port"]) == main.Range(1, 65535), (
            "DatabaseSettings['port'] must be Annotated with its Range, readable via field_range()"
        )

    def test_field_range_is_none_for_plain_types(self, main: ModuleType) -> None:
        assert main.field_range(int) is None, "field_range() must be None for hints without metadata"

    def test_default_timeout_constant(self, main: ModuleType) -> None:
        assert main.DEFAULT_HTTP_TIMEOUT == 30.0


# --------------------------------------------------------------------------- #
# Part 4: signature-preserving decorators
# --------------------------------------------------------------------------- #


class TestPart4Decorators:
    def test_retry_success_calls_once(self, main: ModuleType) -> None:
        calls: list[int] = []

        @main.retry(attempts=3)
        def ok() -> str:
            calls.append(1)
            return "done"

        assert ok() == "done"
        assert len(calls) == 1, "a successful call must not be retried"

    def test_retry_until_success(self, main: ModuleType) -> None:
        calls: list[int] = []

        @main.retry(attempts=3)
        def flaky() -> str:
            calls.append(1)
            if len(calls) < 3:
                raise OSError("transient")
            return "ok"

        assert flaky() == "ok"
        assert len(calls) == 3, "must retry until the third attempt succeeds"

    def test_retry_reraises_last_exception_after_exhaustion(self, main: ModuleType) -> None:
        calls: list[int] = []

        @main.retry(attempts=2, on=(OSError,))
        def always_fails() -> None:
            calls.append(1)
            raise OSError(f"attempt {len(calls)}")

        with pytest.raises(OSError, match="attempt 2"):
            always_fails()
        assert len(calls) == 2, "attempts is the total number of calls, not retries"

    def test_retry_ignores_exceptions_outside_on(self, main: ModuleType) -> None:
        calls: list[int] = []

        @main.retry(attempts=5, on=(OSError,))
        def bad_input() -> None:
            calls.append(1)
            raise ValueError("not retryable")

        with pytest.raises(ValueError):
            bad_input()
        assert len(calls) == 1, "exceptions not listed in `on` must propagate immediately"

    def test_retry_rejects_zero_attempts_at_decoration_time(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.retry(attempts=0)

    def test_retry_forwards_arguments(self, main: ModuleType) -> None:
        @main.retry(attempts=2)
        def add(a: int, *, b: int = 1) -> int:
            return a + b

        assert add(1, b=2) == 3, "positional and keyword arguments must pass through unchanged"

    def test_retry_preserves_metadata(self, main: ModuleType) -> None:
        @main.retry(attempts=2)
        def documented(x: int) -> int:
            """Doc for documented."""
            return x

        assert documented.__name__ == "documented", "functools.wraps must preserve __name__"
        assert documented.__doc__ == "Doc for documented.", "functools.wraps must preserve __doc__"
        assert documented.__wrapped__.__name__ == "documented", "__wrapped__ must reference the original"  # type: ignore[attr-defined]

    def test_tenant_scope_binds_and_restores(self, main: ModuleType) -> None:
        with pytest.raises(LookupError):
            main.current_tenant()
        with main.tenant_scope(main.TenantId("acme")):
            assert main.current_tenant() == "acme"
            with main.tenant_scope(main.TenantId("globex")):
                assert main.current_tenant() == "globex", "inner scope must shadow the outer one"
            assert main.current_tenant() == "acme", "leaving the inner scope must restore the outer tenant"
        with pytest.raises(LookupError):
            main.current_tenant()

    def test_tenant_scope_restores_on_exception(self, main: ModuleType) -> None:
        with pytest.raises(RuntimeError), main.tenant_scope(main.TenantId("acme")):
            raise RuntimeError("boom")
        with pytest.raises(LookupError):
            main.current_tenant()

    def test_tenant_scope_rejects_blank(self, main: ModuleType) -> None:
        with pytest.raises(ValueError), main.tenant_scope(main.TenantId("  ")):
            pass

    def test_with_tenant_injects_ambient_tenant(self, main: ModuleType) -> None:
        @main.with_tenant
        def list_orders(tenant: str, status: str, *, limit: int = 10) -> str:
            return f"{tenant}:{status}:{limit}"

        assert list_orders.__name__ == "list_orders", "with_tenant must preserve __name__"
        with main.tenant_scope(main.TenantId("acme")):
            assert list_orders("open", limit=5) == "acme:open:5", "caller must omit the tenant argument"

    def test_with_tenant_without_scope_raises(self, main: ModuleType) -> None:
        @main.with_tenant
        def whoami(tenant: str) -> str:
            return tenant

        with pytest.raises(LookupError):
            whoami()


# --------------------------------------------------------------------------- #
# Part 5: immutable query builder with Self
# --------------------------------------------------------------------------- #


class TestPart5QueryBuilder:
    def test_bare_select(self, main: ModuleType) -> None:
        assert main.QueryBuilder("users").build() == ("SELECT * FROM users", ())

    def test_where_is_parameterized(self, main: ModuleType) -> None:
        sql, params = main.QueryBuilder("orders").where("status", "open").where("total", 10).build()
        assert sql == "SELECT * FROM orders WHERE status = ? AND total = ?", "filters must use placeholders"
        assert params == ("open", 10), "values must be passed as parameters, in order"

    def test_none_becomes_is_null_without_parameter(self, main: ModuleType) -> None:
        sql, params = main.QueryBuilder("users").where("deleted_at", None).build()
        assert sql == "SELECT * FROM users WHERE deleted_at IS NULL", "None must compile to IS NULL"
        assert params == (), "IS NULL takes no parameter"

    def test_order_and_limit_bind_last(self, main: ModuleType) -> None:
        query = (
            main.QueryBuilder("users")
            .where("active", 1)
            .order_by("created_at", descending=True)
            .order_by("email")
            .limit(25)
        )
        sql, params = query.build()
        assert sql == (
            "SELECT * FROM users WHERE active = ? ORDER BY created_at DESC, email ASC LIMIT ?"
        ), "clauses must appear in SQL order"
        assert params == (1, 25), "LIMIT must be the final bound parameter"

    def test_limit_zero_is_kept(self, main: ModuleType) -> None:
        assert main.QueryBuilder("users").limit(0).build() == ("SELECT * FROM users LIMIT ?", (0,))

    def test_negative_limit_rejected(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.QueryBuilder("users").limit(-1)

    def test_builder_is_immutable(self, main: ModuleType) -> None:
        base = main.QueryBuilder("users")
        base.where("active", 1)
        assert base.build() == ("SELECT * FROM users", ()), "where() must return a new builder"

    @pytest.mark.parametrize("bad", ["users; DROP TABLE x", "1abc", "", "a-b", "a b"])
    def test_invalid_identifiers_are_rejected(self, main: ModuleType, bad: str) -> None:
        with pytest.raises(ValueError):
            main.QueryBuilder(bad)
        with pytest.raises(ValueError):
            main.QueryBuilder("users").where(bad, 1)
        with pytest.raises(ValueError):
            main.QueryBuilder("users").order_by(bad)

    def test_subclass_methods_return_subclass_instances(self, main: ModuleType) -> None:
        class UserQuery(main.QueryBuilder):
            def active_only(self) -> Any:
                return self.where("active", True)

        query = UserQuery("users").where("email", "a@b.c").active_only()
        assert type(query) is UserQuery, "Self-typed methods must preserve the subclass"
        sql, params = query.build()
        assert sql == "SELECT * FROM users WHERE email = ? AND active = ?"
        assert params == ("a@b.c", True)


# --------------------------------------------------------------------------- #
# Part 6: result unions, TypeIs/TypeGuard, exhaustive match
# --------------------------------------------------------------------------- #


class TestPart6Results:
    def test_ok_and_err_hold_their_payload_and_are_frozen(self, main: ModuleType) -> None:
        ok = main.Ok(5)
        err = main.Err("declined")
        assert ok.value == 5 and err.error == "declined"
        with pytest.raises(FrozenInstanceError):
            ok.value = 6  # type: ignore[misc]

    def test_is_ok(self, main: ModuleType) -> None:
        assert main.is_ok(main.Ok(1)) is True
        assert main.is_ok(main.Err("declined")) is False

    def test_collect_all_ok_returns_ok_list(self, main: ModuleType) -> None:
        collected = main.collect([main.Ok(1), main.Ok(2)])
        assert isinstance(collected, main.Ok) and collected.value == [1, 2]

    def test_collect_empty_is_ok_empty(self, main: ModuleType) -> None:
        collected = main.collect([])
        assert isinstance(collected, main.Ok) and collected.value == [], "empty input must yield Ok([])"

    def test_collect_short_circuits_on_first_err(self, main: ModuleType) -> None:
        pulled: list[int] = []
        failure = main.Err("gateway_timeout")

        def results() -> Iterator[Any]:
            for step, result in enumerate([main.Ok(1), failure, main.Ok(3)]):
                pulled.append(step)
                yield result

        collected = main.collect(results())
        assert collected is failure, "collect() must return the first Err object unchanged"
        assert pulled == [0, 1], f"collect() must stop consuming at the first Err, consumed {pulled}"

    def test_all_str_narrows_sequences(self, main: ModuleType) -> None:
        assert main.all_str(["a", "b"]) is True
        assert main.all_str(["a", 1]) is False
        assert main.all_str([]) is True, "vacuously true for an empty sequence"

    @pytest.mark.parametrize(
        ("error", "status"),
        [("declined", 402), ("insufficient_funds", 422), ("gateway_timeout", 504)],
    )
    def test_http_status_per_error(self, main: ModuleType, error: str, status: int) -> None:
        assert main.http_status(error) == status  # type: ignore[arg-type]

    def test_http_status_rejects_unknown_error_at_runtime(self, main: ModuleType) -> None:
        with pytest.raises(AssertionError):
            main.http_status("cosmic_ray")  # type: ignore[arg-type]

    def test_render_result_ok(self, main: ModuleType) -> None:
        assert main.render_result(main.Ok(5)) == "charged 5"

    def test_render_result_err(self, main: ModuleType) -> None:
        assert main.render_result(main.Err("declined")) == "failed: declined (HTTP 402)"

    def test_render_result_rejects_non_result(self, main: ModuleType) -> None:
        with pytest.raises(AssertionError):
            main.render_result("oops")  # type: ignore[arg-type]
