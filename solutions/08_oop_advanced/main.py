"""Advanced OOP reference: descriptors, registries, proxies, mixins, slots, plugins."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from types import GenericAlias
from typing import Any, ClassVar, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Part 1: descriptor fields
# ---------------------------------------------------------------------------


class ValidationError(ValueError):
    """A field rejected a value. ``str()`` reads ``"<field>: <reason>"``."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


class Field:
    """Base data descriptor: validates on write and stores per instance.

    Defining ``__set__`` makes this a *data* descriptor, which takes precedence
    over the instance ``__dict__``. Values are kept under a private key so a
    stray ``obj.__dict__[name]`` write cannot bypass validation.
    """

    def __init__(self, *, required: bool = False, default: Any = None) -> None:
        self.required = required
        self.default = default
        self.name = ""
        self.storage_name = ""

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name
        self.storage_name = f"_{name}"

    def __get__(self, instance: object | None, owner: type | None = None) -> Any:
        if instance is None:
            return self
        return instance.__dict__.get(self.storage_name, self.default)

    def __set__(self, instance: object, value: Any) -> None:
        instance.__dict__[self.storage_name] = self.validate(value)

    def __delete__(self, instance: object) -> None:
        instance.__dict__.pop(self.storage_name, None)

    def validate(self, value: Any) -> Any:
        if value is None:
            if self.required:
                raise ValidationError(self.name, "is required")
            return None
        return self.clean(value)

    def clean(self, value: Any) -> Any:
        """Type/constraint checks for non-None values. Subclasses override."""
        return value


class StringField(Field):
    def __init__(
        self,
        *,
        max_length: int | None = None,
        required: bool = False,
        default: Any = None,
    ) -> None:
        if max_length is not None and max_length < 1:
            raise ValueError("max_length must be >= 1")
        super().__init__(required=required, default=default)
        self.max_length = max_length

    def clean(self, value: Any) -> str:
        if not isinstance(value, str):
            raise ValidationError(self.name, f"must be a str, got {type(value).__name__}")
        if self.max_length is not None and len(value) > self.max_length:
            raise ValidationError(self.name, f"must be at most {self.max_length} characters")
        return value


class IntegerField(Field):
    def __init__(
        self,
        *,
        min_value: int | None = None,
        max_value: int | None = None,
        required: bool = False,
        default: Any = None,
    ) -> None:
        if min_value is not None and max_value is not None and min_value > max_value:
            raise ValueError("min_value must not exceed max_value")
        super().__init__(required=required, default=default)
        self.min_value = min_value
        self.max_value = max_value

    def clean(self, value: Any) -> int:
        # bool is an int subclass; treating True as 1 is a classic silent bug.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(self.name, f"must be an int, got {type(value).__name__}")
        if self.min_value is not None and value < self.min_value:
            raise ValidationError(self.name, f"must be >= {self.min_value}")
        if self.max_value is not None and value > self.max_value:
            raise ValidationError(self.name, f"must be <= {self.max_value}")
        return value


# ---------------------------------------------------------------------------
# Part 2: models with an __init_subclass__ registry
# ---------------------------------------------------------------------------


class Model:
    registry: ClassVar[dict[str, type[Model]]] = {}
    _fields: ClassVar[dict[str, Field]] = {}
    __table__: ClassVar[str] = "model"

    def __init_subclass__(cls, *, abstract: bool = False, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        fields = dict(cls._fields)  # inherited first; overrides keep their slot
        for name, attr in vars(cls).items():
            if isinstance(attr, Field):
                fields[name] = attr
        cls._fields = fields
        cls.__table__ = cls.__name__.lower()
        if not abstract:
            if cls.__name__ in Model.registry:
                raise TypeError(f"model {cls.__name__!r} already registered")
            Model.registry[cls.__name__] = cls

    def __init__(self, **values: Any) -> None:
        unknown = values.keys() - self._fields.keys()
        if unknown:
            names = ", ".join(repr(n) for n in sorted(unknown))
            raise TypeError(f"{type(self).__name__}() got unexpected field(s): {names}")
        for name, field in self._fields.items():
            setattr(self, name, values.get(name, field.default))

    def __repr__(self) -> str:
        args = ", ".join(f"{name}={getattr(self, name)!r}" for name in self._fields)
        return f"{type(self).__name__}({args})"

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self._fields}

    @classmethod
    def fields(cls) -> dict[str, Field]:
        return dict(cls._fields)


def get_model(name: str) -> type[Model]:
    try:
        return Model.registry[name]
    except KeyError:
        raise KeyError(f"no model registered as {name!r}") from None


def table(name: str) -> Callable[[type], type]:
    if not name.isidentifier():
        raise ValueError(f"table name must be an identifier, got {name!r}")

    def decorate(cls: type) -> type:
        cls.__table__ = name  # type: ignore[attr-defined]
        return cls

    return decorate


# ---------------------------------------------------------------------------
# Part 3: lazy-loading proxy
# ---------------------------------------------------------------------------

_UNLOADED: Any = object()


class LazyProxy:
    """Builds its target on first use and forwards attribute access to it.

    ``__getattr__`` reads ``self.__dict__`` directly, never through normal
    attribute lookup, so it cannot recurse while the instance is half-built
    (copy/unpickle creates instances without calling ``__init__``).
    """

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._target: Any = _UNLOADED
        self._init_count = 0

    @property
    def init_count(self) -> int:
        return self._init_count

    @property
    def loaded(self) -> bool:
        return self._target is not _UNLOADED

    def reset(self) -> None:
        self._target = _UNLOADED

    def _load(self) -> Any:
        if self._target is _UNLOADED:
            self._target = self._factory()
            self._init_count += 1
        return self._target

    def __getattr__(self, name: str) -> Any:
        # Only reached when normal lookup fails. Dunders are never forwarded:
        # copy/pickle probe them and must not force an expensive load.
        if name.startswith("__") or "_target" not in self.__dict__:
            raise AttributeError(name)
        return getattr(self._load(), name)

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(dir(self._load())))

    def __repr__(self) -> str:
        if not self.loaded:
            return "LazyProxy(<not loaded>)"
        return f"LazyProxy({self._target!r})"


# ---------------------------------------------------------------------------
# Part 4: cooperative mixins
# ---------------------------------------------------------------------------


class Entity:
    def __init__(self, *, id: int) -> None:  # noqa: A002 - public field name
        # Terminal link in the cooperative chain: deliberately no super() call.
        self.id = id

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id}


class AuditMixin:
    def __init__(self, *, actor: str, **kwargs: Any) -> None:
        self.actor = actor
        self.audit_log: list[str] = []
        super().__init__(**kwargs)

    def record(self, action: str) -> None:
        self.audit_log.append(f"{self.actor}:{action}")

    def to_dict(self) -> dict[str, Any]:
        return {**super().to_dict(), "actor": self.actor}


class JsonMixin:
    # Annotation only: no runtime attribute, so it cannot shadow a real to_dict.
    to_dict: Callable[[], dict[str, Any]]

    def to_json(self, **dumps_kwargs: Any) -> str:
        dumps_kwargs.setdefault("sort_keys", True)
        return json.dumps(self.to_dict(), **dumps_kwargs)


class ComparableMixin:
    def _sort_key(self) -> tuple[Any, ...]:
        raise NotImplementedError

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return self._sort_key() == other._sort_key()  # type: ignore[attr-defined]

    def __lt__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return self._sort_key() < other._sort_key()  # type: ignore[attr-defined]

    def __le__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return self._sort_key() <= other._sort_key()  # type: ignore[attr-defined]

    def __gt__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return self._sort_key() > other._sort_key()  # type: ignore[attr-defined]

    def __ge__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return self._sort_key() >= other._sort_key()  # type: ignore[attr-defined]

    def __hash__(self) -> int:
        return hash((type(self), self._sort_key()))


class Order(AuditMixin, JsonMixin, ComparableMixin, Entity):
    def _sort_key(self) -> tuple[Any, ...]:
        return (self.id,)


# ---------------------------------------------------------------------------
# Part 5: __slots__ event records
# ---------------------------------------------------------------------------


class Event:
    __slots__ = ("name", "ts", "payload")

    def __init__(self, name: str, *, ts: int, payload: Mapping[str, Any] | None = None) -> None:
        self.name = name
        self.ts = ts
        self.payload: dict[str, Any] = dict(payload) if payload is not None else {}

    def __repr__(self) -> str:
        return f"Event(name={self.name!r}, ts={self.ts!r}, payload={self.payload!r})"

    def __copy__(self) -> Event:
        # Bypass __init__ so the payload object is shared, not re-copied.
        clone = type(self).__new__(type(self))
        clone.name = self.name
        clone.ts = self.ts
        clone.payload = self.payload
        return clone

    def __deepcopy__(self, memo: dict[int, Any]) -> Event:
        clone = type(self).__new__(type(self))
        memo[id(self)] = clone  # register before recursing to handle cycles
        clone.name = self.name
        clone.ts = self.ts
        clone.payload = copy.deepcopy(self.payload, memo)
        return clone


# ---------------------------------------------------------------------------
# Part 6: self-registering plugins
# ---------------------------------------------------------------------------


@runtime_checkable
class Stage(Protocol):
    def __call__(self, text: str) -> str: ...


class Plugin:
    registry: ClassVar[dict[str, type[Plugin]]] = {}
    plugin_name: ClassVar[str | None] = None

    def __init_subclass__(cls, *, name: str | None = None, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.plugin_name = name
        if name is None:
            return
        if not isinstance(name, str):
            raise TypeError(f"plugin name must be a str, got {type(name).__name__}")
        if not name.strip():
            raise ValueError("plugin name must be non-empty")
        if name in Plugin.registry:
            raise ValueError(f"plugin {name!r} already registered")
        Plugin.registry[name] = cls

    def __init__(self, **config: Any) -> None:
        self.config = dict(config)
        self.calls = 0

    def __class_getitem__(cls, item: Any) -> GenericAlias:
        return GenericAlias(cls, item)

    def transform(self, text: str) -> str:
        raise NotImplementedError

    def __call__(self, text: str) -> str:
        self.calls += 1
        return self.transform(text)

    @classmethod
    def create(cls, name: str, **config: Any) -> Plugin:
        try:
            plugin_cls = Plugin.registry[name]
        except KeyError:
            raise KeyError(f"unknown plugin {name!r}") from None
        return plugin_cls(**config)

    @classmethod
    def available(cls) -> tuple[str, ...]:
        return tuple(sorted(Plugin.registry))
