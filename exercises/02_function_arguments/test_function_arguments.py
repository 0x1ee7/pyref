"""Tests for function-argument handling: binding, defaults, sentinels, forwarding."""

from __future__ import annotations

import copy
import functools
from types import ModuleType
from typing import Any

import pytest

URL = "https://example.com/api"


class TestPart1BuildRequest:
    def test_method_and_url_are_positional_only(self, main: ModuleType) -> None:
        with pytest.raises(TypeError):
            main.build_request(method="GET", url=URL)

    def test_headers_and_params_are_keyword_only(self, main: ModuleType) -> None:
        with pytest.raises(TypeError):
            main.build_request("GET", URL, {"accept": "*/*"})

    def test_keyword_named_method_lands_in_extra(self, main: ModuleType) -> None:
        req = main.build_request("GET", URL, method="PUT", retries=2)
        assert req.method == "GET", "the positional method must win; the kwarg is just extra data"
        assert req.extra == {"method": "PUT", "retries": 2}, "unknown kwargs must be collected into extra"

    def test_method_is_uppercased_and_validated(self, main: ModuleType) -> None:
        assert main.build_request("get", URL).method == "GET", "method must be uppercased"
        with pytest.raises(ValueError):
            main.build_request("BREW", URL)
        with pytest.raises(TypeError):
            main.build_request(None, URL)  # type: ignore[arg-type]

    def test_url_must_be_http_or_https_str(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.build_request("GET", "ftp://example.com")
        with pytest.raises(TypeError):
            main.build_request("GET", b"https://example.com")  # type: ignore[arg-type]

    def test_defaults(self, main: ModuleType) -> None:
        req = main.build_request("POST", URL)
        assert req.headers == {}, "headers default to an empty mapping"
        assert req.params == {}, "params default to an empty mapping"
        assert req.timeout == 10.0, "timeout defaults to 10.0"
        assert req.extra == {}

    def test_timeout_validation(self, main: ModuleType) -> None:
        req = main.build_request("GET", URL, timeout=3)
        assert req.timeout == 3.0 and isinstance(req.timeout, float), "int timeouts are stored as float"
        with pytest.raises(TypeError):
            main.build_request("GET", URL, timeout=True)
        with pytest.raises(TypeError):
            main.build_request("GET", URL, timeout="5")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            main.build_request("GET", URL, timeout=0)
        with pytest.raises(ValueError):
            main.build_request("GET", URL, timeout=float("nan"))

    def test_params_are_rendered_as_strings(self, main: ModuleType) -> None:
        req = main.build_request(
            "GET",
            URL,
            params={"q": "tea", "page": 2, "ratio": 0.5, "dry_run": True, "fast": False},
        )
        assert req.params == {
            "q": "tea",
            "page": "2",
            "ratio": "0.5",
            "dry_run": "true",
            "fast": "false",
        }, "bools render as lowercase true/false; other scalars via str()"

    def test_unsupported_param_values_and_keys_are_rejected(self, main: ModuleType) -> None:
        with pytest.raises(TypeError):
            main.build_request("GET", URL, params={"tags": ["a", "b"]})
        with pytest.raises(TypeError):
            main.build_request("GET", URL, params={"nothing": None})
        with pytest.raises(TypeError):
            main.build_request("GET", URL, params={1: "x"})  # type: ignore[dict-item]

    def test_header_names_are_lowercased_and_deduplicated(self, main: ModuleType) -> None:
        req = main.build_request("GET", URL, headers={"Accept": "application/json", "X-Trace": "abc"})
        assert req.headers == {"accept": "application/json", "x-trace": "abc"}, "header names must be lowercased"
        with pytest.raises(ValueError):
            main.build_request("GET", URL, headers={"Accept": "a", "accept": "b"})

    def test_default_containers_are_not_shared_between_calls(self, main: ModuleType) -> None:
        first = main.build_request("GET", URL)
        first.headers["x-leak"] = "1"
        first.params["leak"] = "1"
        second = main.build_request("GET", URL)
        assert second.headers == {}, "mutable defaults must not be shared between calls"
        assert second.params == {}, "mutable defaults must not be shared between calls"

    def test_caller_containers_are_copied(self, main: ModuleType) -> None:
        headers = {"Accept": "text/plain"}
        params = {"q": "x"}
        req = main.build_request("GET", URL, headers=headers, params=params)
        req.headers["added"] = "1"
        req.params["added"] = "1"
        assert headers == {"Accept": "text/plain"}, "the caller's headers mapping must not be aliased"
        assert params == {"q": "x"}, "the caller's params mapping must not be aliased"


class TestPart2Select:
    def test_table_is_required_keyword_only(self, main: ModuleType) -> None:
        with pytest.raises(TypeError):
            main.select("id", "users")  # type: ignore[call-arg]
        assert main.select("id", table="users") == ("SELECT id FROM users", ())

    def test_no_columns_selects_star(self, main: ModuleType) -> None:
        assert main.select(table="users") == ("SELECT * FROM users", ())

    def test_dotted_identifiers_are_allowed(self, main: ModuleType) -> None:
        sql, _ = main.select("u.id", "u.email", table="users")
        assert sql == "SELECT u.id, u.email FROM users", "single dotted qualifiers are valid identifiers"

    def test_where_equality_uses_placeholders(self, main: ModuleType) -> None:
        sql, params = main.select("id", table="users", where={"active": True, "org_id": 7})
        assert sql == "SELECT id FROM users WHERE active = ? AND org_id = ?"
        assert params == (True, 7), "values must travel as parameters, in placeholder order"

    def test_where_none_means_is_null_without_a_parameter(self, main: ModuleType) -> None:
        sql, params = main.select("id", table="users", where={"deleted_at": None})
        assert sql == "SELECT id FROM users WHERE deleted_at IS NULL"
        assert params == (), "IS NULL takes no placeholder"

    def test_empty_where_adds_no_clause(self, main: ModuleType) -> None:
        assert main.select("id", table="users", where={}) == ("SELECT id FROM users", ())

    def test_identifiers_are_validated(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.select("id; DROP TABLE users", table="users")
        with pytest.raises(ValueError):
            main.select("id", table="users; --")
        with pytest.raises(ValueError):
            main.select("id", table="users", where={"1x": 2})
        with pytest.raises(TypeError):
            main.select(42, table="users")  # type: ignore[arg-type]

    def test_order_by_string_is_one_column_not_characters(self, main: ModuleType) -> None:
        sql, _ = main.select("id", table="users", order_by="created_at")
        assert sql == "SELECT id FROM users ORDER BY created_at", "a bare str is a single term"

    def test_order_by_descending_prefix_and_multiple_terms(self, main: ModuleType) -> None:
        sql, _ = main.select("id", table="users", order_by=("-created_at", "name"))
        assert sql == "SELECT id FROM users ORDER BY created_at DESC, name"

    def test_order_by_terms_are_validated(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.select("id", table="users", order_by=("-",))
        with pytest.raises(ValueError):
            main.select("id", table="users", order_by=("name ASC",))
        with pytest.raises(TypeError):
            main.select("id", table="users", order_by=(1,))  # type: ignore[arg-type]

    def test_limit_is_parameterised_and_clause_order_is_fixed(self, main: ModuleType) -> None:
        sql, params = main.select("id", table="users", where={"active": True}, limit=25)
        assert sql == "SELECT id FROM users WHERE active = ? LIMIT ?"
        assert params == (True, 25)

    def test_limit_zero_is_allowed_and_none_adds_nothing(self, main: ModuleType) -> None:
        assert main.select(table="users", limit=0) == ("SELECT * FROM users LIMIT ?", (0,))
        assert main.select(table="users", limit=None) == ("SELECT * FROM users", ())

    def test_limit_validation(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.select(table="users", limit=-1)
        with pytest.raises(TypeError):
            main.select(table="users", limit=True)
        with pytest.raises(TypeError):
            main.select(table="users", limit="10")  # type: ignore[arg-type]

    def test_select_from_spec_unpacks_columns_and_options(self, main: ModuleType) -> None:
        spec: dict[str, Any] = {
            "columns": ["id", "email"],
            "table": "users",
            "where": {"active": True},
            "order_by": ["-id"],
            "limit": 5,
        }
        assert main.select_from_spec(spec) == (
            "SELECT id, email FROM users WHERE active = ? ORDER BY id DESC LIMIT ?",
            (True, 5),
        )

    def test_select_from_spec_rejects_bare_string_columns(self, main: ModuleType) -> None:
        with pytest.raises(TypeError):
            main.select_from_spec({"columns": "id", "table": "users"})

    def test_select_from_spec_rejects_unknown_keys(self, main: ModuleType) -> None:
        with pytest.raises(TypeError):
            main.select_from_spec({"columns": ["id"], "table": "users", "offset": 3})


class TestPart3UpdateUser:
    USER: dict[str, Any] = {"id": 1, "email": "ada@example.com", "nickname": "ada"}

    def test_sentinel_is_a_singleton_with_repr_and_identity_copies(self, main: ModuleType) -> None:
        assert repr(main.MISSING) == "MISSING"
        assert main.MISSING is not None, "MISSING must be distinct from None"
        assert copy.copy(main.MISSING) is main.MISSING, "copy.copy must preserve the singleton"
        assert copy.deepcopy(main.MISSING) is main.MISSING, "deepcopy must preserve the singleton"

    def test_omitted_fields_are_left_alone(self, main: ModuleType) -> None:
        assert main.update_user(self.USER) == self.USER

    def test_returns_new_dict_and_does_not_mutate_input(self, main: ModuleType) -> None:
        original = dict(self.USER)
        result = main.update_user(self.USER, nickname="grace")
        assert self.USER == original, "the input user must not be mutated"
        assert result is not self.USER
        assert result["nickname"] == "grace"

    def test_none_clears_the_field(self, main: ModuleType) -> None:
        result = main.update_user(self.USER, nickname=None, email=None)
        assert result == {"id": 1, "email": None, "nickname": None}, "None clears: key stays, value is None"

    def test_missing_and_none_are_distinct(self, main: ModuleType) -> None:
        kept = main.update_user(self.USER)
        cleared = main.update_user(self.USER, email=None)
        assert kept["email"] == "ada@example.com", "omitting email must keep it"
        assert cleared["email"] is None, "passing None must clear it"

    def test_omission_does_not_create_missing_keys(self, main: ModuleType) -> None:
        assert main.update_user({"id": 2}) == {"id": 2}, "omitted fields must not be added"

    def test_email_is_stripped_and_lowercased(self, main: ModuleType) -> None:
        result = main.update_user(self.USER, email="  Grace.Hopper@Navy.MIL ")
        assert result["email"] == "grace.hopper@navy.mil"

    def test_invalid_emails_are_rejected(self, main: ModuleType) -> None:
        for bad in ("", "no-at-sign", "a@b", "two@@x.io", "sp ace@x.io"):
            with pytest.raises(ValueError):
                main.update_user(self.USER, email=bad)

    def test_email_must_be_str_or_none(self, main: ModuleType) -> None:
        with pytest.raises(TypeError):
            main.update_user(self.USER, email=123)  # type: ignore[arg-type]

    def test_nickname_is_stripped_and_bounded(self, main: ModuleType) -> None:
        assert main.update_user(self.USER, nickname="  bob ")["nickname"] == "bob"
        assert main.update_user(self.USER, nickname="x" * 32)["nickname"] == "x" * 32
        with pytest.raises(ValueError):
            main.update_user(self.USER, nickname="   ")
        with pytest.raises(ValueError):
            main.update_user(self.USER, nickname="x" * 33)

    def test_fields_are_keyword_only(self, main: ModuleType) -> None:
        with pytest.raises(TypeError):
            main.update_user(self.USER, "new@example.org")  # type: ignore[misc]


def _counted(value: object, log: list[str], name: str) -> Any:
    def provider() -> object:
        log.append(name)
        return value

    return provider


class TestPart4CallWithDependencies:
    def test_providers_fill_parameters_by_name(self, main: ModuleType) -> None:
        def handler(request: object, db: object, *, clock: object) -> tuple[object, ...]:
            return (request, db, clock)

        result = main.call_with_dependencies(handler, {"db": lambda: "DB", "clock": lambda: 99}, "REQ")
        assert result == ("REQ", "DB", 99)

    def test_explicit_arguments_win_and_suppress_the_provider(self, main: ModuleType) -> None:
        log: list[str] = []

        def handler(db: object) -> object:
            return db

        result = main.call_with_dependencies(handler, {"db": _counted("DB", log, "db")}, db="EXPLICIT")
        assert result == "EXPLICIT", "explicit arguments must take precedence over providers"
        assert log == [], "a provider must not be called when its parameter is explicitly bound"

    def test_providers_are_lazy_and_called_once_per_call(self, main: ModuleType) -> None:
        log: list[str] = []

        def handler(a: object, *, b: object) -> tuple[object, ...]:
            return (a, b)

        providers = {
            "a": _counted(1, log, "a"),
            "b": _counted(2, log, "b"),
            "unused": _counted(3, log, "unused"),
        }
        assert main.call_with_dependencies(handler, providers) == (1, 2)
        assert sorted(log) == ["a", "b"], "only needed providers run, each exactly once"

    def test_defaults_apply_only_when_no_provider_is_given(self, main: ModuleType) -> None:
        def handler(timeout: float = 5, *, retries: int = 1) -> tuple[float, int]:
            return (timeout, retries)

        assert main.call_with_dependencies(handler, {}) == (5, 1)
        assert main.call_with_dependencies(handler, {"retries": lambda: 3}) == (5, 3), (
            "a provider takes precedence over a default"
        )

    def test_unresolvable_required_parameter_raises(self, main: ModuleType) -> None:
        def handler(db: object, *, cache: object) -> None:
            return None

        with pytest.raises(main.MissingDependencyError) as info:
            main.call_with_dependencies(handler, {"db": lambda: 1})
        assert "cache" in str(info.value), "the error message must name the missing parameter"

    def test_missing_dependency_error_is_a_lookup_error(self, main: ModuleType) -> None:
        assert issubclass(main.MissingDependencyError, LookupError)

    def test_var_args_and_var_kwargs_are_never_injected(self, main: ModuleType) -> None:
        def handler(*args: object, db: object, **kwargs: object) -> tuple[object, ...]:
            return (args, db, kwargs)

        providers = {"db": lambda: "DB", "args": lambda: "nope", "kwargs": lambda: "nope"}
        assert main.call_with_dependencies(handler, providers) == ((), "DB", {})

    def test_positional_only_parameters_are_supplied_positionally(self, main: ModuleType) -> None:
        def handler(conn: object, /, *, limit: int = 5) -> tuple[object, int]:
            return (conn, limit)

        assert main.call_with_dependencies(handler, {"conn": lambda: "CONN"}) == ("CONN", 5)

    def test_positional_only_cannot_be_passed_by_keyword(self, main: ModuleType) -> None:
        def handler(conn: object, /) -> object:
            return conn

        with pytest.raises(TypeError):
            main.call_with_dependencies(handler, {}, conn="x")

    def test_partial_objects_respect_their_bound_keywords(self, main: ModuleType) -> None:
        def fetch(item_id: int, client: str, *, retries: int) -> tuple[int, str, int]:
            return (item_id, client, retries)

        bound = functools.partial(fetch, retries=3)
        result = main.call_with_dependencies(bound, {"item_id": lambda: 7, "client": lambda: "C"})
        assert result == (7, "C", 3)

    def test_parameter_names_do_not_collide_with_the_helper_signature(self, main: ModuleType) -> None:
        def handler(func: object, providers: object) -> tuple[object, object]:
            return (func, providers)

        result = main.call_with_dependencies(handler, {"func": lambda: "F", "providers": lambda: "P"})
        assert result == ("F", "P"), "func and providers must be positional-only in the helper"

    def test_unknown_explicit_keyword_is_a_type_error(self, main: ModuleType) -> None:
        def handler(db: object) -> object:
            return db

        with pytest.raises(TypeError):
            main.call_with_dependencies(handler, {"db": lambda: 1}, cache=2)


class TestPart5EventBus:
    def test_publish_forwards_args_and_kwargs(self, main: ModuleType) -> None:
        bus = main.EventBus()
        seen: list[tuple[object, ...]] = []

        def listener(a: int, b: int, *, flag: bool) -> int:
            seen.append((a, b, flag))
            return a + b

        bus.subscribe("sum", listener)
        assert bus.publish("sum", 1, 2, flag=True) == [3]
        assert seen == [(1, 2, True)]

    def test_event_name_is_positional_only_so_payload_can_use_event_keyword(self, main: ModuleType) -> None:
        bus = main.EventBus()
        got: list[object] = []
        bus.subscribe("user.created", lambda event: got.append(event))
        bus.publish("user.created", event="payload")
        assert got == ["payload"], "event= must be forwarded to listeners, not consumed by publish"

    def test_results_follow_subscription_order(self, main: ModuleType) -> None:
        bus = main.EventBus()
        bus.subscribe("e", lambda: "first")
        bus.subscribe("e", lambda: "second")
        assert bus.publish("e") == ["first", "second"]

    def test_publish_without_listeners_returns_empty_list(self, main: ModuleType) -> None:
        assert main.EventBus().publish("nobody", 1) == []

    def test_once_listener_fires_once_even_when_reentered(self, main: ModuleType) -> None:
        bus = main.EventBus()
        hits: list[str] = []

        def once_listener() -> None:
            hits.append("once")
            bus.publish("e")  # re-entrant: the once-listener must already be gone

        bus.subscribe("e", once_listener, once=True)
        bus.publish("e")
        assert hits == ["once"], "a once-listener must not run again during its own publish"
        assert bus.publish("e") == [], "and must be gone for later publishes"

    def test_once_is_keyword_only(self, main: ModuleType) -> None:
        with pytest.raises(TypeError):
            main.EventBus().subscribe("e", print, True)  # type: ignore[misc]

    def test_unsubscribe_reports_whether_it_removed_something(self, main: ModuleType) -> None:
        bus = main.EventBus()

        def listener() -> int:
            return 1

        bus.subscribe("e", listener)
        assert bus.unsubscribe("e", listener) is True
        assert bus.unsubscribe("e", listener) is False
        assert bus.publish("e") == []

    def test_unsubscribe_during_publish_does_not_skip_scheduled_listeners(self, main: ModuleType) -> None:
        bus = main.EventBus()
        calls: list[str] = []

        def b() -> None:
            calls.append("b")

        def a() -> None:
            calls.append("a")
            bus.unsubscribe("e", b)

        bus.subscribe("e", a)
        bus.subscribe("e", b)
        bus.publish("e")
        assert calls == ["a", "b"], "publish iterates over a snapshot of the subscriptions"
        bus.publish("e")
        assert calls == ["a", "b", "a"], "b is gone for subsequent publishes"

    def test_listener_exception_propagates_and_stops_delivery(self, main: ModuleType) -> None:
        bus = main.EventBus()
        later_calls: list[str] = []

        def boom() -> None:
            raise RuntimeError("bad listener")

        bus.subscribe("e", boom)
        bus.subscribe("e", lambda: later_calls.append("later"))
        with pytest.raises(RuntimeError, match="bad listener"):
            bus.publish("e")
        assert later_calls == [], "listeners after a failing one must not run"

    def test_subscribe_rejects_non_callables(self, main: ModuleType) -> None:
        with pytest.raises(TypeError):
            main.EventBus().subscribe("e", "not callable")  # type: ignore[arg-type]

    def test_partial_listeners_receive_forwarded_arguments(self, main: ModuleType) -> None:
        bus = main.EventBus()

        def render(prefix: str, msg: str, *, upper: bool) -> str:
            text = f"{prefix} {msg}"
            return text.upper() if upper else text

        bus.subscribe("log", functools.partial(render, "[app]", upper=True))
        assert bus.publish("log", "hello") == ["[APP] HELLO"]


def _area(width: int, height: int = 1, *, scale: int = 1) -> int:
    return width * height * scale


def _tagged(name: str, **tags: str) -> str:
    return name


def _total(*nums: int) -> int:
    return sum(nums)


class TestPart6CacheKeys:
    def test_positional_and_keyword_spellings_share_a_key(self, main: ModuleType) -> None:
        assert main.make_cache_key(_area, (2, 3), {}) == main.make_cache_key(_area, (2,), {"height": 3})

    def test_defaults_are_applied_before_keying(self, main: ModuleType) -> None:
        assert main.make_cache_key(_area, (2,), {}) == main.make_cache_key(_area, (2, 1), {"scale": 1})

    def test_different_values_produce_different_keys(self, main: ModuleType) -> None:
        assert main.make_cache_key(_area, (2,), {}) != main.make_cache_key(_area, (3,), {})
        assert main.make_cache_key(_area, (2,), {"scale": 1}) != main.make_cache_key(_area, (2,), {"scale": 2})

    def test_var_keyword_order_does_not_matter(self, main: ModuleType) -> None:
        forward = main.make_cache_key(_tagged, ("x",), {"a": "1", "b": "2"})
        reverse = main.make_cache_key(_tagged, ("x",), {"b": "2", "a": "1"})
        assert forward == reverse, "**kwargs are keyed by sorted name"

    def test_var_positional_contributes_its_length(self, main: ModuleType) -> None:
        assert main.make_cache_key(_total, (1, 2), {}) == main.make_cache_key(_total, (*[1, 2],), {})
        assert main.make_cache_key(_total, (1, 2), {}) != main.make_cache_key(_total, (1, 2, 3), {})

    def test_non_binding_call_raises_before_running(self, main: ModuleType) -> None:
        with pytest.raises(TypeError):
            main.make_cache_key(_area, (1, 2, 3, 4), {})

    def test_memoized_calls_share_cache_across_spellings(self, main: ModuleType) -> None:
        calls: list[tuple[int, int, int]] = []

        def area(width: int, height: int = 1, *, scale: int = 1) -> int:
            calls.append((width, height, scale))
            return width * height * scale

        cached = main.memoize(area)
        assert cached(2, 3) == 6
        assert cached(2, height=3) == 6
        assert cached(2, 3, scale=1) == 6
        assert calls == [(2, 3, 1)], "equivalent spellings must hit the same cache entry"
        assert cached.hits == 2, "two calls were served from the cache"
        assert cached.misses == 1, "exactly one call ran the function"

    def test_memoized_preserves_function_metadata(self, main: ModuleType) -> None:
        cached = main.memoize(_area)
        assert cached.__name__ == "_area"
        assert cached.__wrapped__ is _area  # type: ignore[attr-defined]

    def test_unhashable_argument_raises_type_error_without_running(self, main: ModuleType) -> None:
        calls: list[object] = []

        def total(seq: list[int]) -> int:
            calls.append(seq)
            return sum(seq)

        cached = main.memoize(total)
        with pytest.raises(TypeError):
            cached([1, 2])
        assert calls == [], "the function must not run when its key cannot be hashed"
        assert cached.misses == 0, "an unhashable key is not a miss"

    def test_wrong_arity_raises_before_running_the_function(self, main: ModuleType) -> None:
        calls: list[object] = []

        def area(width: int, height: int = 1) -> int:
            calls.append((width, height))
            return width * height

        cached = main.memoize(area)
        with pytest.raises(TypeError):
            cached(1, 2, 3)
        assert calls == []

    def test_exceptions_are_not_cached(self, main: ModuleType) -> None:
        attempts: list[int] = []

        def flaky(x: int) -> int:
            attempts.append(x)
            if len(attempts) == 1:
                raise ConnectionError("transient")
            return x * 10

        cached = main.memoize(flaky)
        with pytest.raises(ConnectionError):
            cached(4)
        assert cached(4) == 40, "a failed call must not poison the cache"
        assert attempts == [4, 4]
