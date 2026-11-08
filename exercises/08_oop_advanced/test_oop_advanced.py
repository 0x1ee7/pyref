"""Tests for the advanced OOP exercises (see README.md for the full spec)."""

from __future__ import annotations

import copy
import json
import pickle
from types import GenericAlias, ModuleType

import pytest


class _Client:
    """Stand-in for an expensive client."""

    def fetch(self, key: str) -> str:
        return f"value:{key}"


class _Sized:
    def __len__(self) -> int:
        return 3

    def ping(self) -> str:
        return "pong"


class TestPart1Descriptors:
    def test_field_records_name_and_class_access_returns_descriptor(self, main: ModuleType) -> None:
        class Account:
            label = main.StringField(max_length=8)

        descriptor = Account.__dict__["label"]
        assert descriptor.name == "label", "__set_name__ should record the attribute name"
        assert Account.label is descriptor, "class-level access must return the descriptor itself"

    def test_set_and_get_round_trip(self, main: ModuleType) -> None:
        class Account:
            label = main.StringField(max_length=8)

        acct = Account()
        acct.label = "savings"
        assert acct.label == "savings", "stored value should be returned on instance access"

    def test_string_field_rejects_non_str(self, main: ModuleType) -> None:
        class Account:
            label = main.StringField()

        with pytest.raises(main.ValidationError, match="must be a str") as exc:
            Account().label = 42
        assert exc.value.field == "label", "ValidationError.field should name the attribute"

    def test_string_max_length_is_inclusive(self, main: ModuleType) -> None:
        class Account:
            label = main.StringField(max_length=8)

        acct = Account()
        acct.label = "x" * 8
        assert acct.label == "x" * 8, "a value of exactly max_length must be accepted"
        with pytest.raises(main.ValidationError, match="at most 8 characters"):
            acct.label = "x" * 9

    def test_integer_field_rejects_bool(self, main: ModuleType) -> None:
        class Account:
            age = main.IntegerField()

        acct = Account()
        acct.age = 3
        assert acct.age == 3, "plain ints should be accepted"
        with pytest.raises(main.ValidationError, match="must be an int"):
            acct.age = True

    def test_integer_bounds_are_inclusive(self, main: ModuleType) -> None:
        class Account:
            age = main.IntegerField(min_value=0, max_value=120)

        acct = Account()
        acct.age = 0
        acct.age = 120
        assert acct.age == 120, "both bounds should be accepted"
        with pytest.raises(main.ValidationError, match="must be >= 0"):
            acct.age = -1
        with pytest.raises(main.ValidationError, match="must be <= 120"):
            acct.age = 121

    def test_required_rejects_none_and_optional_allows_it(self, main: ModuleType) -> None:
        class Record:
            key = main.StringField(required=True)
            note = main.StringField()

        rec = Record()
        rec.note = None
        assert rec.note is None, "optional fields may be set to None"
        with pytest.raises(main.ValidationError, match="is required"):
            rec.key = None

    def test_default_is_returned_until_set_and_restored_by_delete(self, main: ModuleType) -> None:
        class Counter:
            hits = main.IntegerField(default=7)

        c = Counter()
        assert c.hits == 7, "unset field should expose its default"
        c.hits = 9
        assert c.hits == 9, "assigned value should win over the default"
        del c.hits
        assert c.hits == 7, "__delete__ should fall back to the default"

    def test_data_descriptor_takes_precedence_over_instance_dict(self, main: ModuleType) -> None:
        class Account:
            label = main.StringField()

        acct = Account()
        acct.label = "ok"
        acct.__dict__["label"] = "evil"
        assert acct.label == "ok", "a data descriptor must win over a same-named instance dict entry"

    def test_invalid_constructor_arguments_raise_value_error(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.StringField(max_length=0)
        with pytest.raises(ValueError):
            main.IntegerField(min_value=5, max_value=1)


class TestPart2ModelRegistry:
    def test_subclasses_register_under_class_name(self, main: ModuleType) -> None:
        class Customer(main.Model):
            id = main.IntegerField(required=True)

        assert main.get_model("Customer") is Customer, "get_model should return the registered class"
        assert main.Model.registry["Customer"] is Customer, "registry should be keyed by class name"

    def test_abstract_classes_are_not_registered(self, main: ModuleType) -> None:
        class Base(main.Model, abstract=True):
            pass

        assert "Base" not in main.Model.registry, "abstract=True must skip registration"
        with pytest.raises(KeyError, match="Base"):
            main.get_model("Base")

    def test_duplicate_model_name_is_rejected(self, main: ModuleType) -> None:
        class Dup(main.Model):
            pass

        with pytest.raises(TypeError, match="already registered"):

            class Dup(main.Model):  # noqa: F811 - the second definition is the point
                pass

    def test_fields_inherit_in_declaration_order_and_overrides_keep_position(self, main: ModuleType) -> None:
        class Person(main.Model):
            name = main.StringField(max_length=20, required=True)
            age = main.IntegerField(min_value=0, default=0)

        class Employee(Person):
            badge = main.StringField(max_length=6, required=True)
            name = main.StringField(max_length=40, required=True)

        assert list(Person.fields()) == ["name", "age"], "parent field order should be preserved"
        assert list(Employee.fields()) == ["name", "age", "badge"], (
            "inherited fields come first; an override keeps the inherited position"
        )
        assert Employee.fields()["name"].max_length == 40, "override should replace the inherited field"
        assert Person.fields()["name"].max_length == 20, "parent must not be affected by the override"

    def test_init_sets_defaults_and_validates(self, main: ModuleType) -> None:
        class Person(main.Model):
            name = main.StringField(max_length=20, required=True)
            age = main.IntegerField(min_value=0, default=0)

        person = Person(name="ann")
        assert person.age == 0, "missing optional kwargs should use the field default"
        with pytest.raises(main.ValidationError, match="at most 20") as exc:
            Person(name="x" * 21)
        assert exc.value.field == "name", "validation errors should name the offending field"
        with pytest.raises(main.ValidationError, match="is required"):
            Person()

    def test_unknown_kwargs_and_positional_args_are_rejected(self, main: ModuleType) -> None:
        class Person(main.Model):
            name = main.StringField()

        with pytest.raises(TypeError, match="nope"):
            Person(name="a", nope=1)
        with pytest.raises(TypeError):
            Person("a")  # type: ignore[misc]

    def test_repr_and_to_dict(self, main: ModuleType) -> None:
        class Person(main.Model):
            name = main.StringField()
            age = main.IntegerField(default=0)

        person = Person(name="ann", age=3)
        assert repr(person) == "Person(name='ann', age=3)", "repr should list fields in declaration order"
        assert person.to_dict() == {"name": "ann", "age": 3}, "to_dict should map field names to values"

    def test_table_decorator_and_default_table_name(self, main: ModuleType) -> None:
        @main.table("people")
        class Human(main.Model):
            pass

        class Widget(main.Model):
            pass

        assert Human.__table__ == "people", "@table should set __table__ and return the class"
        assert Widget.__table__ == "widget", "default __table__ is the lowercased class name"
        with pytest.raises(ValueError):
            main.table("not valid")


class TestPart3LazyProxy:
    def test_factory_is_not_called_until_first_access(self, main: ModuleType) -> None:
        calls: list[int] = []

        def factory() -> _Client:
            calls.append(1)
            return _Client()

        proxy = main.LazyProxy(factory)
        assert calls == [], "constructing the proxy must not call the factory"
        assert proxy.init_count == 0 and not proxy.loaded, "nothing should be loaded yet"
        assert proxy.fetch("a") == "value:a", "attribute access should forward to the target"
        assert len(calls) == 1, "first access should trigger exactly one factory call"
        assert proxy.init_count == 1, "init_count should record the successful factory call"

    def test_target_is_loaded_once_across_accesses(self, main: ModuleType) -> None:
        proxy = main.LazyProxy(_Client)
        for key in ("a", "b", "c"):
            proxy.fetch(key)
        assert proxy.init_count == 1, "later accesses must reuse the loaded target"

    def test_reset_forces_reinitialisation(self, main: ModuleType) -> None:
        proxy = main.LazyProxy(_Client)
        proxy.fetch("a")
        proxy.reset()
        assert not proxy.loaded, "reset() should drop the target"
        proxy.fetch("b")
        assert proxy.init_count == 2, "the next access after reset() should call the factory again"

    def test_failed_factory_is_not_counted_and_is_retried(self, main: ModuleType) -> None:
        attempts: list[int] = []

        def flaky() -> _Client:
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("boom")
            return _Client()

        proxy = main.LazyProxy(flaky)
        with pytest.raises(RuntimeError, match="boom"):
            proxy.fetch("a")
        assert proxy.init_count == 0, "a failed factory call must not count as an initialisation"
        assert not proxy.loaded, "a failed factory call must leave the proxy unloaded"
        assert proxy.fetch("a") == "value:a", "the next access should retry the factory"
        assert proxy.init_count == 1

    def test_repr_does_not_load_the_target(self, main: ModuleType) -> None:
        calls: list[int] = []

        def factory() -> _Client:
            calls.append(1)
            return _Client()

        proxy = main.LazyProxy(factory)
        assert repr(proxy) == "LazyProxy(<not loaded>)", "unloaded repr should not touch the target"
        assert calls == [], "repr must not call the factory"
        proxy.fetch("a")
        assert "_Client" in repr(proxy), "loaded repr should show the target"

    def test_dunder_methods_bypass_forwarding(self, main: ModuleType) -> None:
        calls: list[int] = []

        def factory() -> _Sized:
            calls.append(1)
            return _Sized()

        proxy = main.LazyProxy(factory)
        with pytest.raises(TypeError):
            len(proxy)
        with pytest.raises(AttributeError):
            proxy.__len__()
        assert calls == [], "dunder lookups must not forward, so the target is never loaded"
        assert proxy.ping() == "pong", "ordinary names still forward after the failed dunder probes"

    def test_dir_merges_target_attributes(self, main: ModuleType) -> None:
        proxy = main.LazyProxy(_Client)
        names = dir(proxy)
        assert "fetch" in names, "dir() should include the target's attributes"
        assert "init_count" in names, "dir() should include the proxy's own attributes"
        assert names == sorted(names), "dir() should be sorted"


class TestPart4Mixins:
    def test_mro_is_the_cooperative_chain(self, main: ModuleType) -> None:
        names = [cls.__name__ for cls in main.Order.__mro__]
        assert names == ["Order", "AuditMixin", "JsonMixin", "ComparableMixin", "Entity", "object"], (
            f"unexpected MRO: {names}"
        )

    def test_constructor_chain_sets_all_attributes(self, main: ModuleType) -> None:
        order = main.Order(id=7, actor="ann")
        assert order.id == 7, "Entity should receive id through the chain"
        assert order.actor == "ann", "AuditMixin should consume actor"
        assert order.audit_log == [], "audit_log should start empty"

    def test_missing_and_unknown_kwargs_raise(self, main: ModuleType) -> None:
        with pytest.raises(TypeError, match="actor"):
            main.Order(id=7)
        with pytest.raises(TypeError, match="colour"):
            main.Order(id=7, actor="ann", colour="red")

    def test_to_dict_is_merged_cooperatively(self, main: ModuleType) -> None:
        order = main.Order(id=7, actor="ann")
        assert order.to_dict() == {"id": 7, "actor": "ann"}, "each mixin should contribute via super()"

    def test_to_json_uses_sorted_keys(self, main: ModuleType) -> None:
        order = main.Order(id=7, actor="ann")
        text = order.to_json()
        assert text == '{"actor": "ann", "id": 7}', "to_json should sort keys by default"
        assert json.loads(text) == {"id": 7, "actor": "ann"}, "output should be valid JSON"

    def test_equality_uses_sort_key_and_exact_type(self, main: ModuleType) -> None:
        class Refund(main.Order):
            pass

        assert main.Order(id=7, actor="ann") == main.Order(id=7, actor="bob"), (
            "equality is defined by _sort_key, not by the audit actor"
        )
        assert main.Order(id=7, actor="a") != Refund(id=7, actor="a"), (
            "different concrete types must never compare equal"
        )

    def test_ordering_hash_and_foreign_types(self, main: ModuleType) -> None:
        orders = [main.Order(id=3, actor="a"), main.Order(id=1, actor="a"), main.Order(id=2, actor="a")]
        assert [o.id for o in sorted(orders)] == [1, 2, 3], "ordering should follow _sort_key"
        assert len({main.Order(id=1, actor="a"), main.Order(id=1, actor="b")}) == 1, (
            "equal objects must hash equally"
        )
        assert main.Order(id=1, actor="a") != 7, "comparing with a foreign type is False, not an error"
        with pytest.raises(TypeError):
            main.Order(id=1, actor="a") < 5  # type: ignore[operator]

    def test_record_appends_audit_entries(self, main: ModuleType) -> None:
        order = main.Order(id=7, actor="ann")
        order.record("created")
        order.record("shipped")
        assert order.audit_log == ["ann:created", "ann:shipped"], "record() should append actor:action entries"


class TestPart5Slots:
    def test_slots_declared_and_no_instance_dict(self, main: ModuleType) -> None:
        assert main.Event.__slots__ == ("name", "ts", "payload"), "__slots__ should list the three attributes"
        event = main.Event("login", ts=1)
        assert not hasattr(event, "__dict__"), "slotted instances must not carry a __dict__"

    def test_unknown_attribute_assignment_raises(self, main: ModuleType) -> None:
        event = main.Event("login", ts=1)
        with pytest.raises(AttributeError):
            event.extra = 1  # type: ignore[attr-defined]

    def test_subclass_without_slots_regains_dict(self, main: ModuleType) -> None:
        class Debug(main.Event):
            pass

        debug = Debug("x", ts=1)
        debug.extra = 2  # type: ignore[attr-defined]
        assert debug.extra == 2, "a subclass that omits __slots__ gets a __dict__ back"  # type: ignore[attr-defined]
        assert hasattr(debug, "__dict__"), "the subclass instance should expose __dict__"

    def test_payload_is_copied_and_defaults_are_independent(self, main: ModuleType) -> None:
        source = {"a": 1}
        event = main.Event("x", ts=1, payload=source)
        source["a"] = 2
        assert event.payload == {"a": 1}, "the constructor must copy the payload"
        assert main.Event("y", ts=1).payload is not main.Event("z", ts=1).payload, (
            "each default payload must be a fresh dict"
        )

    def test_copy_is_shallow_and_shares_payload(self, main: ModuleType) -> None:
        event = main.Event("x", ts=1, payload={"tags": [1]})
        clone = copy.copy(event)
        assert clone is not event, "copy.copy must return a new object"
        assert type(clone) is main.Event, "copy must keep the concrete type"
        assert clone.name == "x" and clone.ts == 1, "scalar fields should be copied"
        assert clone.payload is event.payload, "__copy__ must share the payload object"

    def test_deepcopy_isolates_nested_payload(self, main: ModuleType) -> None:
        event = main.Event("x", ts=1, payload={"tags": ["a"]})
        clone = copy.deepcopy(event)
        clone.payload["tags"].append("b")
        assert event.payload == {"tags": ["a"]}, "__deepcopy__ must not share nested state"
        assert clone.payload is not event.payload, "payload must be a distinct object after deepcopy"

    def test_pickle_round_trip_preserves_slotted_state(self, main: ModuleType) -> None:
        event = main.Event("login", ts=5, payload={"user": "ann"})
        restored = pickle.loads(pickle.dumps(event))
        assert (restored.name, restored.ts, restored.payload) == ("login", 5, {"user": "ann"}), (
            "pickle should preserve every slot"
        )
        assert not hasattr(restored, "__dict__"), "restored instances must stay slot-only"

    def test_repr(self, main: ModuleType) -> None:
        event = main.Event("login", ts=5, payload={"user": "ann"})
        assert repr(event) == "Event(name='login', ts=5, payload={'user': 'ann'})", "repr should show all fields"


class TestPart6Plugins:
    def test_named_subclass_registers_under_keyword(self, main: ModuleType) -> None:
        class Upper(main.Plugin, name="upper"):
            def transform(self, text: str) -> str:
                return text.upper()

        plugin = main.Plugin.create("upper")
        assert isinstance(plugin, Upper), "create() should instantiate the registered class"
        assert Upper.plugin_name == "upper", "plugin_name should record the class keyword"
        assert main.Plugin.available() == ("upper",), "available() should list registered names"

    def test_unnamed_subclass_is_not_registered(self, main: ModuleType) -> None:
        class Helper(main.Plugin):
            pass

        assert Helper.plugin_name is None, "plugin_name must be None without the keyword"
        assert main.Plugin.available() == (), "unnamed classes must not register"

    def test_duplicate_name_is_rejected(self, main: ModuleType) -> None:
        class First(main.Plugin, name="dup"):
            pass

        with pytest.raises(ValueError, match="already registered"):

            class Second(main.Plugin, name="dup"):  # noqa: F811
                pass

    def test_invalid_names_are_rejected(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):

            class Blank(main.Plugin, name=""):
                pass

        with pytest.raises(ValueError):

            class Spaces(main.Plugin, name="   "):
                pass

        with pytest.raises(TypeError):

            class NotStr(main.Plugin, name=3):  # type: ignore[arg-type]
                pass

    def test_unknown_plugin_raises_key_error_with_name(self, main: ModuleType) -> None:
        with pytest.raises(KeyError, match="nope"):
            main.Plugin.create("nope")

    def test_config_reaches_instance_and_plugin_is_callable(self, main: ModuleType) -> None:
        class Prefix(main.Plugin, name="prefix"):
            def __init__(self, **config: str) -> None:
                super().__init__(**config)
                self.prefix = config.get("prefix", "")

            def transform(self, text: str) -> str:
                return f"{self.prefix}{text}"

        plugin = main.Plugin.create("prefix", prefix=">> ")
        assert plugin.config == {"prefix": ">> "}, "config should be stored on the instance"
        assert plugin("hi") == ">> hi", "calling the plugin should delegate to transform()"
        assert plugin.calls == 1, "__call__ should count invocations"

    def test_base_transform_is_abstract(self, main: ModuleType) -> None:
        with pytest.raises(NotImplementedError):
            main.Plugin()("x")

    def test_stage_protocol_is_structural_and_method_only(self, main: ModuleType) -> None:
        class Upper(main.Plugin, name="upper-stage"):
            def transform(self, text: str) -> str:
                return text.upper()

        assert isinstance(Upper(), main.Stage), "any object with __call__ satisfies Stage"
        assert isinstance(lambda text: text, main.Stage), (
            "runtime_checkable only checks member presence, not signatures"
        )
        assert not isinstance(42, main.Stage), "objects without __call__ must not satisfy Stage"
        assert issubclass(Upper, main.Stage), "issubclass works for method-only protocols"
        assert not issubclass(int, main.Stage), "int has no __call__"

    def test_class_getitem_returns_generic_alias(self, main: ModuleType) -> None:
        alias = main.Plugin[str]
        assert isinstance(alias, GenericAlias), "__class_getitem__ should return a types.GenericAlias"
        assert alias.__origin__ is main.Plugin, "origin should be the subscripted class"
        assert alias.__args__ == (str,), "arguments should be preserved"
