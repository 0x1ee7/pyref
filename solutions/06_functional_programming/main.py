"""Reference implementations for the functional programming exercises."""

from __future__ import annotations

import re
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import partial, reduce
from operator import attrgetter, itemgetter, methodcaller
from types import MappingProxyType
from typing import Any, Generic, Literal, TypeVar

T = TypeVar("T")
U = TypeVar("U")
E = TypeVar("E")

# ---------------------------------------------------------------------------
# Part 1: composition and currying
# ---------------------------------------------------------------------------


def _identity(value: Any) -> Any:
    return value


def compose(*funcs: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """Return a function applying ``funcs`` right to left: ``compose(f, g)(x) == f(g(x))``."""
    if not funcs:
        return _identity
    ordered = tuple(reversed(funcs))

    def composed(value: Any) -> Any:
        for func in ordered:
            value = func(value)
        return value

    return composed


def pipe(value: Any, *funcs: Callable[[Any], Any]) -> Any:
    """Thread ``value`` through ``funcs`` left to right."""
    for func in funcs:
        value = func(value)
    return value


def curry(fn: Callable[..., T], arity: int) -> Callable[..., Any]:
    """Accumulate arguments across calls until ``arity`` are collected, then call ``fn``."""
    if arity < 1:
        raise ValueError(f"arity must be >= 1, got {arity}")

    def accumulate_args(collected: tuple[Any, ...]) -> Callable[..., Any]:
        def curried(*args: Any) -> Any:
            combined = (*collected, *args)
            if len(combined) >= arity:
                return fn(*combined)
            return accumulate_args(combined)

        return curried

    return accumulate_args(())


def partial_right(fn: Callable[..., T], *bound: Any) -> Callable[..., T]:
    """Bind positional arguments to the *right* of the call: ``f(a, *bound)``."""

    def wrapper(*args: Any, **kwargs: Any) -> T:
        return fn(*args, *bound, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Part 2: immutable nested updates
# ---------------------------------------------------------------------------


def _update(node: Any, path: tuple[Hashable, ...], fn: Callable[[Any], Any]) -> Any:
    key, rest = path[0], path[1:]
    if node is None:
        node = {}
    if isinstance(node, Mapping):
        old = node.get(key)
        new = fn(old) if not rest else _update(old, rest, fn)
        return {**node, key: new}
    if isinstance(node, (list, tuple)):
        if not isinstance(key, int) or isinstance(key, bool):
            raise TypeError(f"sequence index must be int, got {type(key).__name__}")
        if not -len(node) <= key < len(node):
            raise IndexError(f"index {key} out of range for length {len(node)}")
        items = list(node)
        items[key] = fn(items[key]) if not rest else _update(items[key], rest, fn)
        return items if isinstance(node, list) else tuple(items)
    raise TypeError(f"cannot descend into {type(node).__name__} with key {key!r}")


def update_in(data: Any, path: Iterable[Hashable], fn: Callable[[Any], Any]) -> Any:
    """Return a copy of ``data`` with ``fn`` applied to the value at ``path``."""
    steps = tuple(path)
    if not steps:
        raise ValueError("path must not be empty")
    return _update(data, steps, fn)


def assoc_in(data: Any, path: Iterable[Hashable], value: Any) -> Any:
    """Return a copy of ``data`` with ``value`` stored at ``path``."""
    return update_in(data, path, lambda _old: value)


def dissoc(mapping: Mapping[Any, Any], *keys: Hashable) -> dict[Any, Any]:
    """Return a new dict without ``keys``; missing keys are ignored."""
    doomed = set(keys)
    return {key: value for key, value in mapping.items() if key not in doomed}


# ---------------------------------------------------------------------------
# Part 3: Redux-style cart reducer
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LineItem:
    sku: str
    name: str
    unit_price_cents: int
    quantity: int


@dataclass(frozen=True, slots=True)
class CartState:
    items: tuple[LineItem, ...] = ()
    coupon: str | None = None


@dataclass(frozen=True, slots=True)
class AddItem:
    sku: str
    name: str
    unit_price_cents: int
    quantity: int = 1


@dataclass(frozen=True, slots=True)
class RemoveItem:
    sku: str


@dataclass(frozen=True, slots=True)
class SetQuantity:
    sku: str
    quantity: int


@dataclass(frozen=True, slots=True)
class ApplyCoupon:
    code: str | None


@dataclass(frozen=True, slots=True)
class ClearCart:
    pass


CartAction = AddItem | RemoveItem | SetQuantity | ApplyCoupon | ClearCart

# Coupon code -> percent off the subtotal.
COUPONS: Mapping[str, int] = MappingProxyType({"SAVE10": 10, "HALF": 50})


def cart_reducer(state: CartState, action: CartAction) -> CartState:
    """Pure reducer: returns a new ``CartState`` and never touches ``state``."""
    match action:
        case AddItem(sku=sku, name=name, unit_price_cents=price, quantity=qty):
            if qty < 1:
                raise ValueError(f"quantity must be >= 1, got {qty}")
            if price < 0:
                raise ValueError(f"unit price must be >= 0, got {price}")
            if any(item.sku == sku for item in state.items):
                items = tuple(
                    replace(item, quantity=item.quantity + qty) if item.sku == sku else item
                    for item in state.items
                )
            else:
                items = (*state.items, LineItem(sku, name, price, qty))
            return replace(state, items=items)
        case RemoveItem(sku=sku):
            return replace(state, items=tuple(i for i in state.items if i.sku != sku))
        case SetQuantity(sku=sku, quantity=qty):
            if qty < 0:
                raise ValueError(f"quantity must be >= 0, got {qty}")
            if not any(item.sku == sku for item in state.items):
                raise ValueError(f"unknown sku {sku!r}")
            if qty == 0:
                return cart_reducer(state, RemoveItem(sku))
            items = tuple(replace(i, quantity=qty) if i.sku == sku else i for i in state.items)
            return replace(state, items=items)
        case ApplyCoupon(code=code):
            if code is not None and code not in COUPONS:
                raise ValueError(f"unknown coupon {code!r}")
            return replace(state, coupon=code)
        case ClearCart():
            return CartState()
        case _:
            raise TypeError(f"unknown cart action: {action!r}")


def subtotal_cents(state: CartState) -> int:
    return sum(item.unit_price_cents * item.quantity for item in state.items)


def total_cents(state: CartState) -> int:
    """Subtotal minus the coupon discount, which is floored to whole cents."""
    subtotal = subtotal_cents(state)
    percent = COUPONS.get(state.coupon, 0) if state.coupon is not None else 0
    return subtotal - subtotal * percent // 100


def run_actions(state: CartState, actions: Iterable[CartAction]) -> CartState:
    return reduce(cart_reducer, actions, state)


# ---------------------------------------------------------------------------
# Part 4: event-sourced bank account
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Opened:
    account_id: str
    owner: str


@dataclass(frozen=True, slots=True)
class Deposited:
    amount_cents: int


@dataclass(frozen=True, slots=True)
class Withdrawn:
    amount_cents: int


@dataclass(frozen=True, slots=True)
class Frozen:
    pass


@dataclass(frozen=True, slots=True)
class Unfrozen:
    pass


Event = Opened | Deposited | Withdrawn | Frozen | Unfrozen


@dataclass(frozen=True, slots=True)
class Account:
    account_id: str
    owner: str
    balance_cents: int = 0
    status: Literal["open", "frozen"] = "open"


def _require_positive(amount: int) -> None:
    if amount <= 0:
        raise ValueError(f"amount must be positive, got {amount}")


def _require_open(state: Account) -> None:
    if state.status == "frozen":
        raise ValueError(f"account {state.account_id!r} is frozen")


def apply_event(state: Account | None, event: Event) -> Account:
    """Fold a single event into the account state; ``None`` means "not opened yet"."""
    if isinstance(event, Opened):
        if state is not None:
            raise ValueError(f"account {state.account_id!r} already opened")
        return Account(account_id=event.account_id, owner=event.owner)
    if state is None:
        raise ValueError("account not opened")
    match event:
        case Deposited(amount_cents=amount):
            _require_positive(amount)
            _require_open(state)
            return replace(state, balance_cents=state.balance_cents + amount)
        case Withdrawn(amount_cents=amount):
            _require_positive(amount)
            _require_open(state)
            if amount > state.balance_cents:
                raise ValueError(
                    f"insufficient funds: balance {state.balance_cents}, requested {amount}"
                )
            return replace(state, balance_cents=state.balance_cents - amount)
        case Frozen():
            return replace(state, status="frozen")
        case Unfrozen():
            return replace(state, status="open")
        case _:
            raise TypeError(f"unknown event: {event!r}")


def replay(events: Iterable[Event]) -> Account:
    """Rebuild the current account state from its full event log."""
    state: Account | None = reduce(apply_event, events, None)
    if state is None:
        raise ValueError("cannot replay an empty event log")
    return state


def snapshots(events: Sequence[Event]) -> tuple[Account, ...]:
    """State after each event, in order (``len(result) == len(events)``)."""
    # accumulate(initial=None) treats None as "no initial", so fold explicitly.
    states: list[Account] = []
    state: Account | None = None
    for event in events:
        state = apply_event(state, event)
        states.append(state)
    return tuple(states)


def decide_withdraw(account: Account, amount_cents: int) -> Withdrawn:
    """Validate a withdrawal against ``account`` and return the event to record."""
    event = Withdrawn(amount_cents)
    apply_event(account, event)  # raises if the withdrawal is not allowed
    return event


# ---------------------------------------------------------------------------
# Part 5: Result type and exception-free parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ok(Generic[T]):
    value: T

    def is_ok(self) -> bool:
        return True

    def map(self, fn: Callable[[T], U]) -> Ok[U]:
        return Ok(fn(self.value))

    def map_err(self, fn: Callable[[Any], Any]) -> Ok[T]:
        return self

    def and_then(self, fn: Callable[[T], Result]) -> Result:
        return fn(self.value)

    def unwrap_or(self, default: Any) -> T:
        return self.value


@dataclass(frozen=True, slots=True)
class Err(Generic[E]):
    error: E

    def is_ok(self) -> bool:
        return False

    def map(self, fn: Callable[[Any], Any]) -> Err[E]:
        return self

    def map_err(self, fn: Callable[[E], U]) -> Err[U]:
        return Err(fn(self.error))

    def and_then(self, fn: Callable[[Any], Result]) -> Err[E]:
        return self

    def unwrap_or(self, default: Any) -> Any:
        return default


Result = Ok | Err


def try_call(fn: Callable[..., T], *args: Any, **kwargs: Any) -> Result:
    """Run ``fn``; wrap its return value in ``Ok`` or any ``Exception`` in ``Err``."""
    try:
        return Ok(fn(*args, **kwargs))
    except Exception as exc:
        return Err(exc)


def collect(results: Iterable[Result]) -> Result:
    """Turn an iterable of Results into a Result of a list; stops at the first ``Err``."""
    values: list[Any] = []
    for result in results:
        if isinstance(result, Err):
            return result
        values.append(result.value)
    return Ok(values)


def _parse_int(raw: str, name: str, lo: int, hi: int) -> Result:
    return (
        try_call(int, raw.strip())
        .map_err(lambda _exc: f"{name} must be an integer, got {raw!r}")
        .and_then(
            lambda number: Ok(number)
            if lo <= number <= hi
            else Err(f"{name} must be between {lo} and {hi}")
        )
    )


def parse_port(raw: str) -> Result:
    return _parse_int(raw, "port", 1, 65535)


def parse_ports(raw: str) -> Result:
    """Parse a comma-separated list of ports."""
    return collect(parse_port(part) for part in raw.split(","))


_TRUE_WORDS = frozenset({"true", "yes", "1", "on"})
_FALSE_WORDS = frozenset({"false", "no", "0", "off"})


def parse_bool(raw: str) -> Result:
    word = raw.strip().lower()
    if word in _TRUE_WORDS:
        return Ok(True)
    if word in _FALSE_WORDS:
        return Ok(False)
    return Err(f"expected boolean, got {raw!r}")


@dataclass(frozen=True, slots=True)
class Config:
    host: str
    port: int
    debug: bool = False
    workers: int = 4


_CONFIG_KEYS = frozenset({"host", "port", "debug", "workers"})


def _parse_entries(text: str) -> Result:
    entries: dict[str, str] = {}
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            return Err(f"line {number}: expected 'key = value'")
        key = key.strip()
        if key in entries:
            return Err(f"line {number}: duplicate key {key!r}")
        entries[key] = value.strip()
    return Ok(entries)


def _or_default(raw: str | None, default: Any, parse: Callable[[str], Result]) -> Result:
    return Ok(default) if raw is None else parse(raw)


def _build_config(entries: Mapping[str, str]) -> Result:
    unknown = sorted(set(entries) - _CONFIG_KEYS)
    if unknown:
        return Err(f"unknown key {unknown[0]!r}")
    if "host" not in entries:
        return Err("missing required key 'host'")
    if "port" not in entries:
        return Err("missing required key 'port'")
    host = entries["host"]
    if not host:
        return Err("host must not be empty")
    return parse_port(entries["port"]).and_then(
        lambda port: _or_default(entries.get("debug"), False, parse_bool).and_then(
            lambda debug: _or_default(
                entries.get("workers"), 4, lambda raw: _parse_int(raw, "workers", 1, 64)
            ).map(lambda workers: Config(host, port, debug, workers))
        )
    )


def parse_config(text: str) -> Result:
    """Parse ``key = value`` config text into a ``Config``; never raises."""
    return _parse_entries(text).and_then(_build_config)


# ---------------------------------------------------------------------------
# Part 6: validator combinators
# ---------------------------------------------------------------------------

Validator = Callable[[Any], list[str]]

USERNAME_PATTERN = r"[a-z0-9_]+"
EMAIL_PATTERN = r"[^@\s]+@[^@\s]+\.[^@\s]+"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_type(tp: type) -> Validator:
    """Type check. ``bool`` is rejected where ``int`` is expected."""

    def check(value: Any) -> list[str]:
        if isinstance(value, tp) and not (isinstance(value, bool) and tp is not bool):
            return []
        return [f"expected {tp.__name__}, got {type(value).__name__}"]

    return check


def min_length(n: int) -> Validator:
    """Judges strings only; other values are outside its domain and pass."""

    def check(value: Any) -> list[str]:
        if not isinstance(value, str) or len(value) >= n:
            return []
        return [f"length must be at least {n}"]

    return check


def matches(pattern: str) -> Validator:
    """Full-match against ``pattern``. Judges strings only."""
    regex = re.compile(pattern)

    def check(value: Any) -> list[str]:
        if not isinstance(value, str) or regex.fullmatch(value):
            return []
        return [f"must match pattern {pattern!r}"]

    return check


def in_range(lo: float, hi: float) -> Validator:
    """Inclusive numeric range. Judges ints/floats only (not bools)."""

    def check(value: Any) -> list[str]:
        if not _is_number(value) or lo <= value <= hi:
            return []
        return [f"must be between {lo} and {hi}"]

    return check


def all_of(*validators: Validator) -> Validator:
    """Run every validator and concatenate all errors."""

    def check(value: Any) -> list[str]:
        return [error for validator in validators for error in validator(value)]

    return check


def any_of(*validators: Validator) -> Validator:
    """Pass if any validator passes (short-circuiting); otherwise concatenate all errors."""
    if not validators:
        raise ValueError("any_of needs at least one validator")

    def check(value: Any) -> list[str]:
        errors: list[str] = []
        for validator in validators:
            branch_errors = validator(value)
            if not branch_errors:
                return []
            errors.extend(branch_errors)
        return errors

    return check


def field(name: str, validator: Validator) -> Validator:
    """Validate ``record[name]``; a missing key is an error. Non-mappings are outside its domain."""

    def check(record: Any) -> list[str]:
        if not isinstance(record, Mapping):
            return []
        if name not in record:
            return [f"{name}: missing"]
        return [f"{name}: {error}" for error in validator(record[name])]

    return check


def optional(validator: Validator) -> Validator:
    """``None`` is valid; anything else must satisfy ``validator``."""

    def check(value: Any) -> list[str]:
        return [] if value is None else validator(value)

    return check


def each(validator: Validator) -> Validator:
    """Validate every element of a list or tuple. Other values are outside its domain."""

    def check(value: Any) -> list[str]:
        if not isinstance(value, (list, tuple)):
            return []
        return [f"[{i}] {error}" for i, item in enumerate(value) for error in validator(item)]

    return check


def _optional_key(name: str, validator: Validator) -> Validator:
    """Like ``field`` but an absent key is valid; a present key must satisfy ``validator``."""

    def check(record: Any) -> list[str]:
        if not isinstance(record, Mapping) or name not in record:
            return []
        return [f"{name}: {error}" for error in validator(record[name])]

    return check


def validate_signup(payload: Any) -> list[str]:
    return all_of(
        is_type(Mapping),
        field("username", all_of(is_type(str), min_length(3), matches(USERNAME_PATTERN))),
        field("age", all_of(is_type(int), in_range(13, 120))),
        field("email", all_of(is_type(str), matches(EMAIL_PATTERN))),
        _optional_key("tags", all_of(is_type(list), each(all_of(is_type(str), min_length(1))))),
    )(payload)


# ---------------------------------------------------------------------------
# Part 7: ETL pipeline and reporting
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sale:
    region: str
    product: str
    units: int
    unit_price_cents: int
    sold_on: date


def parse_cents(raw: str) -> int:
    """Parse a non-negative money string with at most two decimals into cents."""
    try:
        amount = Decimal(raw.strip())
    except InvalidOperation as exc:
        raise ValueError(f"invalid money amount: {raw!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"money amount must be finite and non-negative: {raw!r}")
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"money amount has more than two decimals: {raw!r}")
    return int(cents)


def parse_sale(row: Mapping[str, str]) -> Sale:
    units = int(row["units"].strip())
    if units < 0:
        raise ValueError(f"units must be non-negative, got {units}")
    return Sale(
        region=row["region"].strip().lower(),
        product=row["product"].strip(),
        units=units,
        unit_price_cents=parse_cents(row["unit_price"]),
        sold_on=date.fromisoformat(row["sold_on"].strip()),
    )


def _is_live(row: Mapping[str, str]) -> bool:
    return bool(row["region"].strip()) and int(row["units"]) != 0


def clean_sales(rows: Iterable[Mapping[str, str]]) -> list[Sale]:
    """Drop rows with a blank region or zero units, then parse the rest, preserving order."""
    live = filter(_is_live, rows)
    return [parse_sale(row) for row in live]


def revenue_cents(sale: Sale) -> int:
    return sale.units * sale.unit_price_cents


def totals_by(sales: Iterable[Sale], key: Callable[[Sale], str]) -> dict[str, int]:
    """Sum revenue per key; the returned dict's keys are in ascending order."""
    totals: dict[str, int] = {}
    for sale in sales:
        group = key(sale)
        totals[group] = totals.get(group, 0) + revenue_cents(sale)
    return dict(sorted(totals.items(), key=itemgetter(0)))


def ranked(totals: Mapping[str, int]) -> list[tuple[str, int]]:
    """Sort by total descending, then name ascending.

    Two stable sorts: sort by name first, then by total descending; ``sorted``
    with ``reverse=True`` keeps equal elements in their original order.
    """
    by_name = sorted(totals.items(), key=itemgetter(0))
    return sorted(by_name, key=itemgetter(1), reverse=True)


def distinct_regions(sales: Iterable[Sale]) -> tuple[str, ...]:
    return tuple(sorted(set(map(methodcaller("upper"), map(attrgetter("region"), sales)))))


def format_cents(cents: int, *, currency: str = "$") -> str:
    if cents < 0:
        raise ValueError(f"cents must be non-negative, got {cents}")
    dollars, remainder = divmod(cents, 100)
    return f"{currency}{dollars:,}.{remainder:02d}"


def build_report(rows: Iterable[Mapping[str, str]], *, currency: str = "$") -> list[str]:
    """Clean raw rows and emit ``"<region>: <total>"`` lines, highest revenue first."""
    fmt = partial(format_cents, currency=currency)
    return pipe(
        rows,
        clean_sales,
        lambda sales: totals_by(sales, attrgetter("region")),
        ranked,
        lambda pairs: [f"{region}: {fmt(total)}" for region, total in pairs],
    )
