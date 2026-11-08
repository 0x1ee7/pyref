"""Namespaces and scope: closures, registries, class-body rules, and restricted evaluation."""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, TypeVar

# ---------------------------------------------------------------------------
# Part 1: LEGB lookup and closures with nonlocal
# ---------------------------------------------------------------------------


def resolve_scope(
    name: str,
    *,
    local_scope: Mapping[str, object],
    enclosing: Mapping[str, object],
    global_scope: Mapping[str, object],
    builtin_scope: Mapping[str, object],
) -> str:
    """Return which LEGB scope would satisfy ``name``: local, enclosing, global or builtin."""
    lookup_order = (
        ("local", local_scope),
        ("enclosing", enclosing),
        ("global", global_scope),
        ("builtin", builtin_scope),
    )
    for label, scope in lookup_order:
        if name in scope:
            return label
    raise NameError(f"name {name!r} is not defined")


def make_counter(start: int = 0, step: int = 1) -> Callable[[], int]:
    """Return a zero-argument callable yielding ``start``, ``start + step``, ... on each call."""
    if step == 0:
        raise ValueError("step must be non-zero")
    current = start

    def next_value() -> int:
        nonlocal current
        value = current
        current += step
        return value

    return next_value


def make_id_generator(prefix: str, width: int = 4) -> Callable[[], str]:
    """Return a callable producing ``"<prefix>-<n>"`` ids, zero-padded to ``width``, starting at 1."""
    if not prefix:
        raise ValueError("prefix must be non-empty")
    if width < 1:
        raise ValueError("width must be >= 1")
    counter = make_counter(start=1)

    def next_id() -> str:
        return f"{prefix}-{counter():0{width}d}"

    return next_id


# ---------------------------------------------------------------------------
# Part 2: Late binding
# ---------------------------------------------------------------------------


def late_bound_callbacks(labels: Sequence[str]) -> list[Callable[[], str]]:
    """Return one callback per label, all reading the same loop variable (the classic bug)."""
    callbacks: list[Callable[[], str]] = []
    for label in labels:
        # Intentionally late-bound: the closure reads ``label`` when called, not when created.
        callbacks.append(lambda: f"clicked {label}")
    return callbacks


def _make_handler(label: str) -> Callable[[], str]:
    """Bind ``label`` in a fresh enclosing scope for each handler."""

    def handler() -> str:
        return f"clicked {label}"

    return handler


def make_handlers(labels: Iterable[str]) -> dict[str, Callable[[], str]]:
    """Return ``{label: handler}`` where each handler reports its own label."""
    handlers: dict[str, Callable[[], str]] = {}
    for label in labels:
        if label in handlers:
            raise ValueError(f"duplicate label {label!r}")
        handlers[label] = _make_handler(label)
    return handlers


def _make_adder(offset: int) -> Callable[[int], int]:
    def add(value: int) -> int:
        return value + offset

    return add


def make_adders(offsets: Iterable[int]) -> list[Callable[[int], int]]:
    """Return one ``add(value)`` callable per offset, each bound to its own offset."""
    return [_make_adder(offset) for offset in offsets]


# ---------------------------------------------------------------------------
# Part 3: Module-level registry with global
# ---------------------------------------------------------------------------

F = TypeVar("F", bound=Callable[..., Any])

_REGISTRY: dict[str, Callable[..., Any]] = {}
_registrations_total = 0


def register(name: str) -> Callable[[F], F]:
    """Decorator registering a function as plugin ``name``; returns the function unchanged."""
    if not isinstance(name, str) or not name.isidentifier():
        raise ValueError(f"plugin name must be an identifier, got {name!r}")

    def decorator(func: F) -> F:
        global _registrations_total
        if name in _REGISTRY:
            raise ValueError(f"plugin {name!r} is already registered")
        _REGISTRY[name] = func
        _registrations_total += 1
        return func

    return decorator


def unregister(name: str) -> None:
    """Remove plugin ``name``; ``KeyError`` if it is not registered."""
    del _REGISTRY[name]


def get_plugin(name: str) -> Callable[..., Any]:
    """Return the plugin registered as ``name``; ``KeyError`` if absent."""
    return _REGISTRY[name]


def plugin_names() -> list[str]:
    """Names of registered plugins, in registration order."""
    return list(_REGISTRY)


def run_plugins(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Call every plugin with the same arguments; return ``{name: result}`` in registration order."""
    # Snapshot so a plugin that (un)registers others cannot disturb this iteration.
    return {name: plugin(*args, **kwargs) for name, plugin in list(_REGISTRY.items())}


def clear_plugins() -> None:
    """Remove all plugins. Mutates in place so existing references stay valid."""
    _REGISTRY.clear()


def total_registrations() -> int:
    """Number of successful ``register`` calls since import (unaffected by unregister/clear)."""
    return _registrations_total


# ---------------------------------------------------------------------------
# Part 4: Class-body scope and name mangling
# ---------------------------------------------------------------------------


class Scaler:
    """Scales integers by a class-level factor; ``TABLE`` is precomputed at class creation."""

    FACTOR = 3
    TABLE_SIZE = 5
    # Only the first iterable of a comprehension is evaluated in the class scope; everything
    # else runs in the comprehension's own function scope and cannot see FACTOR/TABLE_SIZE.
    TABLE = tuple(i * factor for factor, size in [(FACTOR, TABLE_SIZE)] for i in range(size))

    def scale(self, x: int) -> int:
        return x * self.FACTOR


def mangle_name(class_name: str, attribute: str) -> str:
    """Return the name CPython stores ``attribute`` under when used inside ``class class_name``."""
    if not attribute.startswith("__") or attribute.endswith("__"):
        return attribute
    stripped = class_name.lstrip("_")
    if not stripped:
        return attribute
    return f"_{stripped}{attribute}"


class InsufficientFunds(Exception):
    """Raised when a withdrawal exceeds the current balance."""


class Ledger:
    """A balance whose storage is name-mangled, so subclasses cannot clobber it by accident."""

    def __init__(self, opening: int = 0) -> None:
        if opening < 0:
            raise ValueError("opening balance must be >= 0")
        self.__balance = opening

    @property
    def balance(self) -> int:
        return self.__balance

    def deposit(self, amount: int) -> int:
        if amount <= 0:
            raise ValueError("deposit must be positive")
        self.__balance += amount
        return self.__balance

    def withdraw(self, amount: int) -> int:
        if amount <= 0:
            raise ValueError("withdrawal must be positive")
        if amount > self.__balance:
            raise InsufficientFunds(f"cannot withdraw {amount}; balance is {self.__balance}")
        self.__balance -= amount
        return self.__balance


# ---------------------------------------------------------------------------
# Part 5: Module namespaces
# ---------------------------------------------------------------------------

_MISSING = object()


@contextmanager
def scoped_override(module: ModuleType, name: str, value: object) -> Iterator[None]:
    """Temporarily set ``module.<name>`` to ``value``; restore it, or delete it, on exit."""
    namespace = vars(module)
    previous = namespace.get(name, _MISSING)
    namespace[name] = value
    try:
        yield
    finally:
        if previous is _MISSING:
            namespace.pop(name, None)
        else:
            namespace[name] = previous


def public_names(namespace: Mapping[str, object]) -> list[str]:
    """Sorted names that are not underscore-prefixed and do not refer to modules."""
    return sorted(
        name
        for name, value in namespace.items()
        if not name.startswith("_") and not isinstance(value, ModuleType)
    )


# ---------------------------------------------------------------------------
# Part 6: Executing a config file into a fresh namespace
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Raised when a config file fails to compile or execute."""


def load_config(path: str | Path) -> dict[str, Any]:
    """Execute a Python config file in an empty namespace; return its UPPERCASE public names."""
    config_path = Path(path)
    source = config_path.read_text(encoding="utf-8")
    namespace: dict[str, Any] = {"__name__": "pyref_config", "__file__": str(config_path)}
    try:
        exec(compile(source, str(config_path), "exec"), namespace)
    except Exception as exc:
        raise ConfigError(f"failed to load config {config_path}: {exc}") from exc
    return {
        name: value
        for name, value in namespace.items()
        if name.isupper() and not name.startswith("_")
    }


# ---------------------------------------------------------------------------
# Part 7: Formula evaluation with an explicit, restricted namespace
# ---------------------------------------------------------------------------


class FormulaError(Exception):
    """Base class for every formula failure raised by :func:`evaluate`."""


class UnknownNameError(FormulaError):
    """A cell or function name is not provided by the caller."""


class ForbiddenSyntaxError(FormulaError):
    """The formula uses syntax outside the supported grammar."""


DEFAULT_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "ABS": abs,
    "MIN": min,
    "MAX": max,
    "ROUND": round,
}

_ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    ast.Name,
    ast.Call,
    ast.Load,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.UAdd,
    ast.USub,
)


def _validate(tree: ast.Expression, cells: Mapping[str, float], functions: Mapping[str, Any]) -> None:
    call_targets = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ForbiddenSyntaxError(f"{type(node).__name__} is not allowed in formulas")
        if isinstance(node, ast.Constant) and (
            isinstance(node.value, bool) or not isinstance(node.value, (int, float))
        ):
            raise ForbiddenSyntaxError(f"only int and float literals are allowed, got {node.value!r}")
        if isinstance(node, ast.Call) and (node.keywords or not isinstance(node.func, ast.Name)):
            raise ForbiddenSyntaxError("only plain function calls with positional arguments are allowed")

    for node in ast.walk(tree):
        if not isinstance(node, ast.Name):
            continue
        if node.id.startswith("__"):
            raise ForbiddenSyntaxError(f"dunder name {node.id!r} is not allowed")
        pool = functions if id(node) in call_targets else cells
        if node.id not in pool:
            raise UnknownNameError(f"unknown name {node.id!r}")


def evaluate(
    expression: str,
    cells: Mapping[str, float],
    functions: Mapping[str, Callable[..., Any]] | None = None,
) -> float:
    """Evaluate an arithmetic formula over ``cells`` and whitelisted ``functions``.

    The formula is validated against a small AST grammar before it is compiled, so
    no attribute access, subscripts, comprehensions, or builtins can be reached.
    """
    available_functions = {**DEFAULT_FUNCTIONS, **(functions or {})}
    overlap = cells.keys() & available_functions.keys()
    if overlap:
        raise ValueError(f"names used as both cells and functions: {sorted(overlap)}")

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"invalid formula {expression!r}: {exc.msg}") from exc
    _validate(tree, cells, available_functions)

    namespace: dict[str, Any] = {**cells, **available_functions, "__builtins__": {}}
    code = compile(tree, "<formula>", "eval")
    try:
        return float(eval(code, namespace))
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise FormulaError(f"cannot evaluate {expression!r}: {exc}") from exc
