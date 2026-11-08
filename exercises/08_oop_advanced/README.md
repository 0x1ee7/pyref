# 08 · Advanced OOP and the Python object model

A small data-access and integration toolkit built the way a senior codebase
would build it: declarative models validated by descriptors, a registry filled
by `__init_subclass__`, a lazy proxy around an expensive client, cooperative
mixins with a checkable MRO, memory-lean `__slots__` event records, and a
plugin system where subclasses register themselves by a class keyword.

Implement everything in `main.py`. The tests in `test_oop_advanced.py` are the
full spec alongside this README. Every test fails until you implement the
API below.

## Part 1: Descriptor fields

Implement in `main.py`:

- `class ValidationError(ValueError)`
  - `__init__(self, field: str, reason: str)`
  - attributes `field`, `reason`; `str(err)` is `f"{field}: {reason}"`.
- `class Field` (base **data** descriptor)
  - `__init__(self, *, required: bool = False, default: Any = None)`
  - `__set_name__` records `name`; values are stored on the instance under the
    private key `"_" + name`, never under `name` itself. Because the descriptor
    defines `__set__`, it takes precedence over the instance `__dict__`.
  - `__get__(None, owner)` returns the descriptor itself. On an instance it returns
    the stored value, or `default` if nothing has been stored.
  - `__set__` runs `validate()` and stores the result.
  - `__delete__` removes the stored value, so reads fall back to `default`.
  - `validate(value)`: `None` raises `ValidationError(name, "is required")` when
    `required`, otherwise `None` is accepted as-is. Any other value goes to
    `clean(value)`, which subclasses override and the base returns unchanged.
- `class StringField(Field)`
  - `__init__(self, *, max_length: int | None = None, required=False, default=None)`
  - `max_length` must be `>= 1` when given, else `ValueError`.
  - non-`str` values: `ValidationError(name, "must be a str, got <typename>")`.
  - `len(value) > max_length`: `ValidationError(name, "must be at most N characters")`.
- `class IntegerField(Field)`
  - `__init__(self, *, min_value: int | None = None, max_value: int | None = None, required=False, default=None)`
  - `min_value > max_value` raises `ValueError`.
  - `bool` is rejected (it is an `int` subclass): `"must be an int, got bool"`.
  - below min: `"must be >= N"`; above max: `"must be <= N"`.

Concepts: `__set_name__`, data vs non-data descriptors, descriptor `__get__`
on class vs instance, `__delete__`, the `bool`-is-an-`int` gotcha.

## Part 2: Models with an `__init_subclass__` registry

Implement in `main.py`:

- `class Model`
  - `registry: ClassVar[dict[str, type[Model]]]`: non-abstract subclasses,
    keyed by class `__name__`.
  - `__init_subclass__(cls, *, abstract: bool = False, **kwargs)`:
    - collects `Field` attributes into `cls._fields`, inheriting the parent's
      fields first. Declaration order is kept, and an override keeps the
      inherited position.
    - sets `cls.__table__ = cls.__name__.lower()`.
    - unless `abstract=True`, registers the class. A duplicate name raises
      `TypeError("model 'X' already registered")`.
  - `__init__(self, **values)`: unknown keys raise
    `TypeError("<Cls>() got unexpected field(s): 'x'")`. Positional args are a
    `TypeError` (signature is kwargs-only). Each field is assigned from `values`,
    falling back to `field.default`, so validation runs through the descriptors.
  - `__repr__`: `Person(name='ann', age=3)` in declaration order.
  - `to_dict() -> dict[str, Any]`: field name to value, declaration order.
  - `@classmethod fields() -> dict[str, Field]`: a copy of the field mapping.
- `get_model(name: str) -> type[Model]`: `KeyError` with the name when missing.
- `table(name: str) -> Callable[[type], type]`: class decorator that sets
  `__table__` and returns the same class. `name` must satisfy `str.isidentifier()`,
  else `ValueError`.

Concepts: `__init_subclass__` with class keywords, generated `__init__`/`__repr__`,
class decorators, registries as class state, field inheritance.

## Part 3: Lazy-loading proxy

Implement in `main.py`:

- `class LazyProxy`
  - `__init__(self, factory: Callable[[], Any])`. The factory is **not** called here.
  - The target is built on the first attribute access that reaches it, then reused.
  - `__getattr__(name)`: only reached when normal lookup fails. It forwards to the
    target, loading it if needed. Names starting with `__` are **never** forwarded:
    raise `AttributeError`. This also guards against recursion while the instance
    is half-built (for example during copy/unpickle).
  - `__dir__()`: loads the target. Returns the sorted union of the proxy's own names
    and the target's.
  - `__repr__()`: `"LazyProxy(<not loaded>)"` when unloaded (must not load), otherwise
    `f"LazyProxy({target!r})"`.
  - `init_count` (property): number of successful factory calls.
  - `loaded` (property): whether a target is currently held.
  - `reset()`: discards the target so the next access calls the factory again.
  - If the factory raises, the error propagates, the target stays unloaded, and
    `init_count` is not incremented.
  - Dunder special methods bypass `__getattr__` entirely: `len(proxy)` raises
    `TypeError` and `proxy.__len__()` raises `AttributeError`, without loading.

Concepts: `__getattr__` vs `__getattribute__`, why dunders bypass forwarding,
lazy initialisation, instance state that must be read through `__dict__` in
hooks, name shadowing by proxy-owned attributes.

## Part 4: Cooperative mixins and MRO

Implement in `main.py`:

- `class Entity`: `__init__(self, *, id: int)` (does **not** call `super()`);
  `to_dict()` returns `{"id": ...}`.
- `class AuditMixin`
  - `__init__(self, *, actor: str, **kwargs)`: stores `actor`, starts
    `audit_log: list[str] = []`, then calls `super().__init__(**kwargs)`.
  - `record(action)` appends `f"{actor}:{action}"` to `audit_log`.
  - `to_dict()` returns `{**super().to_dict(), "actor": actor}`.
- `class JsonMixin`
  - `to_json(**dumps_kwargs) -> str`: `json.dumps(self.to_dict(), ...)`, with
    `sort_keys=True` by default unless the caller overrides it.
- `class ComparableMixin`
  - `_sort_key() -> tuple` raises `NotImplementedError`; subclasses override it.
  - `__eq__`, `__lt__`, `__le__`, `__gt__`, `__ge__`: compare `_sort_key()` only when
    `type(other) is type(self)`. Otherwise return `NotImplemented`.
  - `__hash__` is `hash((type(self), self._sort_key()))`.
- `class Order(AuditMixin, JsonMixin, ComparableMixin, Entity)` with
  `_sort_key()` returning `(self.id,)`.

The MRO must be exactly
`[Order, AuditMixin, JsonMixin, ComparableMixin, Entity, object]`.

Concepts: cooperative `super()`, keyword-only constructor chains, MRO
inspection, `NotImplemented` protocol, `__eq__` implies `__hash__` handling.

## Part 5: `__slots__` event records and copy protocols

Implement in `main.py`:

- `class Event`
  - `__slots__ = ("name", "ts", "payload")`
  - `__init__(self, name: str, *, ts: int, payload: Mapping[str, Any] | None = None)`:
    the payload is copied, so later mutation of the caller's dict does not leak in.
    Default is a fresh empty dict per instance.
  - instances have no `__dict__`; assigning an unknown attribute raises `AttributeError`.
  - a subclass that does not declare `__slots__` gets a `__dict__` back.
  - `__repr__`: `Event(name='login', ts=5, payload={'user': 'ann'})`.
  - `__copy__()`: a new `Event` with the same `payload` **object** (shallow).
  - `__deepcopy__(memo)`: deep-copies the payload through `copy.deepcopy(..., memo)`
    and registers itself in `memo` before recursing.
  - pickling works without custom hooks (the slotted state round-trips).

Concepts: `__slots__` semantics and gotchas, `__copy__`/`__deepcopy__` with the
memo protocol, constructing instances without `__init__` (`cls.__new__`), shallow
vs deep copy identity.

## Part 6: Self-registering plugins, `Protocol`, `__call__`, `__class_getitem__`

Implement in `main.py`:

- `@runtime_checkable class Stage(Protocol)`: a single member, `__call__(self, text: str) -> str`.
- `class Plugin`
  - `registry: ClassVar[dict[str, type[Plugin]]]`
  - `plugin_name: ClassVar[str | None]`: `None` unless the class declares `name=`.
    It is not inherited.
  - `__init_subclass__(cls, *, name: str | None = None, **kwargs)`:
    - `name is None`: the class is not registered.
    - non-`str` name: `TypeError`. Blank or whitespace-only name: `ValueError`.
    - duplicate name: `ValueError("plugin 'x' already registered")`.
  - `__init__(self, **config)`: stores `config` as a dict and sets `calls = 0`.
  - `transform(text) -> str` raises `NotImplementedError`; subclasses override it.
  - `__call__(text)`: increments `calls`, then returns `self.transform(text)`.
  - `@classmethod create(name, **config) -> Plugin`: instantiates the registered
    class with `**config`. Unknown name: `KeyError` containing the name.
  - `@classmethod available() -> tuple[str, ...]`: sorted registered names.
  - `__class_getitem__(item)` returns `types.GenericAlias(cls, item)`, so
    `Plugin[str]` works at runtime without `Generic`.

Concepts: `__init_subclass__` class keywords, `Protocol` + `runtime_checkable`
(structural `isinstance`/`issubclass`, and what they do *not* check), `__call__`
as an object protocol, `__class_getitem__`, validating class-keyword arguments.

## Run

```bash
uv run pytest exercises/08_oop_advanced                     # all parts
uv run pytest exercises/08_oop_advanced -k part2            # one part
uv run pytest exercises/08_oop_advanced --solutions         # reference check
```

## Stretch (untested)

- Make `LazyProxy` thread-safe with a lock so concurrent first access calls the
  factory exactly once. Measure it with `threading.Barrier`.
- Give `Field` a `default_factory` for mutable defaults and reject shared
  mutable defaults at class creation.
- Add `__set_name__`-time checks that reject a `Field` assigned to two names.
- Replace `Plugin.registry` with a `weakref.WeakValueDictionary` and discuss the
  lifetime trade-off.
