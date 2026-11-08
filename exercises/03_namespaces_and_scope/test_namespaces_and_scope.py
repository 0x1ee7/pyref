"""Tests for 03 · Namespaces and scope."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


class TestPart1LegbClosuresNonlocal:
    def test_resolve_scope_follows_legb_order(self, main: ModuleType) -> None:
        scopes = {
            "local_scope": {"x": 1},
            "enclosing": {"x": 2, "y": 2},
            "global_scope": {"x": 3, "y": 3, "z": 3},
            "builtin_scope": {"x": 4, "y": 4, "z": 4, "len": len},
        }
        assert main.resolve_scope("x", **scopes) == "local", "local must win over every outer scope"
        assert main.resolve_scope("y", **scopes) == "enclosing", "enclosing must win over global/builtin"
        assert main.resolve_scope("z", **scopes) == "global", "global must win over builtin"
        assert main.resolve_scope("len", **scopes) == "builtin", "builtins are the last resort"
        with pytest.raises(NameError, match="missing"):
            main.resolve_scope("missing", **{k: {} for k in scopes})

    def test_counter_steps_from_start_with_defaults_and_custom_values(self, main: ModuleType) -> None:
        counter = main.make_counter(start=10, step=5)
        assert [counter() for _ in range(3)] == [10, 15, 20], "counter should yield start, then step by step"
        defaults = main.make_counter()
        assert [defaults(), defaults(), defaults()] == [0, 1, 2], "defaults are start=0, step=1"
        with pytest.raises(ValueError):
            main.make_counter(step=0)

    def test_counters_keep_independent_state(self, main: ModuleType) -> None:
        a = main.make_counter()
        b = main.make_counter(start=100)
        assert (a(), a(), b(), a()) == (0, 1, 100, 2), "each factory call must own its nonlocal state"

    @settings(
        derandomize=True,
        max_examples=50,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(start=st.integers(-1000, 1000), step=st.integers(-50, 50).filter(lambda s: s != 0))
    def test_counter_is_arithmetic_progression(self, main: ModuleType, start: int, step: int) -> None:
        counter = main.make_counter(start=start, step=step)
        values = [counter() for _ in range(10)]
        assert values == [start + i * step for i in range(10)], "values must form an arithmetic progression"

    def test_id_generator_pads_and_counts_from_one(self, main: ModuleType) -> None:
        next_id = main.make_id_generator("ORD", width=4)
        assert next_id() == "ORD-0001"
        assert next_id() == "ORD-0002"

    def test_id_generator_does_not_truncate_wide_numbers(self, main: ModuleType) -> None:
        next_id = main.make_id_generator("X", width=2)
        ids = [next_id() for _ in range(100)]
        assert ids[0] == "X-01", "numbers narrower than width are zero-padded"
        assert ids[-1] == "X-100", "numbers wider than width must not be truncated"

    def test_id_generator_validates_arguments(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.make_id_generator("")
        with pytest.raises(ValueError):
            main.make_id_generator("A", width=0)


class TestPart2LateBinding:
    def test_late_bound_callbacks_all_see_last_label(self, main: ModuleType) -> None:
        callbacks = main.late_bound_callbacks(["a", "b", "c"])
        results = [cb() for cb in callbacks]
        assert results == ["clicked c"] * 3, (
            "closures in a loop share the loop variable; every callback must report the last label"
        )
        assert main.late_bound_callbacks([]) == [], "empty input yields no callbacks"

    def test_make_handlers_binds_each_label(self, main: ModuleType) -> None:
        handlers = main.make_handlers(["save", "open"])
        assert set(handlers) == {"save", "open"}
        assert handlers["save"]() == "clicked save", "each handler must report its own label"
        assert handlers["open"]() == "clicked open", "each handler must report its own label"

    def test_make_handlers_accepts_any_iterable_and_rejects_duplicates(self, main: ModuleType) -> None:
        handlers = main.make_handlers(label for label in ["only"])
        assert handlers["only"]() == "clicked only"
        with pytest.raises(ValueError):
            main.make_handlers(["dup", "dup"])

    def test_make_adders_capture_their_own_offset(self, main: ModuleType) -> None:
        adders = main.make_adders([1, 10, 100])
        assert [add(5) for add in adders] == [6, 15, 105], "each adder must keep the offset it was created with"


class TestPart3ModuleRegistry:
    def test_register_returns_function_unchanged(self, main: ModuleType) -> None:
        def greet(name: str) -> str:
            return f"hi {name}"

        decorated = main.register("greet")(greet)
        assert decorated is greet, "the decorator must return the same function object"
        assert main.get_plugin("greet") is greet, "the registry must hold that function"

    def test_register_rejects_duplicates_and_keeps_original(self, main: ModuleType) -> None:
        @main.register("hook")
        def first() -> str:
            return "first"

        with pytest.raises(ValueError):
            main.register("hook")(lambda: "second")
        assert main.get_plugin("hook") is first, "a rejected duplicate must not replace the original"

    def test_register_rejects_non_identifiers(self, main: ModuleType) -> None:
        for bad_name in ["", "bad name", "1abc", "dash-name"]:
            with pytest.raises(ValueError):
                main.register(bad_name)

    def test_unregister_removes_plugin(self, main: ModuleType) -> None:
        main.register("temp")(lambda: None)
        main.unregister("temp")
        assert "temp" not in main.plugin_names()
        with pytest.raises(KeyError):
            main.get_plugin("temp")
        with pytest.raises(KeyError):
            main.unregister("temp")

    def test_plugin_names_keep_registration_order_and_run_passes_args(self, main: ModuleType) -> None:
        main.register("zeta")(lambda x, *, offset=0: x * 2 + offset)
        main.register("alpha")(lambda x, *, offset=0: x * x + offset)
        assert main.plugin_names() == ["zeta", "alpha"], "order is registration order, not sorted"
        results = main.run_plugins(3, offset=1)
        assert list(results) == ["zeta", "alpha"], "results must be keyed in registration order"
        assert results == {"zeta": 7, "alpha": 10}

    def test_total_registrations_counts_successes_only(self, main: ModuleType) -> None:
        main.register("a")(lambda: None)
        main.register("b")(lambda: None)
        with pytest.raises(ValueError):
            main.register("a")(lambda: None)
        main.unregister("a")
        assert main.total_registrations() == 2, "unregister must not reduce the total; failures must not count"

    def test_clear_plugins_empties_registry_but_not_total(self, main: ModuleType) -> None:
        main.register("only")(lambda: None)
        main.clear_plugins()
        assert main.plugin_names() == []
        main.register("again")(lambda: None)
        assert main.plugin_names() == ["again"], "registry must accept new plugins after clear"
        assert main.total_registrations() == 2, "total counts every successful registration since import"


class TestPart4ClassBodyAndMangling:
    def test_scaler_table_is_computed_in_class_body_without_leaking_names(self, main: ModuleType) -> None:
        assert main.Scaler.TABLE == (0, 3, 6, 9, 12), "TABLE must be built from FACTOR and TABLE_SIZE"
        assert main.Scaler().scale(4) == 12
        for leaked in ("factor", "size", "i"):
            assert leaked not in vars(main.Scaler), f"{leaked!r} must not remain as a class attribute"

    def test_subclass_override_does_not_change_precomputed_table(self, main: ModuleType) -> None:
        class Double(main.Scaler):
            FACTOR = 2

        assert Double().scale(4) == 8, "method reads FACTOR at call time through self"
        assert Double.TABLE == (0, 3, 6, 9, 12), "TABLE was fixed when the base class was created"

    def test_mangle_name_rules(self, main: ModuleType) -> None:
        cases = {
            ("Ledger", "__balance"): "_Ledger__balance",
            ("_Private", "__x"): "_Private__x",
            ("__", "__x"): "__x",
            ("Ledger", "__init__"): "__init__",
            ("Ledger", "_single"): "_single",
            ("Ledger", "___x"): "_Ledger___x",
        }
        for (class_name, attribute), expected in cases.items():
            assert main.mangle_name(class_name, attribute) == expected, (
                f"mangle_name({class_name!r}, {attribute!r}) should be {expected!r}"
            )

    def test_mangle_name_matches_interpreter(self, main: ModuleType) -> None:
        class _Probe:
            def __init__(self) -> None:
                self.__secret = 1

        assert main.mangle_name("_Probe", "__secret") in vars(_Probe()), (
            "mangle_name must predict the attribute name the interpreter actually uses"
        )

    def test_ledger_stores_balance_under_mangled_name_and_survives_subclass_writes(
        self, main: ModuleType
    ) -> None:
        class Hijacker(main.Ledger):
            def hijack(self) -> None:
                self.__balance = 10**9  # becomes _Hijacker__balance

        ledger = Hijacker(50)
        assert "_Ledger__balance" in vars(ledger), "balance must be stored under the mangled name"
        assert "__balance" not in vars(ledger)
        ledger.hijack()
        assert ledger.balance == 50, "name mangling must keep the subclass write from touching the balance"

    def test_ledger_deposit_withdraw_and_errors(self, main: ModuleType) -> None:
        ledger = main.Ledger()
        assert ledger.deposit(30) == 30
        assert ledger.withdraw(10) == 20
        with pytest.raises(main.InsufficientFunds):
            ledger.withdraw(21)
        assert ledger.balance == 20, "a rejected withdrawal must leave the balance unchanged"
        with pytest.raises(ValueError):
            ledger.deposit(0)
        with pytest.raises(ValueError):
            main.Ledger(-1)


class TestPart5ModuleNamespaces:
    def test_scoped_override_replaces_then_restores_even_on_error(self, main: ModuleType) -> None:
        fake = ModuleType("fake")
        fake.LIMIT = 5  # type: ignore[attr-defined]
        with main.scoped_override(fake, "LIMIT", 99):
            assert fake.LIMIT == 99, "value must be visible inside the block"  # type: ignore[attr-defined]
        assert fake.LIMIT == 5, "previous value must be restored on exit"  # type: ignore[attr-defined]
        with pytest.raises(RuntimeError):
            with main.scoped_override(fake, "LIMIT", 99):
                raise RuntimeError("boom")
        assert fake.LIMIT == 5, "restoration must happen even when the block raises"  # type: ignore[attr-defined]

    def test_scoped_override_removes_name_that_did_not_exist(self, main: ModuleType) -> None:
        fake = ModuleType("fake")
        with main.scoped_override(fake, "NEW", 1):
            assert fake.NEW == 1  # type: ignore[attr-defined]
        assert "NEW" not in vars(fake), "a name that did not exist before must be deleted again"

    def test_scoped_override_nests_and_survives_block_deleting_the_name(self, main: ModuleType) -> None:
        fake = ModuleType("fake")
        fake.MODE = "base"  # type: ignore[attr-defined]
        with main.scoped_override(fake, "MODE", "outer"):
            with main.scoped_override(fake, "MODE", "inner"):
                assert fake.MODE == "inner"  # type: ignore[attr-defined]
            assert fake.MODE == "outer", "inner exit must restore the outer override"  # type: ignore[attr-defined]
            del fake.MODE  # type: ignore[attr-defined]
        assert fake.MODE == "base", "exit must restore the original value even if the block deleted it"  # type: ignore[attr-defined]

    def test_public_names_filters_private_and_modules(self, main: ModuleType) -> None:
        namespace = {"b": 1, "_hidden": 2, "A": 3, "os_module": sys}
        assert main.public_names(namespace) == ["A", "b"], "sorted, no underscore names, no modules"


class TestPart6ConfigLoader:
    def test_returns_only_uppercase_public_names(self, main: ModuleType, tmp_path: Path) -> None:
        config = tmp_path / "settings.py"
        config.write_text(
            "MAX_RETRIES = 3\n"
            "name = 'lower'\n"
            "_SECRET = 1\n"
            "HOST = 'localhost'\n"
            "DEBUG = False\n"
            "import os\n"
            "derived = MAX_RETRIES * 2\n",
            encoding="utf-8",
        )
        assert main.load_config(config) == {"MAX_RETRIES": 3, "HOST": "localhost", "DEBUG": False}
        assert main.load_config(str(config)) == main.load_config(config), "str and Path must both work"

    def test_config_runs_in_fresh_namespace_and_cannot_see_loader(self, main: ModuleType, tmp_path: Path) -> None:
        first = tmp_path / "first.py"
        first.write_text("SHARED = 1\n", encoding="utf-8")
        second = tmp_path / "second.py"
        second.write_text("COPY = SHARED\n", encoding="utf-8")
        peek = tmp_path / "peek.py"
        peek.write_text("LOADER = load_config\n", encoding="utf-8")
        main.load_config(first)
        with pytest.raises(main.ConfigError):
            main.load_config(second)
        with pytest.raises(main.ConfigError) as excinfo:
            main.load_config(peek)
        assert isinstance(excinfo.value.__cause__, NameError), "the original NameError must be chained"

    def test_compile_and_runtime_errors_are_wrapped_with_path(self, main: ModuleType, tmp_path: Path) -> None:
        bad = tmp_path / "bad.py"
        bad.write_text("X = (\n", encoding="utf-8")
        with pytest.raises(main.ConfigError, match="bad.py") as excinfo:
            main.load_config(bad)
        assert isinstance(excinfo.value.__cause__, SyntaxError)

        boom = tmp_path / "boom.py"
        boom.write_text("X = 1 / 0\n", encoding="utf-8")
        with pytest.raises(main.ConfigError) as excinfo:
            main.load_config(boom)
        assert isinstance(excinfo.value.__cause__, ZeroDivisionError)

    def test_missing_file_propagates_file_not_found(self, main: ModuleType, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            main.load_config(tmp_path / "nope.py")


class TestPart7RestrictedEvaluation:
    def test_arithmetic_over_cells_returns_float(self, main: ModuleType) -> None:
        result = main.evaluate("A1 + B2 * 2", {"A1": 1, "B2": 3})
        assert result == 7.0
        assert isinstance(result, float), "evaluate must always return a float"
        assert main.evaluate("(A1 + 1) * -2", {"A1": 3}) == -8.0, "unary minus and parentheses"
        assert main.evaluate("7 // 2 + 7 % 4", {}) == 6.0, "floor division and modulo"

    def test_default_and_custom_functions(self, main: ModuleType) -> None:
        assert main.evaluate("ABS(-2.5) + MAX(A1, 2, 3) + ROUND(2.567, 2)", {"A1": 1}) == 2.5 + 3 + 2.57
        result = main.evaluate("DOUBLE(A1) + 1", {"A1": 4}, {"DOUBLE": lambda x: 2 * x})
        assert result == 9.0, "custom functions are merged with the defaults"

    def test_unknown_names_raise(self, main: ModuleType) -> None:
        with pytest.raises(main.UnknownNameError, match="Z9"):
            main.evaluate("A1 + Z9", {"A1": 1})
        with pytest.raises(main.UnknownNameError):
            main.evaluate("len(A1)", {"A1": 1})
        with pytest.raises(main.FormulaError):
            main.evaluate("__import__('os')", {})

    def test_attribute_access_dunders_and_unsupported_grammar_are_forbidden(self, main: ModuleType) -> None:
        with pytest.raises(main.ForbiddenSyntaxError):
            main.evaluate("().__class__", {})
        with pytest.raises(main.ForbiddenSyntaxError):
            main.evaluate("A1.real", {"A1": 1})
        for formula in ["A1 ** 2", "A1 if A1 else 0", "True", "'text'", "A1 < 2"]:
            with pytest.raises(main.ForbiddenSyntaxError):
                main.evaluate(formula, {"A1": 1})

    def test_cell_names_shadow_builtins_and_do_not_leak_between_calls(self, main: ModuleType) -> None:
        assert main.evaluate("len + 1", {"len": 2}) == 3.0, "a cell named like a builtin is just a cell"
        assert main.evaluate("A1", {"A1": 1}) == 1.0
        with pytest.raises(main.UnknownNameError):
            main.evaluate("A1", {})

    def test_runtime_failures_and_cell_function_overlap(self, main: ModuleType) -> None:
        with pytest.raises(main.FormulaError) as excinfo:
            main.evaluate("A1 / 0", {"A1": 1})
        assert isinstance(excinfo.value.__cause__, ZeroDivisionError)
        with pytest.raises(main.FormulaError):
            main.evaluate("MAX()", {})
        with pytest.raises(main.FormulaError):
            main.evaluate("1 +", {})
        with pytest.raises(ValueError):
            main.evaluate("MAX(1)", {"MAX": 5})
