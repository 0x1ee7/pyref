"""OOP fundamentals: reference implementation.

A small commerce and banking core built from value objects, containers,
an account hierarchy, dataclasses, ABCs, and an immutable version type.
"""

from __future__ import annotations

import json
import operator
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any, ClassVar, Final, Literal, Self

# ---------------------------------------------------------------------------
# Part 1: Money
# ---------------------------------------------------------------------------

MINOR_UNITS: Final[dict[str, int]] = {"JPY": 0, "KRW": 0, "KWD": 3}
DEFAULT_MINOR_UNITS: Final = 2
_CURRENCY_RE: Final = re.compile(r"[A-Z]{3}")


class CurrencyMismatchError(ValueError):
    """Raised when two amounts in different currencies are combined or compared."""


def _to_decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise TypeError("bool is not a valid amount")
    if isinstance(value, float):
        raise TypeError("float amounts are not allowed; pass a Decimal or str instead")
    if isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, Decimal):
        number = value
    elif isinstance(value, str):
        try:
            number = Decimal(value)
        except InvalidOperation:
            raise ValueError(f"not a decimal amount: {value!r}") from None
    else:
        raise TypeError(f"unsupported amount type: {type(value).__name__}")
    if not number.is_finite():
        raise ValueError("amount must be finite")
    return number


def _minor_places(currency: str) -> int:
    return MINOR_UNITS.get(currency, DEFAULT_MINOR_UNITS)


class Money:
    """Immutable, currency-aware, exact decimal amount."""

    __slots__ = ("_amount", "_currency")

    def __init__(self, amount: Decimal | int | str, currency: str) -> None:
        self._amount = _to_decimal(amount)
        if not isinstance(currency, str):
            raise TypeError("currency must be a str")
        if not _CURRENCY_RE.fullmatch(currency):
            raise ValueError(f"currency must be three uppercase letters, got {currency!r}")
        self._currency = currency

    @classmethod
    def zero(cls, currency: str) -> Money:
        return cls(0, currency)

    @property
    def amount(self) -> Decimal:
        return self._amount

    @property
    def currency(self) -> str:
        return self._currency

    def _check_currency(self, other: Money) -> None:
        if self._currency != other._currency:
            raise CurrencyMismatchError(f"cannot combine {self._currency} with {other._currency}")

    def __add__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._check_currency(other)
        return Money(self._amount + other._amount, self._currency)

    def __radd__(self, other: object) -> Money:
        # sum() starts from the integer 0; accept it so sum(moneys) works.
        if isinstance(other, int) and not isinstance(other, bool) and other == 0:
            return self
        return NotImplemented

    def __sub__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._check_currency(other)
        return Money(self._amount - other._amount, self._currency)

    def __mul__(self, factor: object) -> Money:
        if isinstance(factor, bool) or not isinstance(factor, (int, Decimal)):
            return NotImplemented
        return Money(self._amount * Decimal(factor), self._currency)

    __rmul__ = __mul__

    def __neg__(self) -> Money:
        return Money(-self._amount, self._currency)

    def _compare(self, other: object, op: Callable[[Decimal, Decimal], bool]) -> bool:
        if not isinstance(other, Money):
            return NotImplemented  # type: ignore[return-value]
        self._check_currency(other)
        return op(self._amount, other._amount)

    def __lt__(self, other: object) -> bool:
        return self._compare(other, operator.lt)

    def __le__(self, other: object) -> bool:
        return self._compare(other, operator.le)

    def __gt__(self, other: object) -> bool:
        return self._compare(other, operator.gt)

    def __ge__(self, other: object) -> bool:
        return self._compare(other, operator.ge)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self._currency == other._currency and self._amount == other._amount

    def __hash__(self) -> int:
        return hash((self._amount, self._currency))

    def __bool__(self) -> bool:
        return self._amount != 0

    def __repr__(self) -> str:
        return f"Money({format(self._amount, 'f')!r}, {self._currency!r})"

    def __str__(self) -> str:
        return f"{format(self._amount, 'f')} {self._currency}"

    def rounded(self) -> Money:
        quantum = Decimal(1).scaleb(-_minor_places(self._currency))
        return Money(self._amount.quantize(quantum, rounding=ROUND_HALF_EVEN), self._currency)

    def allocate(self, parts: int) -> tuple[Money, ...]:
        if isinstance(parts, bool) or not isinstance(parts, int):
            raise TypeError("parts must be an int")
        if parts < 1:
            raise ValueError("parts must be at least 1")
        places = _minor_places(self._currency)
        if self._amount != self._amount.quantize(Decimal(1).scaleb(-places)):
            raise ValueError(f"amount has more precision than {places} minor-unit places")
        units = int(self._amount.scaleb(places))
        sign = -1 if units < 0 else 1
        base, remainder = divmod(abs(units), parts)
        shares = []
        for index in range(parts):
            share_units = base + (1 if index < remainder else 0)
            shares.append(Money(Decimal(sign * share_units).scaleb(-places), self._currency))
        return tuple(shares)


# ---------------------------------------------------------------------------
# Part 2: Product
# ---------------------------------------------------------------------------


class Product:
    """A sellable item. ``total_created`` is a class-wide construction counter."""

    total_created: int = 0
    _SKU_RE: ClassVar[re.Pattern[str]] = re.compile(r"[A-Z0-9-]{4,16}")

    def __init__(self, sku: str, name: str, price: Money, quantity: int = 0) -> None:
        self._sku = self._validate_sku(sku)
        self.name = name
        self.price = price
        self.quantity = quantity
        # Reference the class explicitly: `self.total_created += 1` would create
        # an instance attribute and silently shadow the class counter.
        Product.total_created += 1

    @staticmethod
    def _validate_sku(sku: object) -> str:
        if not isinstance(sku, str):
            raise TypeError("sku must be a str")
        if not Product._SKU_RE.fullmatch(sku):
            raise ValueError(f"sku must be 4-16 chars of A-Z, 0-9 or '-': {sku!r}")
        return sku

    @property
    def sku(self) -> str:
        return self._sku

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("name must be a str")
        stripped = value.strip()
        if not stripped or len(stripped) > 100:
            raise ValueError("name must be 1-100 characters after stripping")
        self._name = stripped

    @property
    def price(self) -> Money:
        return self._price

    @price.setter
    def price(self, value: Money) -> None:
        if not isinstance(value, Money):
            raise TypeError("price must be a Money")
        self._price = value

    @property
    def quantity(self) -> int:
        return self._quantity

    @quantity.setter
    def quantity(self, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("quantity must be an int")
        if value < 0:
            raise ValueError("quantity cannot be negative")
        self._quantity = value

    @property
    def in_stock(self) -> bool:
        return self._quantity > 0

    def restock(self, count: int) -> None:
        if isinstance(count, bool) or not isinstance(count, int):
            raise TypeError("count must be an int")
        if count < 1:
            raise ValueError("restock count must be at least 1")
        self.quantity = self._quantity + count

    @classmethod
    def count_created(cls) -> int:
        return Product.total_created


# ---------------------------------------------------------------------------
# Part 3: Accounts
# ---------------------------------------------------------------------------


class InsufficientFundsError(Exception):
    """Withdrawal exceeds what the account can pay out."""


class WithdrawalLimitExceeded(Exception):
    """Savings account has used up its withdrawals for the period."""


def _require_money(value: object) -> Money:
    if not isinstance(value, Money):
        raise TypeError("expected a Money")
    return value


def _require_positive(amount: object) -> Money:
    money = _require_money(amount)
    if not money > Money.zero(money.currency):
        raise ValueError("amount must be positive")
    return money


class Account:
    """Base bank account: balance plus deposit and withdraw."""

    def __init__(self, owner: str, balance: Money | None = None) -> None:
        if not isinstance(owner, str):
            raise TypeError("owner must be a str")
        if not owner.strip():
            raise ValueError("owner must not be blank")
        self._owner = owner.strip()
        self._balance = Money.zero("USD") if balance is None else _require_money(balance)

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def balance(self) -> Money:
        return self._balance

    @property
    def available_funds(self) -> Money:
        return self._balance

    def deposit(self, amount: Money) -> Money:
        self._balance = self._balance + _require_positive(amount)
        return self._balance

    def withdraw(self, amount: Money) -> Money:
        amount = _require_positive(amount)
        if amount > self.available_funds:
            raise InsufficientFundsError(f"{self._owner} cannot withdraw {amount}")
        self._balance = self._balance - amount
        return self._balance


class CheckingAccount(Account):
    """Account that may go negative down to an overdraft limit."""

    def __init__(
        self,
        owner: str,
        balance: Money | None = None,
        overdraft_limit: Money | None = None,
    ) -> None:
        super().__init__(owner, balance)
        if overdraft_limit is None:
            overdraft_limit = Money.zero(self._balance.currency)
        _require_money(overdraft_limit)
        if overdraft_limit < Money.zero(self._balance.currency):
            raise ValueError("overdraft limit cannot be negative")
        self._overdraft_limit = overdraft_limit

    @property
    def overdraft_limit(self) -> Money:
        return self._overdraft_limit

    @property
    def available_funds(self) -> Money:
        return self._balance + self._overdraft_limit


class SavingsAccount(Account):
    """Account with a cap on successful withdrawals per period."""

    def __init__(
        self,
        owner: str,
        balance: Money | None = None,
        *,
        max_withdrawals: int = 3,
    ) -> None:
        super().__init__(owner, balance)
        if isinstance(max_withdrawals, bool) or not isinstance(max_withdrawals, int):
            raise TypeError("max_withdrawals must be an int")
        if max_withdrawals < 0:
            raise ValueError("max_withdrawals cannot be negative")
        self._max_withdrawals = max_withdrawals
        self._withdrawals = 0

    @property
    def max_withdrawals(self) -> int:
        return self._max_withdrawals

    @property
    def withdrawals_this_period(self) -> int:
        return self._withdrawals

    def reset_period(self) -> None:
        self._withdrawals = 0

    def withdraw(self, amount: Money) -> Money:
        if self._withdrawals >= self._max_withdrawals:
            raise WithdrawalLimitExceeded(f"limit of {self._max_withdrawals} withdrawals reached")
        new_balance = super().withdraw(amount)
        self._withdrawals += 1
        return new_balance


# ---------------------------------------------------------------------------
# Part 4: ShoppingCart
# ---------------------------------------------------------------------------


def _require_quantity(quantity: object) -> int:
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise TypeError("quantity must be an int")
    if quantity < 1:
        raise ValueError("quantity must be at least 1")
    return quantity


class ShoppingCart:
    """Container of product lines keyed by sku."""

    def __init__(self, currency: str = "USD") -> None:
        Money.zero(currency)  # validates the currency code
        self._currency = currency
        self._lines: dict[str, tuple[Product, int]] = {}

    @property
    def currency(self) -> str:
        return self._currency

    def add(self, product: Product, quantity: int = 1) -> None:
        if not isinstance(product, Product):
            raise TypeError("product must be a Product")
        quantity = _require_quantity(quantity)
        if product.price.currency != self._currency:
            raise CurrencyMismatchError(f"cart is {self._currency}, product is {product.price.currency}")
        if product.sku in self._lines:
            kept, existing = self._lines[product.sku]
            self._lines[product.sku] = (kept, existing + quantity)
        else:
            self._lines[product.sku] = (product, quantity)

    def remove(self, sku: str, quantity: int = 1) -> None:
        if sku not in self._lines:
            raise KeyError(sku)
        quantity = _require_quantity(quantity)
        product, present = self._lines[sku]
        if quantity > present:
            raise ValueError(f"cannot remove {quantity} of {sku}; only {present} in cart")
        if quantity == present:
            del self._lines[sku]
        else:
            self._lines[sku] = (product, present - quantity)

    def total(self) -> Money:
        return sum(
            (product.price * quantity for product, quantity in self._lines.values()),
            start=Money.zero(self._currency),
        )

    def __len__(self) -> int:
        return sum(quantity for _, quantity in self._lines.values())

    def __iter__(self) -> Iterator[tuple[str, int]]:
        for sku, (_, quantity) in self._lines.items():
            yield sku, quantity

    def __contains__(self, item: object) -> bool:
        if isinstance(item, Product):
            item = item.sku
        return isinstance(item, str) and item in self._lines

    def __getitem__(self, sku: str) -> int:
        return self._lines[sku][1]

    def __repr__(self) -> str:
        return f"ShoppingCart(currency={self._currency!r}, items={len(self)})"


# ---------------------------------------------------------------------------
# Part 5: Employee
# ---------------------------------------------------------------------------


def _non_blank(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a str")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{label} must not be blank")
    return stripped


def _clean_skills(skills: Iterable[str]) -> list[str]:
    if isinstance(skills, str):
        raise TypeError("skills must be a sequence of strings, not a single string")
    seen: dict[str, None] = {}
    for raw in skills:
        if not isinstance(raw, str):
            raise TypeError("each skill must be a str")
        skill = raw.strip().lower()
        if skill:
            seen.setdefault(skill)
    return list(seen)


@dataclass
class Employee:
    employee_id: int
    name: str
    email: str
    department: str
    salary: Money
    skills: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if isinstance(self.employee_id, bool) or not isinstance(self.employee_id, int):
            raise TypeError("employee_id must be an int")
        if self.employee_id <= 0:
            raise ValueError("employee_id must be positive")
        self.name = _non_blank(self.name, "name")
        self.department = _non_blank(self.department, "department")
        self.email = self.normalize_email(self.email)
        _require_money(self.salary)
        if self.salary < Money.zero(self.salary.currency):
            raise ValueError("salary cannot be negative")
        self.skills = _clean_skills(self.skills)

    @staticmethod
    def normalize_email(raw: str) -> str:
        if not isinstance(raw, str):
            raise TypeError("email must be a str")
        email = raw.strip().lower()
        local, separator, domain = email.partition("@")
        if not separator or not local or not domain or "@" in domain or any(c.isspace() for c in email):
            raise ValueError(f"invalid email: {raw!r}")
        return email

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        return cls(
            employee_id=int(data["employee_id"]),
            name=data["name"],
            email=data["email"],
            department=data["department"],
            salary=Money(data["salary"], data["currency"]),
            skills=data.get("skills", []),
        )

    @classmethod
    def from_csv_row(cls, row: Sequence[str]) -> Self:
        if len(row) != 7:
            raise ValueError(f"expected 7 columns, got {len(row)}")
        employee_id, name, email, department, salary, currency, skills = row
        return cls(
            employee_id=int(employee_id),
            name=name,
            email=email,
            department=department,
            salary=Money(salary, currency),
            skills=skills.split(";"),
        )


# ---------------------------------------------------------------------------
# Part 6: Notifiers
# ---------------------------------------------------------------------------


class NotificationError(Exception):
    """A channel failed to deliver a notification."""


def _require_message(message: object) -> str:
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message must be a non-blank str")
    return message


class Notifier(ABC):
    """A delivery channel. Subclasses provide a channel name and a send()."""

    @property
    @abstractmethod
    def channel_name(self) -> str: ...

    @abstractmethod
    def send(self, recipient: str, message: str) -> None: ...

    def describe(self) -> str:
        return f"{self.channel_name} notifier"


class EmailNotifier(Notifier):
    _ADDRESS_RE: ClassVar[re.Pattern[str]] = re.compile(r"[^@\s]+@[^@\s]+")

    def __init__(self, transport: Callable[[str, str], None]) -> None:
        self._transport = transport

    @property
    def channel_name(self) -> str:
        return "email"

    def send(self, recipient: str, message: str) -> None:
        if not self._ADDRESS_RE.fullmatch(recipient):
            raise ValueError(f"invalid email address: {recipient!r}")
        self._transport(recipient, _require_message(message))


class SMSNotifier(Notifier):
    MAX_LENGTH: ClassVar[int] = 160
    _E164_RE: ClassVar[re.Pattern[str]] = re.compile(r"\+[1-9][0-9]{6,14}")

    def __init__(self, transport: Callable[[str, str], None]) -> None:
        self._transport = transport

    @property
    def channel_name(self) -> str:
        return "sms"

    def send(self, recipient: str, message: str) -> None:
        if not self._E164_RE.fullmatch(recipient):
            raise ValueError(f"recipient must be E.164, got {recipient!r}")
        text = _require_message(message)
        if len(text) > self.MAX_LENGTH:
            raise ValueError(f"message exceeds {self.MAX_LENGTH} characters")
        self._transport(recipient, text)


class WebhookNotifier(Notifier):
    def __init__(self, endpoint: str, post: Callable[[str, bytes], int]) -> None:
        if not endpoint.startswith("https://"):
            raise ValueError("webhook endpoint must use https://")
        self._endpoint = endpoint
        self._post = post

    @property
    def channel_name(self) -> str:
        return "webhook"

    def send(self, recipient: str, message: str) -> None:
        target = _non_blank(recipient, "recipient")
        body = json.dumps({"message": _require_message(message), "recipient": target}, sort_keys=True)
        status = self._post(self._endpoint, body.encode("utf-8"))
        if not 200 <= status < 300:
            raise NotificationError(f"webhook responded with HTTP {status}")


class NotificationDispatcher:
    """Fan a message out to several channels, collecting per-channel outcomes."""

    def __init__(self, notifiers: Iterable[Notifier]) -> None:
        items = tuple(notifiers)
        by_channel: dict[str, Notifier] = {}
        for notifier in items:
            if not isinstance(notifier, Notifier):
                raise TypeError("dispatcher accepts only Notifier instances")
            if notifier.channel_name in by_channel:
                raise ValueError(f"duplicate channel: {notifier.channel_name!r}")
            by_channel[notifier.channel_name] = notifier
        self._by_channel = by_channel

    @property
    def channels(self) -> tuple[str, ...]:
        return tuple(self._by_channel)

    def __len__(self) -> int:
        return len(self._by_channel)

    def dispatch(self, message: str, recipients: Mapping[str, str]) -> dict[str, BaseException | None]:
        unknown = [channel for channel in recipients if channel not in self._by_channel]
        if unknown:
            raise KeyError(unknown[0])
        results: dict[str, BaseException | None] = {}
        for channel, recipient in recipients.items():
            try:
                self._by_channel[channel].send(recipient, message)
            except Exception as exc:
                results[channel] = exc
            else:
                results[channel] = None
        return results


# ---------------------------------------------------------------------------
# Part 7: Version
# ---------------------------------------------------------------------------

_IDENTIFIER_RE: Final = re.compile(r"[0-9A-Za-z-]+")
_SEMVER_RE: Final = re.compile(
    r"v?([0-9]+)\.([0-9]+)\.([0-9]+)(?:-([^+]+))?(?:\+([^+]+))?"
)
_NUMERIC_RE: Final = re.compile(r"0|[1-9][0-9]*")


def _non_negative(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an int")
    if value < 0:
        raise ValueError(f"{label} cannot be negative")
    return value


def _identifiers(value: object, label: str, *, strict_numeric: bool) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a str")
    if value == "":
        return ()
    parts = tuple(value.split("."))
    for part in parts:
        if not _IDENTIFIER_RE.fullmatch(part):
            raise ValueError(f"invalid {label} identifier: {part!r}")
        if strict_numeric and part.isdigit() and not _NUMERIC_RE.fullmatch(part):
            raise ValueError(f"numeric {label} identifiers cannot have leading zeros: {part!r}")
    return parts


class Version:
    """Immutable semantic version; build metadata is ignored by comparison."""

    __slots__ = ("_major", "_minor", "_patch", "_prerelease", "_build")

    def __init__(
        self,
        major: int,
        minor: int = 0,
        patch: int = 0,
        prerelease: str = "",
        build: str = "",
    ) -> None:
        self._major = _non_negative(major, "major")
        self._minor = _non_negative(minor, "minor")
        self._patch = _non_negative(patch, "patch")
        self._prerelease = _identifiers(prerelease, "prerelease", strict_numeric=True)
        self._build = _identifiers(build, "build", strict_numeric=False)

    @classmethod
    def parse(cls, text: str) -> Version:
        if not isinstance(text, str):
            raise TypeError("version text must be a str")
        match = _SEMVER_RE.fullmatch(text)
        if match is None:
            raise ValueError(f"not a semantic version: {text!r}")
        major, minor, patch, prerelease, build = match.groups()
        for core in (major, minor, patch):
            if not _NUMERIC_RE.fullmatch(core):
                raise ValueError(f"core version numbers cannot have leading zeros: {text!r}")
        return cls(int(major), int(minor), int(patch), prerelease or "", build or "")

    @property
    def major(self) -> int:
        return self._major

    @property
    def minor(self) -> int:
        return self._minor

    @property
    def patch(self) -> int:
        return self._patch

    @property
    def prerelease(self) -> str:
        return ".".join(self._prerelease)

    @property
    def build(self) -> str:
        return ".".join(self._build)

    def bump(self, part: Literal["major", "minor", "patch"] | str) -> Version:
        if part == "major":
            return Version(self._major + 1)
        if part == "minor":
            return Version(self._major, self._minor + 1)
        if part == "patch":
            return Version(self._major, self._minor, self._patch + 1)
        raise ValueError(f"unknown version part: {part!r}")

    def _precedence(self) -> tuple[int, int, int, tuple[int, tuple[tuple[int, int, str], ...]]]:
        if not self._prerelease:
            pre: tuple[int, tuple[tuple[int, int, str], ...]] = (1, ())
        else:
            pre = (0, tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in self._prerelease))
        return (self._major, self._minor, self._patch, pre)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._precedence() == other._precedence()

    def __hash__(self) -> int:
        return hash(self._precedence())

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._precedence() < other._precedence()

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._precedence() <= other._precedence()

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._precedence() > other._precedence()

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._precedence() >= other._precedence()

    def __str__(self) -> str:
        text = f"{self._major}.{self._minor}.{self._patch}"
        if self._prerelease:
            text += "-" + self.prerelease
        if self._build:
            text += "+" + self.build
        return text

    def __repr__(self) -> str:
        return f"Version.parse({str(self)!r})"
