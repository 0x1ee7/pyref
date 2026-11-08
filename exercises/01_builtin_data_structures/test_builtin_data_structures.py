"""Acceptance tests for the built-in data structures exercise."""

from __future__ import annotations

import copy
from collections import Counter
from decimal import Decimal
from types import ModuleType
from collections.abc import Iterator

import pytest


class FakeClock:
    """Manually advanced monotonic clock."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# Part 1: TTL + LRU cache
# ---------------------------------------------------------------------------


class TestPart1TTLCache:
    def test_get_returns_value_or_default_on_miss(self, main: ModuleType) -> None:
        cache = main.TTLCache(maxsize=4, ttl=10, clock=FakeClock())
        cache.put("a", 1)
        assert cache.get("a") == 1, "live entry should be returned"
        assert cache.get("b") is None, "missing key should return None by default"
        assert cache.get("b", "x") == "x", "missing key should return the given default"

    def test_put_replaces_value_and_refreshes_expiry(self, main: ModuleType) -> None:
        clock = FakeClock()
        cache = main.TTLCache(maxsize=4, ttl=10, clock=clock)
        cache.put("a", 1)
        clock.now = 8
        cache.put("a", 2)
        clock.now = 15  # original expiry (10) passed, refreshed expiry (18) not yet
        assert cache.get("a") == 2, "re-put should replace the value and restart the TTL"

    def test_expiry_boundary_is_inclusive_miss(self, main: ModuleType) -> None:
        clock = FakeClock()
        cache = main.TTLCache(maxsize=4, ttl=10, clock=clock)
        cache.put("a", 1)
        clock.now = 9.999
        assert cache.get("a") == 1, "entry should still be live just before expiry"
        clock.now = 10.0
        assert cache.get("a") is None, "entry should be expired at exactly written_at + ttl"

    def test_get_does_not_extend_ttl(self, main: ModuleType) -> None:
        clock = FakeClock()
        cache = main.TTLCache(maxsize=4, ttl=10, clock=clock)
        cache.put("a", 1)
        clock.now = 5
        assert cache.get("a") == 1
        clock.now = 10
        assert cache.get("a") is None, "reading an entry must not extend its lifetime"

    def test_lru_eviction_respects_recency(self, main: ModuleType) -> None:
        cache = main.TTLCache(maxsize=2, ttl=100, clock=FakeClock())
        cache.put("a", 1)
        cache.put("b", 2)
        assert cache.get("a") == 1  # a becomes most recently used
        cache.put("c", 3)  # must evict b, the least recently used
        assert cache.keys() == ["a", "c"], "keys() should be LRU -> MRU after eviction"

    def test_contains_does_not_touch_recency_or_counters(self, main: ModuleType) -> None:
        cache = main.TTLCache(maxsize=2, ttl=100, clock=FakeClock())
        cache.put("a", 1)
        cache.put("b", 2)
        assert "a" in cache
        assert "zzz" not in cache
        assert (cache.hits, cache.misses) == (0, 0), "__contains__ must not count as get()"
        cache.put("c", 3)  # a is still the LRU entry, so it is evicted
        assert cache.keys() == ["b", "c"], "__contains__ must not refresh recency"

    def test_expired_entries_do_not_count_toward_capacity(self, main: ModuleType) -> None:
        clock = FakeClock()
        cache = main.TTLCache(maxsize=2, ttl=10, clock=clock)
        cache.put("a", 1)  # expires at 10
        clock.now = 5
        cache.put("b", 2)  # expires at 15
        clock.now = 12
        cache.put("c", 3)  # a is expired and must not force eviction of live b
        assert cache.keys() == ["b", "c"], "expired entries should be purged before eviction"

    def test_len_excludes_expired_entries(self, main: ModuleType) -> None:
        clock = FakeClock()
        cache = main.TTLCache(maxsize=10, ttl=10, clock=clock)
        cache.put("a", 1)
        clock.now = 5
        cache.put("b", 2)
        clock.now = 12
        assert len(cache) == 1, "len() should count only live entries"

    def test_hit_and_miss_counters(self, main: ModuleType) -> None:
        clock = FakeClock()
        cache = main.TTLCache(maxsize=4, ttl=10, clock=clock)
        cache.put("a", 1)
        cache.get("a")
        cache.get("b")
        cache.get("a")
        assert (cache.hits, cache.misses) == (2, 1), "hits/misses should count get() outcomes"
        clock.now = 10
        cache.get("a")  # expired counts as a miss
        assert (cache.hits, cache.misses) == (2, 2), "an expired entry read should be a miss"

    @pytest.mark.parametrize(("maxsize", "ttl"), [(0, 1.0), (-1, 1.0), (1, 0), (1, -5)])
    def test_invalid_configuration_rejected(self, main: ModuleType, maxsize: int, ttl: float) -> None:
        with pytest.raises(ValueError):
            main.TTLCache(maxsize=maxsize, ttl=ttl, clock=FakeClock())


# ---------------------------------------------------------------------------
# Part 2: Sliding-window rate limiter
# ---------------------------------------------------------------------------


class TestPart2SlidingWindowLimiter:
    def test_allows_up_to_limit_then_denies(self, main: ModuleType) -> None:
        limiter = main.SlidingWindowLimiter(limit=3, window=60, clock=FakeClock())
        results = [limiter.allow("client") for _ in range(4)]
        assert results == [True, True, True, False], "exactly `limit` requests fit in the window"

    def test_window_slides_at_exact_boundary(self, main: ModuleType) -> None:
        clock = FakeClock()
        limiter = main.SlidingWindowLimiter(limit=2, window=10, clock=clock)
        assert limiter.allow("c") is True  # t=0
        clock.now = 4
        assert limiter.allow("c") is True  # t=4
        clock.now = 5
        assert limiter.allow("c") is False, "window is full at t=5"
        clock.now = 10
        assert limiter.allow("c") is True, "event at t=0 is exactly `window` old and must have left"
        assert limiter.allow("c") is False, "window now holds events at t=4 and t=10"

    def test_denied_requests_are_not_recorded(self, main: ModuleType) -> None:
        clock = FakeClock()
        limiter = main.SlidingWindowLimiter(limit=1, window=10, clock=clock)
        assert limiter.allow("c") is True  # t=0
        clock.now = 5
        assert [limiter.allow("c") for _ in range(3)] == [False, False, False]
        clock.now = 10
        assert limiter.allow("c") is True, "denied attempts at t=5 must not extend the window"

    def test_clients_are_isolated(self, main: ModuleType) -> None:
        limiter = main.SlidingWindowLimiter(limit=1, window=60, clock=FakeClock())
        assert limiter.allow("alice") is True
        assert limiter.allow("bob") is True, "bob's budget is independent of alice's"
        assert limiter.allow("alice") is False

    def test_remaining_is_side_effect_free(self, main: ModuleType) -> None:
        limiter = main.SlidingWindowLimiter(limit=3, window=60, clock=FakeClock())
        assert limiter.remaining("c") == 3
        assert limiter.allow("c") is True
        assert limiter.remaining("c") == 2
        assert limiter.remaining("c") == 2, "remaining() must not consume budget"
        limiter.allow("c")
        limiter.allow("c")
        limiter.allow("c")  # denied
        assert limiter.remaining("c") == 0

    def test_retry_after_reports_wait_until_oldest_expires(self, main: ModuleType) -> None:
        clock = FakeClock()
        limiter = main.SlidingWindowLimiter(limit=2, window=10, clock=clock)
        assert limiter.retry_after("c") == 0.0, "no wait needed while budget remains"
        limiter.allow("c")  # t=0
        clock.now = 3
        limiter.allow("c")  # t=3
        clock.now = 4
        assert limiter.retry_after("c") == pytest.approx(6.0), "oldest event at t=0 leaves at t=10"
        clock.now = 6
        assert limiter.retry_after("c") == pytest.approx(4.0)
        clock.now = 10
        assert limiter.retry_after("c") == 0.0
        assert limiter.allow("c") is True

    def test_prune_drops_idle_clients_only(self, main: ModuleType) -> None:
        clock = FakeClock()
        limiter = main.SlidingWindowLimiter(limit=1, window=5, clock=clock)
        limiter.allow("a")  # t=0
        clock.now = 2
        limiter.allow("b")  # t=2
        clock.now = 6
        assert limiter.prune() == 1, "only 'a' has no live events at t=6"
        clock.now = 7
        assert limiter.prune() == 1, "'b' expires at t=7"
        assert limiter.prune() == 0, "nothing left to prune"

    @pytest.mark.parametrize(("limit", "window"), [(0, 1.0), (-1, 1.0), (1, 0), (1, -1.0)])
    def test_invalid_configuration_rejected(self, main: ModuleType, limit: int, window: float) -> None:
        with pytest.raises(ValueError):
            main.SlidingWindowLimiter(limit=limit, window=window, clock=FakeClock())


# ---------------------------------------------------------------------------
# Part 3: Top-K product ranking
# ---------------------------------------------------------------------------


class TestPart3TopKProducts:
    def test_aggregates_units_per_sku(self, main: ModuleType) -> None:
        sales = [("A", 2), ("B", 5), ("A", 4)]
        assert main.top_k_products(sales, 5) == [("A", 6), ("B", 5)]

    def test_orders_by_units_desc_then_sku_asc(self, main: ModuleType) -> None:
        sales = [("Z", 3), ("A", 3), ("M", 3), ("Q", 9)]
        assert main.top_k_products(sales, 4) == [("Q", 9), ("A", 3), ("M", 3), ("Z", 3)], (
            "ties must be broken by SKU ascending"
        )

    def test_k_limits_result_size(self, main: ModuleType) -> None:
        sales = [("Z", 3), ("A", 3), ("M", 3), ("Q", 9)]
        assert main.top_k_products(sales, 2) == [("Q", 9), ("A", 3)]

    def test_k_edge_values(self, main: ModuleType) -> None:
        sales = [("A", 1), ("B", 2)]
        assert main.top_k_products(sales, 0) == [], "k=0 yields nothing"
        assert main.top_k_products(sales, -3) == [], "negative k yields nothing"
        assert main.top_k_products(sales, 100) == [("B", 2), ("A", 1)], "k beyond distinct SKUs yields all"
        assert main.top_k_products([], 3) == [], "empty input yields nothing"

    def test_accepts_one_shot_iterator(self, main: ModuleType) -> None:
        stream = iter([("A", 1), ("B", 4)])
        assert main.top_k_products(stream, 1) == [("B", 4)]

    def test_negative_units_rejected(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.top_k_products([("A", 2), ("A", -1)], 3)

    def test_large_stream_matches_reference_counts(self, main: ModuleType) -> None:
        def lines() -> Iterator[tuple[str, int]]:
            return ((f"SKU-{i % 37:03d}", i % 5) for i in range(100_000))

        expected_totals: Counter[str] = Counter()
        for sku, units in lines():
            expected_totals[sku] += units
        expected = sorted(expected_totals.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        assert main.top_k_products(lines(), 5) == expected


# ---------------------------------------------------------------------------
# Part 4: Layered configuration
# ---------------------------------------------------------------------------


class TestPart4LayeredConfig:
    def test_deep_merge_merges_nested_dicts(self, main: ModuleType) -> None:
        base = {"db": {"host": "a", "port": 5432}, "debug": False}
        override = {"db": {"port": 6000}}
        assert main.deep_merge(base, override) == {"db": {"host": "a", "port": 6000}, "debug": False}

    def test_deep_merge_replaces_lists(self, main: ModuleType) -> None:
        assert main.deep_merge({"hosts": [1, 2]}, {"hosts": [3]}) == {"hosts": [3]}, (
            "lists must be replaced, not concatenated"
        )

    def test_deep_merge_none_overrides_and_type_switches(self, main: ModuleType) -> None:
        assert main.deep_merge({"x": {"a": 1}}, {"x": None}) == {"x": None}
        assert main.deep_merge({"a": 1}, {"a": {"b": 2}}) == {"a": {"b": 2}}
        assert main.deep_merge({"a": {"b": 2}}, {"a": "x"}) == {"a": "x"}

    def test_deep_merge_does_not_mutate_inputs(self, main: ModuleType) -> None:
        base = {"db": {"host": "a"}, "tags": ["x"]}
        override = {"db": {"port": 1}, "tags": ["y"]}
        base_before = copy.deepcopy(base)
        override_before = copy.deepcopy(override)
        main.deep_merge(base, override)
        assert base == base_before, "base must not be mutated"
        assert override == override_before, "override must not be mutated"

    def test_deep_merge_result_shares_no_mutable_state(self, main: ModuleType) -> None:
        base = {"db": {"host": "a"}, "tags": ["x"]}
        result = main.deep_merge(base, {})
        result["db"]["host"] = "changed"
        result["tags"].append("y")
        assert base == {"db": {"host": "a"}, "tags": ["x"]}, "result must not alias nested inputs"

    def test_parse_env_maps_double_underscore_to_nesting(self, main: ModuleType) -> None:
        environ = {"APP_DB__HOST": "db.internal", "APP_DB__POOL__SIZE": "10"}
        assert main.parse_env(environ, "APP") == {"db": {"host": "db.internal", "pool": {"size": 10}}}

    def test_parse_env_coerces_bools_and_ints_only(self, main: ModuleType) -> None:
        environ = {"APP_DEBUG": "TRUE", "APP_RETRIES": "-2", "APP_NAME": "0x1", "APP_OFF": "False"}
        parsed = main.parse_env(environ, "APP")
        assert parsed["debug"] is True
        assert parsed["retries"] == -2 and isinstance(parsed["retries"], int)
        assert parsed["name"] == "0x1", "non-decimal strings must stay strings"
        assert parsed["off"] is False

    def test_parse_env_ignores_other_prefixes(self, main: ModuleType) -> None:
        environ = {"APPLE_X": "1", "APP_Y": "2", "PATH": "/bin"}
        assert main.parse_env(environ, "APP") == {"y": 2}

    @pytest.mark.parametrize("key", ["APP_", "APP_DB____HOST", "APP___X"])
    def test_parse_env_rejects_empty_segments(self, main: ModuleType, key: str) -> None:
        with pytest.raises(ValueError):
            main.parse_env({key: "v"}, "APP")

    def test_precedence_cli_env_file_defaults(self, main: ModuleType) -> None:
        cfg = main.Config(
            defaults={"db": {"host": "d", "port": 1, "pool": {"size": 5}}},
            file={"db": {"host": "f", "port": 2}},
            env={"db": {"host": "e"}},
            cli={"db": {"host": "c"}},
        )
        assert cfg.get("db.host") == "c" and cfg.source("db.host") == "cli"
        assert cfg.get("db.port") == 2 and cfg.source("db.port") == "file"
        assert cfg.get("db.pool.size") == 5 and cfg.source("db.pool.size") == "defaults"

    def test_missing_paths_return_default_and_no_source(self, main: ModuleType) -> None:
        cfg = main.Config(defaults={"db": {"host": "d"}})
        assert cfg.get("nope.x", "fallback") == "fallback"
        assert cfg.get("db.host.deep", "dflt") == "dflt", "path through a scalar is absent"
        assert cfg.source("nope.x") is None

    def test_explicit_none_counts_as_present(self, main: ModuleType) -> None:
        cfg = main.Config(defaults={"feature": True}, file={"feature": None})
        assert cfg.get("feature", "x") is None, "a None in a higher layer still wins"
        assert cfg.source("feature") == "file"

    def test_as_dict_is_fresh_effective_config(self, main: ModuleType) -> None:
        cfg = main.Config(
            defaults={"db": {"host": "d", "port": 1}, "tags": ["a"]},
            file={"db": {"port": 2}},
            cli={"tags": ["z"]},
        )
        effective = cfg.as_dict()
        assert effective == {"db": {"host": "d", "port": 2}, "tags": ["z"]}
        effective["db"]["host"] = "mutated"
        assert cfg.get("db.host") == "d", "as_dict() must return a fresh structure"

    def test_layers_are_snapshotted_at_construction(self, main: ModuleType) -> None:
        defaults = {"db": {"host": "original"}}
        cfg = main.Config(defaults=defaults)
        defaults["db"]["host"] = "mutated"
        assert cfg.get("db.host") == "original", "later mutation of inputs must not leak in"


# ---------------------------------------------------------------------------
# Part 5: Reconciliation and pricing
# ---------------------------------------------------------------------------


class TestPart5ReconciliationAndPricing:
    def test_classifies_missing_unexpected_mismatched(self, main: ModuleType) -> None:
        result = main.reconcile(expected={"A": 5, "B": 3, "C": 2}, counted={"A": 5, "B": 1, "D": 4})
        assert result.missing == frozenset({"C"})
        assert result.unexpected == frozenset({"D"})
        assert result.mismatched == {"B": -2}
        assert result.is_clean is False

    def test_zero_count_is_mismatch_not_missing(self, main: ModuleType) -> None:
        result = main.reconcile(expected={"A": 3}, counted={"A": 0})
        assert result.missing == frozenset(), "a SKU counted as 0 is present, not missing"
        assert result.mismatched == {"A": -3}

    def test_clean_reconciliation(self, main: ModuleType) -> None:
        result = main.reconcile(expected={"A": 1, "B": 2}, counted={"B": 2, "A": 1})
        assert result.is_clean is True
        assert result.missing == frozenset() and result.unexpected == frozenset()
        assert result.mismatched == {}

    def test_mismatched_keys_are_sorted(self, main: ModuleType) -> None:
        result = main.reconcile(expected={"Z": 1, "A": 1, "M": 1}, counted={"Z": 2, "A": 2, "M": 2})
        assert list(result.mismatched) == ["A", "M", "Z"]
        assert all(delta == 1 for delta in result.mismatched.values())

    def test_negative_quantities_rejected(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.reconcile(expected={"A": -1}, counted={})
        with pytest.raises(ValueError):
            main.reconcile(expected={}, counted={"A": -1})

    def test_price_tiers_boundaries(self, main: ModuleType) -> None:
        tiers = main.PriceTiers([(1, Decimal("10.00")), (10, Decimal("9.00")), (50, Decimal("8.50"))])
        assert tiers.unit_price(1) == Decimal("10.00")
        assert tiers.unit_price(9) == Decimal("10.00")
        assert tiers.unit_price(10) == Decimal("9.00"), "boundary quantity belongs to the new tier"
        assert tiers.unit_price(49) == Decimal("9.00")
        assert tiers.unit_price(50) == Decimal("8.50")
        assert tiers.unit_price(1000) == Decimal("8.50")

    def test_price_tiers_accept_unsorted_input(self, main: ModuleType) -> None:
        tiers = main.PriceTiers([(10, Decimal("9.00")), (1, Decimal("10.00"))])
        assert tiers.unit_price(5) == Decimal("10.00")
        assert tiers.unit_price(10) == Decimal("9.00")

    def test_line_total_is_exact_decimal(self, main: ModuleType) -> None:
        tiers = main.PriceTiers([(1, Decimal("0.10"))])
        total = tiers.line_total(3)
        assert total == Decimal("0.30"), "Decimal arithmetic must be exact here"
        assert isinstance(total, Decimal)

    def test_price_tiers_reject_float_prices(self, main: ModuleType) -> None:
        with pytest.raises(TypeError):
            main.PriceTiers([(1, 9.99)])  # type: ignore[list-item]

    @pytest.mark.parametrize(
        "tiers",
        [
            [],
            [(2, Decimal("1.00"))],
            [(1, Decimal("1.00")), (1, Decimal("0.90"))],
            [(1, Decimal("0"))],
        ],
        ids=["empty", "not-starting-at-1", "duplicate-min", "zero-price"],
    )
    def test_price_tiers_reject_bad_tables(self, main: ModuleType, tiers: list[tuple[int, Decimal]]) -> None:
        with pytest.raises(ValueError):
            main.PriceTiers(tiers)

    def test_unit_price_rejects_non_positive_quantity(self, main: ModuleType) -> None:
        tiers = main.PriceTiers([(1, Decimal("1.00"))])
        with pytest.raises(ValueError):
            tiers.unit_price(0)

    def test_sla_tier_boundaries_inclusive_upper(self, main: ModuleType) -> None:
        cases = {
            0: "critical",
            15: "critical",
            15.01: "high",
            60: "high",
            60.5: "normal",
            240: "normal",
            240.1: "low",
            float("inf"): "low",
        }
        for minutes, expected in cases.items():
            assert main.sla_tier(minutes) == expected, f"sla_tier({minutes}) should be {expected!r}"

    @pytest.mark.parametrize("minutes", [-1, float("nan")], ids=["negative", "nan"])
    def test_sla_tier_rejects_invalid_input(self, main: ModuleType, minutes: float) -> None:
        with pytest.raises(ValueError):
            main.sla_tier(minutes)


# ---------------------------------------------------------------------------
# Part 6: Records, grouping, hashability
# ---------------------------------------------------------------------------


class TestPart6RecordsGroupingHashability:
    def test_shipment_is_immutable_and_hashable(self, main: ModuleType) -> None:
        shipment = main.Shipment("A", 2, "NYC")
        with pytest.raises(AttributeError):
            shipment.qty = 3  # type: ignore[misc]
        assert {shipment, main.Shipment("A", 2, "NYC")} == {shipment}, "equal records hash equal"

    def test_with_qty_returns_new_record(self, main: ModuleType) -> None:
        shipment = main.Shipment("A", 2, "NYC")
        updated = shipment.with_qty(5)
        assert updated.qty == 5 and shipment.qty == 2, "original must be untouched"
        assert updated == ("A", 5, "NYC"), "a Shipment compares equal to a plain tuple"

    def test_shipment_unpacks_like_tuple(self, main: ModuleType) -> None:
        sku, qty, dest = main.Shipment("B", 7, "SFO")
        assert (sku, qty, dest) == ("B", 7, "SFO")

    def test_group_by_preserves_first_seen_and_input_order(self, main: ModuleType) -> None:
        S = main.Shipment
        items = [S("A", 1, "SFO"), S("B", 2, "NYC"), S("C", 3, "SFO")]
        grouped = main.group_by(items, key=lambda s: s.dest)
        assert list(grouped) == ["SFO", "NYC"], "keys follow first-seen order"
        assert grouped["SFO"] == [S("A", 1, "SFO"), S("C", 3, "SFO")], "items keep input order"

    def test_group_by_returns_plain_dict(self, main: ModuleType) -> None:
        grouped = main.group_by([1, 2, 3], key=lambda n: n % 2)
        assert type(grouped) is dict, "must not leak a defaultdict"
        with pytest.raises(KeyError):
            grouped[99]

    def test_group_by_consumes_iterable_once(self, main: ModuleType) -> None:
        grouped = main.group_by(iter(range(6)), key=lambda n: n % 3)
        assert grouped == {0: [0, 3], 1: [1, 4], 2: [2, 5]}

    def test_freeze_is_hashable_and_order_independent(self, main: ModuleType) -> None:
        first = {"a": [1, {"b": 2}], "c": {3, 4}}
        second = {"c": {4, 3}, "a": [1, {"b": 2}]}
        frozen = main.freeze(first)
        hash(frozen)  # must not raise
        assert main.freeze(second) == frozen, "mapping and set order must not matter"

    def test_freeze_distinguishes_list_from_tuple_and_list_order(self, main: ModuleType) -> None:
        assert main.freeze([1, 2]) != main.freeze((1, 2)), "list and tuple with same contents differ"
        assert main.freeze([1, 2]) != main.freeze([2, 1]), "list order matters"

    def test_freeze_mapping_does_not_collide_with_set_of_pairs(self, main: ModuleType) -> None:
        assert main.freeze({"a": 1}) != main.freeze({("a", 1)})

    def test_freeze_rejects_unhashable_leaves(self, main: ModuleType) -> None:
        with pytest.raises(TypeError):
            main.freeze({"payload": bytearray(b"x")})

    def test_freeze_works_as_cache_key(self, main: ModuleType) -> None:
        cache: dict[object, str] = {}
        cache[main.freeze({"q": "shoes", "filters": {"size": [9, 10]}})] = "hit"
        assert cache[main.freeze({"filters": {"size": [9, 10]}, "q": "shoes"})] == "hit"

    def test_auto_vivify_creates_on_getitem(self, main: ModuleType) -> None:
        d = main.AutoVivifyDict(list)
        d["a"].append(1)
        assert d["a"] == [1]
        assert "a" in d

    def test_get_contains_and_len_do_not_create(self, main: ModuleType) -> None:
        d = main.AutoVivifyDict(list)
        assert d.get("b") is None
        assert "b" not in d
        assert len(d) == 0

    def test_each_missing_key_gets_fresh_value(self, main: ModuleType) -> None:
        d = main.AutoVivifyDict(dict)
        assert d["x"] is not d["y"], "factory must run per key"

    def test_copy_keeps_factory_and_is_independent(self, main: ModuleType) -> None:
        d = main.AutoVivifyDict(list)
        d["seen"] = [0]
        clone = copy.copy(d)
        assert clone["fresh"] == [], "copy must keep the factory"
        assert "fresh" not in d, "copy must be independent of the original"

    def test_factory_must_be_callable(self, main: ModuleType) -> None:
        with pytest.raises(TypeError):
            main.AutoVivifyDict(3)  # type: ignore[arg-type]
