# 07 — OOP fundamentals

A small commerce and banking core: money values, products, a shopping cart, bank accounts, employee records, notification channels, and software versions. Every part builds one object-oriented tool that shows up in production code. The spec is the README plus `test_oop_fundamentals.py`. Implement everything in `main.py`.

Conventions: every class lives in `main.py`. Public attributes are read-only unless a Part says otherwise. Error types are exactly the ones named.

---

## Part 1 — `Money` value object

Immutable, currency-aware, exact decimal money.

```python
class CurrencyMismatchError(ValueError): ...

class Money:
    def __init__(self, amount: Decimal | int | str, currency: str) -> None: ...

    @classmethod
    def zero(cls, currency: str) -> Money: ...

    @property
    def amount(self) -> Decimal: ...      # read-only
    @property
    def currency(self) -> str: ...        # read-only

    def rounded(self) -> Money: ...
    def allocate(self, parts: int) -> tuple[Money, ...]: ...
```

Construction:
- `amount` accepts `int`, `Decimal`, or `str` (anything `Decimal(str)` parses). Store it as a `Decimal` exactly as given: `Money("1.005", "USD").amount == Decimal("1.005")`. No quantising on construction.
- `float` → `TypeError` (message points at Decimal/str). `bool` → `TypeError`. Other types → `TypeError`.
- Unparseable strings, `NaN`, and infinities → `ValueError`.
- `currency` must be a `str` (else `TypeError`) of exactly three ASCII uppercase letters (else `ValueError`). `"usd"` is invalid.
- Assigning to `amount` or `currency` raises `AttributeError`.

Arithmetic and comparison:
- `+` and `-` with another `Money` of the same currency return a new `Money`. Different currency → `CurrencyMismatchError`. Other operand types → `NotImplemented` (so Python raises `TypeError`).
- `sum()` must work. `sum(moneys)` starts from the integer `0`, so `Money.__radd__` must accept `0` and return the money unchanged. Any other non-`Money` left operand → `NotImplemented`.
- `*` and `rmul`: multiply by `int` or `Decimal` (not `bool`, not `float`, not `Money`). No rounding.
- Unary `-` returns the negated amount.
- `<`, `<=`, `>`, `>=`: same-currency `Money` only. Mismatched currency → `CurrencyMismatchError`. Non-`Money` → `NotImplemented`.
- `==`: equal when currency matches and amounts are numerically equal (`Money(1, "USD") == Money("1.00", "USD")`). Non-`Money` → `NotImplemented` (so `Money(1, "USD") == 1` is `False`).
- Hashable, and consistent with `==`: equal objects hash equally, so `{Money(1,"USD"), Money("1.00","USD")}` has one element.
- `bool(money)` is `False` exactly when the amount is zero.
- `repr` is eval-style and uses plain (non-scientific) notation: `Money('10.50', 'USD')`.
- `str` is `"10.50 USD"`, also non-scientific.

Rounding:
- `rounded()` quantises to the currency's minor units using `ROUND_HALF_EVEN` (banker's rounding). Minor units: `JPY` and `KRW` have 0 places, `KWD` has 3, everything else 2.
- `Money("1.005","USD").rounded() == Money("1.00","USD")`; `Money("1.015","USD").rounded() == Money("1.02","USD")`; `Money("100.5","JPY").rounded() == Money("100","JPY")`.

Allocation (no lost pennies):
- `allocate(parts)` splits the money into `parts` shares, each a `Money` with the currency's minor units. The shares sum exactly to `self`. The first shares receive the extra minor units. Example: `Money("10.00","USD").allocate(3)` → `3.34, 3.33, 3.33`.
- Negative amounts are allocated by magnitude and the sign is applied to each share: `Money("-10.00","USD").allocate(3)` → `-3.34, -3.33, -3.33`.
- `parts` must be an `int` (not `bool`) → else `TypeError`; `parts < 1` → `ValueError`.
- If the amount has more precision than the currency's minor units (`Money("1.005","USD").allocate(2)`) → `ValueError`.

## Part 2 — `Product`: class vs instance attributes and validated properties

```python
class Product:
    total_created: int = 0            # class attribute: count of every Product ever constructed

    def __init__(self, sku: str, name: str, price: Money, quantity: int = 0) -> None: ...

    @property
    def sku(self) -> str: ...         # read-only
    @property
    def name(self) -> str: ...        # settable
    @property
    def price(self) -> Money: ...     # settable
    @property
    def quantity(self) -> int: ...    # settable
    @property
    def in_stock(self) -> bool: ...

    def restock(self, count: int) -> None: ...
    @classmethod
    def count_created(cls) -> int: ...
```

- `sku`: a `str` (else `TypeError`) of 4–16 characters from `A-Z`, `0-9`, `-`. Otherwise `ValueError`. Read-only.
- `name`: a `str` (else `TypeError`). Stored stripped. Empty after stripping, or longer than 100 characters → `ValueError`.
- `price`: must be a `Money` (else `TypeError`).
- `quantity`: an `int` ≥ 0. `bool` or non-`int` → `TypeError`; negative → `ValueError`. Default `0`. Setter validates too.
- `restock(count)`: `count` must be an `int` ≥ 1 (validation as above, `count=0` is `ValueError`); adds to `quantity`.
- `in_stock`: `quantity > 0`.
- `total_created` is a **class attribute**. Every successful construction of `Product` or any subclass increments `Product.total_created` (always through the class `Product`, never through `self` or `type(self)`, so it is never shadowed by an instance attribute). A failed construction (validation error) does **not** increment it. `count_created()` is a classmethod returning `Product.total_created`.

## Part 3 — Account hierarchy with `super()`

```python
class InsufficientFundsError(Exception): ...
class WithdrawalLimitExceeded(Exception): ...

class Account:
    def __init__(self, owner: str, balance: Money | None = None) -> None: ...
    @property
    def owner(self) -> str: ...
    @property
    def balance(self) -> Money: ...
    @property
    def available_funds(self) -> Money: ...         # what withdraw() may take; base: balance
    def deposit(self, amount: Money) -> Money: ...  # returns new balance
    def withdraw(self, amount: Money) -> Money: ...  # returns new balance

class CheckingAccount(Account):
    def __init__(self, owner: str, balance: Money | None = None,
                 overdraft_limit: Money | None = None) -> None: ...
    @property
    def overdraft_limit(self) -> Money: ...         # default zero in balance's currency

class SavingsAccount(Account):
    def __init__(self, owner: str, balance: Money | None = None, *,
                 max_withdrawals: int = 3) -> None: ...
    @property
    def max_withdrawals(self) -> int: ...
    @property
    def withdrawals_this_period(self) -> int: ...
    def reset_period(self) -> None: ...
    def withdraw(self, amount: Money) -> Money: ...
```

- `owner`: non-empty `str` (stripped); otherwise `ValueError`/`TypeError` as appropriate.
- `balance` defaults to `Money.zero("USD")`. Must be a `Money` if given (`TypeError`).
- `deposit` and `withdraw` require a `Money` (else `TypeError`) with a strictly positive amount (else `ValueError`). Currency mismatch with the account → `CurrencyMismatchError`.
- `withdraw` succeeds iff `amount <= available_funds`; otherwise `InsufficientFundsError` and the balance is unchanged. Withdrawing exactly the available funds is allowed.
- `CheckingAccount`: `available_funds` = `balance + overdraft_limit`, so the balance may go negative down to `-overdraft_limit`. `overdraft_limit` must be a `Money` ≥ 0 in the balance's currency (else `ValueError`/`TypeError`). Reuse base `withdraw` via `super()` in `__init__`; do not duplicate validation logic.
- `SavingsAccount`: `max_withdrawals` is keyword-only, an `int` ≥ 0 (`bool` → `TypeError`, negative → `ValueError`). `withdraw` first checks the limit: if `withdrawals_this_period >= max_withdrawals`, raise `WithdrawalLimitExceeded` (even before amount validation). Otherwise delegate to `super().withdraw(...)`, and count only **successful** withdrawals. Deposits never count. `reset_period()` zeroes the counter.

## Part 4 — `ShoppingCart`: a container with dunder methods

```python
class ShoppingCart:
    def __init__(self, currency: str = "USD") -> None: ...
    @property
    def currency(self) -> str: ...
    def add(self, product: Product, quantity: int = 1) -> None: ...
    def remove(self, sku: str, quantity: int = 1) -> None: ...
    def total(self) -> Money: ...

    def __len__(self) -> int: ...                  # total units across all lines
    def __iter__(self) -> Iterator[tuple[str, int]]: ...   # (sku, quantity), insertion order
    def __contains__(self, item: object) -> bool: ...      # sku str or Product
    def __getitem__(self, sku: str) -> int: ...            # quantity for sku
    def __repr__(self) -> str: ...
```

- Invalid `currency` raises whatever `Money.zero` raises for a bad currency code.
- `add`: `product` must be a `Product` (else `TypeError`); `quantity` an `int` ≥ 1 (`bool` → `TypeError`, `< 1` → `ValueError`). `product.price.currency` must equal the cart currency, else `CurrencyMismatchError`. Adding a sku already present accumulates quantity.
- `remove(sku, quantity=1)`: unknown sku → `KeyError`. `quantity` validated as in `add`. Removing more than present → `ValueError`. Removing all units deletes the line.
- `__len__` is the sum of quantities. An empty cart is falsy and a non-empty cart is truthy (this follows from `__len__`; do not add a separate `__bool__`).
- `__iter__` yields `(sku, quantity)` tuples in insertion order.
- `__contains__`: a `Product` is checked by its `sku`; a `str` by membership; any other type → `False` (no exception).
- `__getitem__`: quantity for the sku; missing sku → `KeyError`.
- `total()`: `sum` of `price * quantity` over the lines, in the cart currency. Empty cart → `Money.zero(currency)`. Use `sum(..., start=...)` with the Money zero, not the integer `0`.
- `repr`: `ShoppingCart(currency='USD', items=3)`, where `items` is `len(cart)`.

## Part 5 — `Employee`: dataclass, `default_factory`, alternative constructors

```python
@dataclass
class Employee:
    employee_id: int
    name: str
    email: str
    department: str
    salary: Money
    skills: list[str] = field(default_factory=list)

    def __post_init__(self) -> None: ...

    @staticmethod
    def normalize_email(raw: str) -> str: ...

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self: ...

    @classmethod
    def from_csv_row(cls, row: Sequence[str]) -> Self: ...
```

Validation in `__post_init__` (applied to every construction path):
- `employee_id`: `int` > 0 (`bool` → `TypeError`; non-positive → `ValueError`).
- `name`, `department`: stripped; empty → `ValueError`; non-`str` → `TypeError`.
- `email`: normalised with `normalize_email`.
- `salary`: must be a `Money` (else `TypeError`), and not negative (`ValueError`).
- `skills`: must not be a bare `str` (`TypeError`). Each skill is stripped and lowercased. Empty skills are dropped. Duplicates are dropped, first occurrence kept. Result is a new `list`.

`normalize_email` (staticmethod, callable from class and instance): strip and lowercase. Must have exactly one `@`, non-empty local part and domain, and no whitespace anywhere. Otherwise `ValueError`. Non-`str` → `TypeError`.

`from_dict(data)`: required keys `employee_id`, `name`, `email`, `department`, `salary`, `currency`. Missing key → `KeyError`. `employee_id` may be an `int` or a digit string (coerced with `int`). `salary` is passed to `Money` with `currency` (so `"95000.00"` and `Decimal` both work). Optional `skills`: a list of strings, default empty. Extra keys are ignored.

`from_csv_row(row)`: exactly 7 columns: `employee_id, name, email, department, salary, currency, skills`. `skills` is `;`-separated and may be empty. Wrong column count → `ValueError`.

Alternative constructors are classmethods, so `Contractor.from_dict(...)` returns a `Contractor` when `Contractor` subclasses `Employee`.

Equality is the dataclass default: field-by-field.

## Part 6 — `Notifier` ABC and `NotificationDispatcher`

```python
class NotificationError(Exception): ...

class Notifier(ABC):
    @property
    @abstractmethod
    def channel_name(self) -> str: ...
    @abstractmethod
    def send(self, recipient: str, message: str) -> None: ...
    def describe(self) -> str: ...          # f"{channel_name} notifier"

class EmailNotifier(Notifier):
    def __init__(self, transport: Callable[[str, str], None]) -> None: ...   # transport(address, body)

class SMSNotifier(Notifier):
    MAX_LENGTH: ClassVar[int] = 160
    def __init__(self, transport: Callable[[str, str], None]) -> None: ...   # transport(number, text)

class WebhookNotifier(Notifier):
    def __init__(self, endpoint: str, post: Callable[[str, bytes], int]) -> None: ...

class NotificationDispatcher:
    def __init__(self, notifiers: Iterable[Notifier]) -> None: ...
    @property
    def channels(self) -> tuple[str, ...]: ...
    def dispatch(self, message: str, recipients: Mapping[str, str]) -> dict[str, BaseException | None]: ...
```

- `Notifier` cannot be instantiated. A subclass must implement **both** `channel_name` and `send`; missing either → `TypeError` on instantiation.
- Channel names: `EmailNotifier` → `"email"`, `SMSNotifier` → `"sms"`, `WebhookNotifier` → `"webhook"`.
- `message` for every channel: must be a non-blank `str` → else `ValueError`.
- `EmailNotifier.send`: the recipient must match `local@domain` with no whitespace and no extra `@` (else `ValueError`). Then calls `transport(recipient, message)`. Transport exceptions propagate.
- `SMSNotifier.send`: the recipient must be E.164: a `+`, a non-zero first digit, then 7–15 digits in total (else `ValueError`). A message longer than `MAX_LENGTH` (160 characters) → `ValueError`; exactly 160 is fine. Then calls `transport(recipient, message)`.
- `WebhookNotifier`: `endpoint` must start with `https://` (else `ValueError`). `send(recipient, message)` requires a non-blank `recipient`; the body is UTF-8 JSON `{"message": ..., "recipient": ...}` with sorted keys. It calls `post(endpoint, body)`. A status code outside 200–299 raises `NotificationError`.
- `NotificationDispatcher(notifiers)`: each item must be a `Notifier` (else `TypeError`). Duplicate `channel_name`s → `ValueError`. `channels` returns the names in construction order. `len(dispatcher)` is the number of notifiers.
- `dispatch(message, recipients)`: `recipients` maps channel name → recipient. Every key must be a known channel. An unknown key raises `KeyError` **before anything is sent**. Each channel is then sent to; a failure is caught (any `Exception`), recorded, and the loop continues. The result maps each requested channel to `None` (success) or the exception instance. Result keys follow `recipients` order.

## Part 7 — `Version`: immutable semantic version with precedence

```python
class Version:
    def __init__(self, major: int, minor: int = 0, patch: int = 0,
                 prerelease: str = "", build: str = "") -> None: ...
    @classmethod
    def parse(cls, text: str) -> Version: ...

    @property
    def major(self) -> int: ...         # read-only, likewise minor, patch, prerelease, build
    def bump(self, part: str) -> Version: ...        # "major" | "minor" | "patch"

    def __str__(self) -> str: ...       # canonical, e.g. "1.2.3-rc.1+build.5"
    def __repr__(self) -> str: ...      # "Version.parse('1.2.3-rc.1+build.5')"
    # ==, !=, <, <=, >, >=, hash
```

- `major`, `minor`, `patch`: `int` ≥ 0 (`bool` → `TypeError`, negative → `ValueError`). `minor` and `patch` default to `0`.
- `prerelease` and `build`: dot-separated identifiers. Each identifier is 1+ characters from `[0-9A-Za-z-]`. Empty string means "none". A numeric prerelease identifier must not have leading zeros (`"01"` → `ValueError`); numeric build identifiers may have leading zeros.
- `parse(text)`: strict `[v]MAJOR.MINOR.PATCH[-PRERELEASE][+BUILD]`. A leading `v` is allowed. Core numbers must not have leading zeros. Anything else → `ValueError`. Non-`str` → `TypeError`.
- Instances are immutable: assigning any attribute raises `AttributeError`.
- Precedence (semver 2.0): compare `major`, `minor`, `patch` numerically. A release outranks any prerelease with the same core. Prerelease identifiers are compared left to right: numeric identifiers numerically, alphanumeric identifiers lexically (ASCII), numeric identifiers below alphanumeric ones, and a shorter identifier list is lower when it is a prefix.
- **Build metadata is ignored** for `==`, `!=`, ordering, and hashing. `Version.parse("1.0.0+a") == Version.parse("1.0.0+b")`, and they hash equally. `str` and `repr` still include the build.
- Comparing with a non-`Version` uses `NotImplemented`: `Version.parse("1.0.0") == "1.0.0"` is `False`; `<` raises `TypeError`.
- `bump(part)`: returns a new `Version` with the part incremented and the lower parts reset to zero. Prerelease and build are cleared. Unknown part → `ValueError`.
- Instances are hashable and usable as dict keys and set members.

---

## Concepts exercised

- Classes, `__init__`, `__slots__`, read-only properties, validated setters
- Class attributes vs instance attributes, and shadowing pitfalls
- Data-model dunders: `__repr__`, `__str__`, `__eq__`/`__hash__` consistency, ordering, `__add__`/`__radd__` (for `sum()`), `__mul__`/`__rmul__`, `__neg__`, `__bool__`, `__len__`, `__iter__`, `__contains__`, `__getitem__`
- Returning `NotImplemented` correctly
- Decimal arithmetic, quantising, `ROUND_HALF_EVEN`, allocation without lost minor units
- Inheritance, `super()` chaining, polymorphic dispatch through base-class methods
- Alternative constructors via `classmethod`, `staticmethod` helpers
- `@dataclass`: `field(default_factory=...)`, `__post_init__`, default equality
- `ABC`, `@abstractmethod`, abstract properties, injected callables for testability
- Immutable value objects and semantic-version precedence

## Run

```
uv run pytest exercises/07_oop_fundamentals
uv run pytest exercises/07_oop_fundamentals -k part2
uv run pytest exercises/07_oop_fundamentals --solutions     # reference implementation
```

## Stretch (untested)

- Make `Money` generic over currency with a registry of minor-unit rules loaded from a config file.
- Add `__format__` to `Money` supporting `"{:,.2f}"` with locale-free grouping.
- Replace `Version` precedence with a `functools.total_ordering`-based class and compare the two for consistency.
- Add an `Account` event log via a `__init_subclass__` hook that registers account types by name.
- Add an async `Notifier` variant using `asyncio.run` in the dispatcher, keeping the sync API.
