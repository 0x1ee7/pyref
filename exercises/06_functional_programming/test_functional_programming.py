from __future__ import annotations

import copy
import dataclasses
from datetime import date
from operator import attrgetter
from types import MappingProxyType, ModuleType
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


def _recorder() -> tuple[list[Any], Any]:
    seen: list[Any] = []

    def record(value: Any) -> Any:
        seen.append(value)
        return value

    return seen, record


class TestPart1Composition:
    def test_compose_applies_right_to_left(self, main: ModuleType) -> None:
        double_after_inc = main.compose(lambda x: x * 2, lambda x: x + 1)
        assert double_after_inc(3) == 8, "compose(f, g)(x) must equal f(g(x))"

    def test_compose_without_functions_is_identity(self, main: ModuleType) -> None:
        marker = object()
        assert main.compose()(marker) is marker, "compose() with no functions must return its argument"

    def test_pipe_applies_left_to_right(self, main: ModuleType) -> None:
        assert main.pipe(3, lambda x: x + 1, lambda x: x * 2) == 8, "pipe must apply functions in order"

    def test_pipe_without_functions_returns_value(self, main: ModuleType) -> None:
        marker = object()
        assert main.pipe(marker) is marker, "pipe with no functions must return the value unchanged"

    def test_pipe_and_compose_do_not_mutate_payload(self, main: ModuleType) -> None:
        payload = [3, 1, 2]
        snapshot = list(payload)
        result = main.pipe(payload, sorted, lambda xs: [x * 10 for x in xs])
        assert result == [10, 20, 30], "pipe result is wrong"
        assert payload == snapshot, "pipe must not mutate the payload it was given"

        composed = main.compose(lambda xs: [x + 1 for x in xs], sorted)
        assert composed(payload) == [2, 3, 4], "compose result is wrong"
        assert payload == snapshot, "compose must not mutate the payload it was given"

    def test_curry_accumulates_across_calls(self, main: ModuleType) -> None:
        add3 = main.curry(lambda a, b, c: a + b + c, 3)
        assert add3(1)(2)(3) == 6, "one argument per call must accumulate"
        assert add3(1, 2)(3) == 6, "several arguments per call must accumulate"
        assert add3(1, 2, 3) == 6, "all arguments in one call must work"

    def test_curry_partial_applications_are_independent(self, main: ModuleType) -> None:
        base = main.curry(lambda a, b, c: a + b + c, 3)(10)
        assert base(1)(1) == 12, "partial application result is wrong"
        assert base(2)(2) == 14, "reusing a partial application must not leak earlier arguments"

    def test_curry_rejects_non_positive_arity(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.curry(lambda: 0, 0)

    def test_partial_right_appends_bound_arguments(self, main: ModuleType) -> None:
        subtract = main.partial_right(lambda a, b: a - b, 1)
        assert subtract(10) == 9, "partial_right must put bound arguments after the call arguments"

    def test_partial_right_passes_keyword_arguments_through(self, main: ModuleType) -> None:
        def label(value: int, unit: str, *, prefix: str, suffix: str) -> str:
            return f"{prefix}{value}{unit}{suffix}"

        wrapped = main.partial_right(label, "kg")
        assert wrapped(7, prefix="<", suffix=">") == "<7kg>", "keyword arguments must pass through unchanged"


class TestPart2ImmutableUpdates:
    def test_assoc_in_creates_missing_intermediate_dicts(self, main: ModuleType) -> None:
        result = main.assoc_in({}, ["user", "prefs", "theme"], "dark")
        assert result == {"user": {"prefs": {"theme": "dark"}}}, "missing intermediate keys must be created"

    def test_assoc_in_never_mutates_input(self, main: ModuleType) -> None:
        data = {"user": {"name": "ada", "prefs": {"theme": "light"}}, "tags": ["x"]}
        snapshot = copy.deepcopy(data)
        result = main.assoc_in(data, ["user", "prefs", "theme"], "dark")
        assert result["user"]["prefs"]["theme"] == "dark", "new value missing from result"
        assert data == snapshot, "assoc_in must not mutate its input, including nested dicts"

    def test_untouched_branches_are_shared(self, main: ModuleType) -> None:
        data = {"user": {"name": "ada"}, "tags": ["x"]}
        result = main.assoc_in(data, ["user", "name"], "grace")
        assert result["tags"] is data["tags"], "untouched subtrees must be shared, not copied"
        assert result["user"] is not data["user"], "the changed path must be a fresh object"

    def test_update_in_list_index_keeps_list_type(self, main: ModuleType) -> None:
        data = {"items": [1, 2, 3]}
        result = main.update_in(data, ["items", 1], lambda v: v * 10)
        assert result == {"items": [1, 20, 3]}, "list index update is wrong"
        assert isinstance(result["items"], list), "lists must stay lists"
        assert data == {"items": [1, 2, 3]}, "update_in must not mutate its input"

    def test_assoc_in_tuple_stays_tuple(self, main: ModuleType) -> None:
        result = main.assoc_in({"pair": (1, 2)}, ["pair", 0], 9)
        assert result == {"pair": (9, 2)}, "tuple update is wrong"
        assert isinstance(result["pair"], tuple), "tuples must stay tuples"

    def test_index_out_of_range_raises_index_error(self, main: ModuleType) -> None:
        with pytest.raises(IndexError):
            main.assoc_in({"items": [1]}, ["items", 5], 0)

    def test_descending_through_scalar_raises_type_error(self, main: ModuleType) -> None:
        with pytest.raises(TypeError):
            main.assoc_in({"a": 1}, ["a", "b"], 2)

    def test_empty_path_raises_value_error(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.assoc_in({}, [], 1)
        with pytest.raises(ValueError):
            main.update_in({}, [], lambda v: v)

    def test_update_in_applies_fn_to_current_value(self, main: ModuleType) -> None:
        data = {"n": {"count": 1}}
        assert main.update_in(data, ["n", "count"], lambda c: c + 1) == {"n": {"count": 2}}
        assert data == {"n": {"count": 1}}, "update_in must not mutate its input"

    def test_update_in_missing_leaf_receives_none(self, main: ModuleType) -> None:
        received: list[Any] = []

        def fn(value: Any) -> int:
            received.append(value)
            return 1

        assert main.update_in({}, ["x"], fn) == {"x": 1}
        assert received == [None], "a missing leaf must be passed to fn as None"

    def test_assoc_in_accepts_read_only_mapping(self, main: ModuleType) -> None:
        result = main.assoc_in(MappingProxyType({"a": 1}), ["b"], 2)
        assert result == {"a": 1, "b": 2}, "read-only mappings must be accepted"
        assert isinstance(result, dict), "result must be a plain dict"

    def test_dissoc_removes_keys_without_mutation(self, main: ModuleType) -> None:
        source = {"a": 1, "b": 2, "c": 3}
        assert main.dissoc(source, "a", "c") == {"b": 2}, "dissoc removed the wrong keys"
        assert source == {"a": 1, "b": 2, "c": 3}, "dissoc must not mutate its input"

    def test_dissoc_ignores_missing_keys_and_accepts_proxy(self, main: ModuleType) -> None:
        proxy = MappingProxyType({"a": 1, "b": 2})
        assert main.dissoc(proxy, "a", "zzz") == {"b": 2}, "missing keys must be ignored"


def _add(main: ModuleType, sku: str, price: int = 100, quantity: int = 1) -> Any:
    return main.AddItem(sku=sku, name=sku.title(), unit_price_cents=price, quantity=quantity)


class TestPart3CartReducer:
    def test_add_item_appends_new_line(self, main: ModuleType) -> None:
        state = main.cart_reducer(main.CartState(), _add(main, "mug", 1200))
        assert state.items == (main.LineItem("mug", "Mug", 1200, 1),), "AddItem must append a new line"

    def test_adding_existing_sku_merges_and_keeps_position(self, main: ModuleType) -> None:
        state = main.run_actions(
            main.CartState(), [_add(main, "a"), _add(main, "b"), _add(main, "a", quantity=2)]
        )
        assert [item.sku for item in state.items] == ["a", "b"], "existing line must keep its position"
        assert [item.quantity for item in state.items] == [3, 1], "quantities must be merged"

    def test_reducer_never_mutates_previous_state(self, main: ModuleType) -> None:
        before = main.run_actions(main.CartState(), [_add(main, "a", quantity=2)])
        frozen_copy = copy.deepcopy(before)
        after = main.cart_reducer(before, _add(main, "a", quantity=5))
        assert before == frozen_copy, "reducer must not change the previous state"
        assert before.items[0].quantity == 2, "previous line quantity must be unchanged"
        assert after.items[0].quantity == 7, "new state must carry the merged quantity"
        assert after is not before, "reducer must return a new state object"

    def test_state_and_items_are_immutable(self, main: ModuleType) -> None:
        state = main.run_actions(main.CartState(), [_add(main, "a")])
        assert isinstance(state.items, tuple), "items must be stored as a tuple"
        with pytest.raises(dataclasses.FrozenInstanceError):
            state.coupon = "SAVE10"  # type: ignore[misc]

    def test_remove_and_zero_quantity_remove_line(self, main: ModuleType) -> None:
        state = main.run_actions(main.CartState(), [_add(main, "a"), _add(main, "b"), _add(main, "c")])
        removed = main.cart_reducer(state, main.RemoveItem("b"))
        assert [i.sku for i in removed.items] == ["a", "c"], "RemoveItem removed the wrong line"
        zeroed = main.cart_reducer(state, main.SetQuantity("a", 0))
        assert [i.sku for i in zeroed.items] == ["b", "c"], "SetQuantity(0) must remove the line"

    def test_remove_unknown_sku_is_noop(self, main: ModuleType) -> None:
        state = main.run_actions(main.CartState(), [_add(main, "a")])
        assert main.cart_reducer(state, main.RemoveItem("ghost")) == state, "unknown sku must be a no-op"

    def test_invalid_quantities_and_skus_raise(self, main: ModuleType) -> None:
        state = main.run_actions(main.CartState(), [_add(main, "a")])
        with pytest.raises(ValueError):
            main.cart_reducer(state, _add(main, "b", quantity=0))
        with pytest.raises(ValueError):
            main.cart_reducer(state, main.SetQuantity("a", -1))
        with pytest.raises(ValueError):
            main.cart_reducer(state, main.SetQuantity("ghost", 2))

    def test_coupon_total_uses_floored_discount(self, main: ModuleType) -> None:
        state = main.run_actions(
            main.CartState(), [_add(main, "pen", 333, quantity=3), main.ApplyCoupon("SAVE10")]
        )
        assert main.subtotal_cents(state) == 999, "subtotal is wrong"
        assert main.total_cents(state) == 900, "10% of 999 floors to 99 cents off"
        half = main.cart_reducer(state, main.ApplyCoupon("HALF"))
        assert main.total_cents(half) == 500, "50% of 999 floors to 499 cents off"

    def test_unknown_coupon_rejected_and_none_clears(self, main: ModuleType) -> None:
        state = main.cart_reducer(main.CartState(), main.ApplyCoupon("SAVE10"))
        with pytest.raises(ValueError):
            main.cart_reducer(state, main.ApplyCoupon("BOGUS"))
        cleared = main.cart_reducer(state, main.ApplyCoupon(None))
        assert cleared.coupon is None, "ApplyCoupon(None) must clear the coupon"

    def test_clear_cart_resets_items_and_coupon(self, main: ModuleType) -> None:
        state = main.run_actions(
            main.CartState(), [_add(main, "a"), main.ApplyCoupon("HALF"), main.ClearCart()]
        )
        assert state == main.CartState(), "ClearCart must reset items and coupon"

    def test_unknown_action_raises_type_error(self, main: ModuleType) -> None:
        with pytest.raises(TypeError):
            main.cart_reducer(main.CartState(), "checkout")  # type: ignore[arg-type]

    def test_run_actions_does_not_consume_or_modify_action_list(self, main: ModuleType) -> None:
        actions = [_add(main, "a"), _add(main, "a")]
        snapshot = list(actions)
        state = main.run_actions(main.CartState(), actions)
        assert state.items[0].quantity == 2, "both actions must be applied"
        assert actions == snapshot, "run_actions must not modify the action list"


def _opened(main: ModuleType) -> Any:
    return main.Opened("acc-1", "Ada")


class TestPart4EventSourcing:
    def test_replay_derives_balance_from_log(self, main: ModuleType) -> None:
        events = [
            _opened(main),
            main.Deposited(10_000),
            main.Withdrawn(2_500),
            main.Deposited(500),
        ]
        assert main.replay(events) == main.Account("acc-1", "Ada", 8_000, "open"), "replay balance is wrong"

    def test_replay_empty_log_raises(self, main: ModuleType) -> None:
        with pytest.raises(ValueError, match="empty"):
            main.replay([])

    def test_overdraft_is_rejected(self, main: ModuleType) -> None:
        events = [_opened(main), main.Deposited(100), main.Withdrawn(101)]
        with pytest.raises(ValueError, match="insufficient funds"):
            main.replay(events)

    def test_frozen_account_blocks_movements_until_unfrozen(self, main: ModuleType) -> None:
        blocked = [_opened(main), main.Deposited(100), main.Frozen(), main.Deposited(50)]
        with pytest.raises(ValueError, match="frozen"):
            main.replay(blocked)
        recovered = [
            _opened(main),
            main.Deposited(100),
            main.Frozen(),
            main.Unfrozen(),
            main.Deposited(50),
        ]
        assert main.replay(recovered).balance_cents == 150, "unfreezing must restore movements"

    def test_opening_twice_and_acting_before_open_are_rejected(self, main: ModuleType) -> None:
        with pytest.raises(ValueError, match="already opened"):
            main.replay([_opened(main), _opened(main)])
        with pytest.raises(ValueError, match="not opened"):
            main.replay([main.Deposited(5)])

    def test_non_positive_amounts_are_rejected(self, main: ModuleType) -> None:
        with pytest.raises(ValueError, match="positive"):
            main.replay([_opened(main), main.Deposited(0)])

    def test_snapshots_reflect_state_after_each_event(self, main: ModuleType) -> None:
        events = [_opened(main), main.Deposited(100), main.Withdrawn(40)]
        snaps = main.snapshots(events)
        assert len(snaps) == len(events), "there must be one snapshot per event"
        assert snaps[1] == main.Account("acc-1", "Ada", 100, "open"), "second snapshot is wrong"
        assert snaps[-1] == main.replay(events), "last snapshot must equal replay"

    def test_replay_does_not_mutate_event_log(self, main: ModuleType) -> None:
        events = [_opened(main), main.Deposited(100), main.Withdrawn(40)]
        snapshot = copy.deepcopy(events)
        main.replay(events)
        main.snapshots(events)
        assert events == snapshot, "replay and snapshots must not change the event log"
        with pytest.raises(dataclasses.FrozenInstanceError):
            events[1].amount_cents = 1  # type: ignore[misc]

    def test_decide_withdraw_returns_event_without_touching_account(self, main: ModuleType) -> None:
        account = main.replay([_opened(main), main.Deposited(100)])
        before = copy.deepcopy(account)
        event = main.decide_withdraw(account, 30)
        assert isinstance(event, main.Withdrawn), "decide_withdraw must return a Withdrawn event"
        assert event.amount_cents == 30, "event carries the wrong amount"
        assert account == before, "decide_withdraw must not change the account"
        with pytest.raises(ValueError, match="insufficient funds"):
            main.decide_withdraw(account, 101)


class TestPart5ResultAndParsing:
    def test_ok_map_and_unwrap(self, main: ModuleType) -> None:
        mapped = main.Ok(2).map(lambda x: x * 5)
        assert mapped == main.Ok(10), "Ok.map must transform the value"
        assert mapped.unwrap_or(0) == 10, "Ok.unwrap_or must return the value"
        assert mapped.is_ok() is True

    def test_err_short_circuits_map_and_and_then(self, main: ModuleType) -> None:
        calls, record = _recorder()
        err = main.Err("boom")
        assert err.map(record) == main.Err("boom"), "Err.map must return the error unchanged"
        assert err.and_then(lambda v: main.Ok(record(v))) == main.Err("boom"), "Err.and_then must short-circuit"
        assert calls == [], "callbacks must not run on Err"
        assert err.unwrap_or(7) == 7, "Err.unwrap_or must return the default"
        assert err.is_ok() is False

    def test_and_then_chains_results(self, main: ModuleType) -> None:
        assert main.Ok("8080").and_then(main.parse_port) == main.Ok(8080), "and_then must chain Results"
        failed = main.Ok("abc").and_then(main.parse_port)
        assert isinstance(failed, main.Err), "a failing step must produce Err"
        assert "integer" in failed.error, "error message should explain the failure"

    def test_map_err_transforms_only_errors(self, main: ModuleType) -> None:
        assert main.Err("x").map_err(str.upper) == main.Err("X"), "map_err must transform the error"
        assert main.Ok(1).map_err(str.upper) == main.Ok(1), "map_err must leave Ok alone"

    def test_try_call_wraps_exceptions(self, main: ModuleType) -> None:
        assert main.try_call(int, "42") == main.Ok(42), "successful call must yield Ok"
        failed = main.try_call(int, "x")
        assert isinstance(failed, main.Err), "exceptions must be captured as Err"
        assert isinstance(failed.error, ValueError), "the captured error must be the exception"

    def test_collect_stops_at_first_error(self, main: ModuleType) -> None:
        produced: list[str] = []

        def results() -> Any:
            for item in [main.Ok(1), main.Err("bad"), main.Ok(3)]:
                produced.append(str(item))
                yield item

        assert main.collect(results()) == main.Err("bad"), "collect must return the first Err"
        assert len(produced) == 2, "collect must stop consuming after the first Err"
        assert main.collect([main.Ok(1), main.Ok(2)]) == main.Ok([1, 2]), "all Ok must collect to a list"

    def test_parse_ports_uses_collect(self, main: ModuleType) -> None:
        assert main.parse_ports("80, 443,8080") == main.Ok([80, 443, 8080]), "ports must be parsed in order"
        assert isinstance(main.parse_ports("80,,443"), main.Err), "an empty entry must be an error"
        assert "between 1 and 65535" in main.parse_ports("80,70000").error, "range error expected"

    def test_parse_bool_is_case_insensitive(self, main: ModuleType) -> None:
        assert main.parse_bool(" YES ") == main.Ok(True), "yes must be True"
        assert main.parse_bool("off") == main.Ok(False), "off must be False"
        assert isinstance(main.parse_bool("maybe"), main.Err), "unknown words must be Err"

    def test_parse_config_happy_path(self, main: ModuleType) -> None:
        text = """
        # service config
        host = api.local
        port = 8080
        debug = yes
        workers = 8
        """
        assert main.parse_config(text) == main.Ok(main.Config("api.local", 8080, True, 8))

    def test_parse_config_defaults(self, main: ModuleType) -> None:
        assert main.parse_config("host=x\nport=1") == main.Ok(main.Config("x", 1, False, 4)), (
            "debug defaults to False and workers to 4"
        )

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("port=80", "missing required key 'host'"),
            ("host=a\nport=80\nhost=b", "duplicate key 'host'"),
            ("host=a\nport=80\ntimeout=5", "unknown key 'timeout'"),
            ("host=a\nport 80", "line 2: expected 'key = value'"),
            ("host=a\nport=99999", "port must be between 1 and 65535"),
            ("host=a\nport=abc", "port must be an integer"),
            ("host=a\nport=80\ndebug=maybe", "expected boolean, got 'maybe'"),
            ("host=a\nport=80\nworkers=0", "workers must be between 1 and 64"),
            ("host=\nport=80", "host must not be empty"),
        ],
    )
    def test_parse_config_reports_errors_as_values(self, main: ModuleType, text: str, expected: str) -> None:
        result = main.parse_config(text)
        assert isinstance(result, main.Err), f"expected Err for {text!r}"
        assert expected in result.error, f"error for {text!r} should mention {expected!r}, got {result.error!r}"

    @settings(
        derandomize=True,
        max_examples=200,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(text=st.text(max_size=120))
    def test_parse_config_never_raises(self, main: ModuleType, text: str) -> None:
        result = main.parse_config(text)
        assert isinstance(result, (main.Ok, main.Err)), "parse_config must return a Result for any text"


_SIGNUP_OK = {"username": "ada_99", "age": 36, "email": "ada@example.com", "tags": ["admin"]}


class TestPart6Validators:
    def test_is_type_rejects_bool_for_int(self, main: ModuleType) -> None:
        assert main.is_type(int)(True) == ["expected int, got bool"], "bool must not pass as int"
        assert main.is_type(int)(3) == [], "int must pass"

    def test_min_length_and_matches_judge_strings_only(self, main: ModuleType) -> None:
        assert main.min_length(3)("ab") == ["length must be at least 3"], "short string must fail"
        assert main.min_length(3)(None) == [], "non-strings are outside min_length's domain"
        pattern = r"[a-z]+"
        assert main.matches(pattern)("abc1") == [f"must match pattern {pattern!r}"], "pattern mismatch expected"
        assert main.matches(pattern)("abc") == [], "full match must pass"

    def test_in_range_is_inclusive_and_rejects_bool(self, main: ModuleType) -> None:
        assert main.in_range(13, 120)(12) == ["must be between 13 and 120"], "below range must fail"
        assert main.in_range(13, 120)(13) == [], "lower bound is inclusive"
        assert main.in_range(13, 120)(120) == [], "upper bound is inclusive"
        assert main.in_range(13, 120)("x") == [], "non-numbers are outside in_range's domain"

    def test_all_of_collects_every_error_in_order(self, main: ModuleType) -> None:
        validator = main.all_of(main.min_length(3), main.matches(r"[0-9]+"))
        assert validator("A") == [
            "length must be at least 3",
            f"must match pattern {r'[0-9]+'!r}",
        ], "all_of must report every failing validator"

    def test_any_of_short_circuits_on_first_pass(self, main: ModuleType) -> None:
        calls, record = _recorder()

        def passing(value: Any) -> list[str]:
            record(value)
            return []

        def never(value: Any) -> list[str]:
            record(value)
            return ["never"]

        assert main.any_of(passing, never)("x") == [], "any_of must pass when one branch passes"
        assert calls == ["x"], "later branches must not run once one passes"

    def test_any_of_all_failing_concatenates_errors(self, main: ModuleType) -> None:
        validator = main.any_of(main.min_length(5), main.matches(r"[0-9]+"))
        assert validator("ab") == ["length must be at least 5", f"must match pattern {r'[0-9]+'!r}"]

    def test_any_of_without_validators_raises(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.any_of()

    def test_field_reports_missing_and_prefixes_nested_errors(self, main: ModuleType) -> None:
        assert main.field("email", main.is_type(str))({}) == ["email: missing"], "missing key must be reported"
        assert main.field("age", main.in_range(13, 120))({"age": 7}) == [
            "age: must be between 13 and 120"
        ], "nested errors must be prefixed with the field name"

    def test_optional_and_each(self, main: ModuleType) -> None:
        assert main.optional(main.min_length(2))(None) == [], "None must be valid for optional"
        assert main.optional(main.min_length(2))("a") == ["length must be at least 2"]
        assert main.each(main.min_length(1))(["ok", ""]) == ["[1] length must be at least 1"], (
            "each must prefix errors with the element index"
        )
        assert main.each(main.min_length(1))("not-a-list") == [], "non-sequences are outside each's domain"

    def test_validate_signup_accepts_valid_payload(self, main: ModuleType) -> None:
        assert main.validate_signup(dict(_SIGNUP_OK)) == []
        without_tags = {k: v for k, v in _SIGNUP_OK.items() if k != "tags"}
        assert main.validate_signup(without_tags) == [], "tags are optional"

    def test_validate_signup_collects_all_errors(self, main: ModuleType) -> None:
        payload = {"username": "A", "age": 7, "email": "bad", "tags": ["ok", ""]}
        errors = main.validate_signup(payload)
        assert len(errors) == 5, f"expected five messages, got {errors!r}"
        assert errors[0] == "username: length must be at least 3", "first error is wrong"
        assert errors[-1] == "tags: [1] length must be at least 1", "last error is wrong"

    def test_validate_signup_rejects_non_mapping(self, main: ModuleType) -> None:
        assert main.validate_signup([]) == ["expected Mapping, got list"]

    def test_validate_signup_does_not_mutate_payload(self, main: ModuleType) -> None:
        payload = {"username": "A", "age": 7, "email": "bad", "tags": ["ok", ""]}
        snapshot = copy.deepcopy(payload)
        main.validate_signup(payload)
        assert payload == snapshot, "validators must not mutate the payload"


ROWS: list[dict[str, str]] = [
    {"region": " EU ", "product": "Mug ", "units": "3", "unit_price": "12.50", "sold_on": "2026-01-05"},
    {"region": "us", "product": "Mug", "units": "0", "unit_price": "12.50", "sold_on": "2026-01-06"},
    {"region": "  ", "product": "Pen", "units": "4", "unit_price": "1.25", "sold_on": "2026-01-06"},
    {"region": "us", "product": "Desk", "units": "1", "unit_price": "200", "sold_on": "2026-01-07"},
    {"region": "eu", "product": "Pen", "units": "10", "unit_price": "1.25", "sold_on": "2026-01-08"},
]


class TestPart7EtlAndReports:
    @pytest.mark.parametrize(
        ("raw", "cents"),
        [("12.5", 1250), ("12", 1200), ("0.01", 1), ("0", 0)],
    )
    def test_parse_cents_is_exact(self, main: ModuleType, raw: str, cents: int) -> None:
        assert main.parse_cents(raw) == cents, f"{raw!r} must parse to {cents} cents"

    @pytest.mark.parametrize("raw", ["12.345", "-1", "abc", "", "nan"])
    def test_parse_cents_rejects_bad_money(self, main: ModuleType, raw: str) -> None:
        with pytest.raises(ValueError):
            main.parse_cents(raw)

    def test_clean_sales_filters_normalises_and_keeps_order(self, main: ModuleType) -> None:
        sales = main.clean_sales(ROWS)
        assert [(s.region, s.product) for s in sales] == [("eu", "Mug"), ("us", "Desk"), ("eu", "Pen")], (
            "blank-region and zero-unit rows must be dropped; region lowercased, product stripped"
        )
        assert sales[0] == main.Sale("eu", "Mug", 3, 1250, date(2026, 1, 5)), "parsed sale is wrong"

    def test_clean_sales_accepts_any_iterable(self, main: ModuleType) -> None:
        assert main.clean_sales(iter(ROWS)) == main.clean_sales(ROWS), "generators must work too"

    def test_clean_sales_does_not_mutate_rows(self, main: ModuleType) -> None:
        rows = copy.deepcopy(ROWS)
        main.clean_sales(rows)
        assert rows == ROWS, "clean_sales must not mutate the input rows"

    def test_negative_units_rejected(self, main: ModuleType) -> None:
        bad = [{"region": "eu", "product": "X", "units": "-2", "unit_price": "1", "sold_on": "2026-01-01"}]
        with pytest.raises(ValueError):
            main.clean_sales(bad)

    def test_totals_by_sums_revenue_with_sorted_keys(self, main: ModuleType) -> None:
        totals = main.totals_by(main.clean_sales(ROWS), attrgetter("region"))
        assert list(totals) == ["eu", "us"], "keys must be sorted ascending"
        assert totals == {"eu": 5000, "us": 20000}, "revenue sums are wrong"

    def test_ranked_breaks_ties_by_name(self, main: ModuleType) -> None:
        assert main.ranked({"b": 100, "a": 100, "c": 300}) == [("c", 300), ("a", 100), ("b", 100)], (
            "sort by total descending, then name ascending"
        )

    def test_distinct_regions_are_uppercase_and_sorted(self, main: ModuleType) -> None:
        assert main.distinct_regions(main.clean_sales(ROWS)) == ("EU", "US")

    def test_format_cents(self, main: ModuleType) -> None:
        assert main.format_cents(123_456_789) == "$1,234,567.89", "thousands separators and cents are wrong"
        assert main.format_cents(5) == "$0.05", "cents must be zero-padded"
        assert main.format_cents(1234, currency="EUR ") == "EUR 12.34", "currency prefix must be honoured"
        with pytest.raises(ValueError):
            main.format_cents(-1)

    def test_build_report_end_to_end(self, main: ModuleType) -> None:
        assert main.build_report(ROWS) == ["us: $200.00", "eu: $50.00"], "report lines are wrong"
        assert main.build_report(ROWS, currency="EUR ")[0] == "us: EUR 200.00"

    def test_build_report_does_not_mutate_input(self, main: ModuleType) -> None:
        rows = copy.deepcopy(ROWS)
        main.build_report(rows)
        assert rows == ROWS, "build_report must not mutate its input"
