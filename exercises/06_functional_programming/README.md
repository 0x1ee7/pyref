# 06 · Functional programming in Python

You maintain the back-office services of a small retail platform: a shopping cart
backend, a payments ledger, a config loader for the edge workers, a signup API, and
the nightly sales ETL. The team wants this code written in a functional style: small
pure functions, immutable state, errors as values, and reducers instead of mutation.

Everything goes in `main.py`. Tests use the `main` fixture, so every name below must
be importable from the top level of `main.py`.

## Part 1 · Composition and currying

- `compose(*funcs) -> Callable`: right-to-left composition. `compose(f, g)(x) == f(g(x))`.
  With no arguments it returns the identity function.
- `pipe(value, *funcs) -> Any`: left-to-right threading. `pipe(3, inc, double) == 8`.
  With no functions it returns `value` unchanged.
- `curry(fn, arity) -> Callable`: collects arguments across calls, possibly several per
  call, until `arity` are present, then calls `fn(*args)`. Partial applications are
  independent values: applying one must not affect another. `arity < 1` raises `ValueError`.
- `partial_right(fn, *bound) -> Callable`: appends `bound` after the call-time positional
  arguments: `partial_right(sub, 1)(10) == sub(10, 1) == 9`. Keyword arguments pass through.

Concepts: higher-order functions, closures, right vs left composition, currying,
`functools.partial` (the reference solution uses it in Part 7).

## Part 2 · Immutable nested updates

Redux-style helpers for nested `dict`/`list`/`tuple` data. None of them may mutate
their input. Untouched subtrees must be shared with the original (identity preserved).

- `assoc_in(data, path, value) -> Any`: returns a copy with `value` at `path`.
  Missing intermediate keys are created as `dict`s. `path` is an iterable of keys (`str`)
  or indexes (`int`) for lists/tuples. Lists stay lists and tuples stay tuples.
  - Sequence index out of range -> `IndexError`. Negative indexes follow Python rules.
  - Descending through a scalar (e.g. int or str) -> `TypeError`.
  - An empty `path` -> `ValueError`.
- `update_in(data, path, fn) -> Any`: like `assoc_in`, but the new value is `fn(current)`.
  A missing leaf is passed to `fn` as `None`. Same error rules as `assoc_in`.
- `dissoc(mapping, *keys) -> dict`: a new dict without `keys`. Missing keys are ignored.
  Accepts any `Mapping`, including `MappingProxyType`.

Concepts: structural sharing, copy-on-write, `Mapping` vs `dict`, `MappingProxyType`
as a read-only view, why `dict(...)`/`{**d}` copies are shallow.

## Part 3 · Redux-style shopping cart reducer

Domain types (frozen dataclasses, all with the exact field names shown):

- `LineItem(sku: str, name: str, unit_price_cents: int, quantity: int)`
- `CartState(items: tuple[LineItem, ...] = (), coupon: str | None = None)`
- Actions: `AddItem(sku, name, unit_price_cents, quantity=1)`, `RemoveItem(sku)`,
  `SetQuantity(sku, quantity)`, `ApplyCoupon(code: str | None)`, `ClearCart()`
- `COUPONS`: a read-only `MappingProxyType` mapping coupon code to percent off:
  `{"SAVE10": 10, "HALF": 50}`.

Functions:

- `cart_reducer(state, action) -> CartState`: pure. Returns a new state; the previous state
  and the action are left untouched.
  - `AddItem`: appends a new line at the end, or, if the sku already exists, adds
    `quantity` to the existing line and keeps its position. The existing line keeps its
    name and price. `quantity < 1` or `unit_price_cents < 0` -> `ValueError`.
  - `RemoveItem`: removes the line. An unknown sku is a no-op that returns an equal state.
  - `SetQuantity`: sets the line's quantity. `0` removes the line. Negative quantity
    -> `ValueError`. Unknown sku -> `ValueError`.
  - `ApplyCoupon`: sets the coupon; `None` clears it. Unknown code -> `ValueError`.
  - `ClearCart`: returns `CartState()`, which clears items and coupon.
  - Any other object -> `TypeError`.
- `subtotal_cents(state) -> int`: sum of `unit_price_cents * quantity`.
- `total_cents(state) -> int`: subtotal minus the coupon discount. The discount is
  `subtotal * percent // 100` (floored to whole cents). No coupon means no discount.
- `run_actions(state, actions) -> CartState`: folds an iterable of actions with
  `functools.reduce`. The iterable is not mutated.

Concepts: reducers, frozen dataclasses, `dataclasses.replace`, `match` on class patterns,
`reduce`, action-as-data, why reducers replace mutation.

## Part 4 · Event-sourced bank account

Events (frozen dataclasses):

- `Opened(account_id: str, owner: str)`
- `Deposited(amount_cents: int)`, `Withdrawn(amount_cents: int)`
- `Frozen()`, `Unfrozen()`

State: `Account(account_id: str, owner: str, balance_cents: int = 0, status: "open" | "frozen" = "open")`.

Functions:

- `apply_event(state: Account | None, event) -> Account`: the fold step. `None` means
  the account does not exist yet.
  - `Opened` when `state` is not `None` -> `ValueError` containing "already opened".
  - Any other event when `state` is `None` -> `ValueError` containing "not opened".
  - `Deposited`/`Withdrawn` amount `<= 0` -> `ValueError` containing "positive".
  - `Deposited`/`Withdrawn` on a frozen account -> `ValueError` containing "frozen".
  - `Withdrawn` above the balance -> `ValueError` containing "insufficient funds".
  - `Frozen` is idempotent; `Unfrozen` restores status `"open"`.
- `replay(events) -> Account`: `functools.reduce` over the log, starting from `None`.
  An empty log -> `ValueError` containing "empty".
- `snapshots(events) -> tuple[Account, ...]`: the state after each event, so
  `len(snapshots(events)) == len(events)`. Built with `itertools.accumulate`.
  Its last element equals `replay(events)`.
- `decide_withdraw(account, amount_cents) -> Withdrawn`: a command handler. It checks
  the withdrawal against the current state and returns the event to record. It does not
  change `account`. Invalid withdrawals raise the same errors as `apply_event`.

Concepts: event sourcing, folds, `itertools.accumulate` with `initial=`, command vs event,
the "decide then evolve" split, why the log is the source of truth.

## Part 5 · `Result` types and exception-free config parsing

- `Ok(value)` and `Err(error)`: frozen generic dataclasses with these methods:
  - `is_ok() -> bool`
  - `map(fn)`: transforms the value. `Err` returns itself.
  - `map_err(fn)`: transforms the error. `Ok` returns itself.
  - `and_then(fn)`: `fn(value)` returns a `Result`. `Err` returns itself without calling `fn`.
  - `unwrap_or(default)`: value for `Ok`, `default` for `Err`.
  - `Ok` and `Err` are the two variants of `Result`. Use `Ok | Err` in annotations.
- `try_call(fn, *args, **kwargs) -> Result`: `Ok(fn(...))`, or `Err(exc)` for any `Exception`.
- `collect(results) -> Result`: an iterable of `Result`s becomes `Ok(list_of_values)`.
  It returns the first `Err` it meets and stops consuming the iterable there.
- `parse_port(raw: str) -> Result[int, str]`: strips whitespace. Non-integer ->
  `Err("port must be an integer, got ...")`. Outside `1..65535` ->
  `Err("port must be between 1 and 65535")`.
- `parse_ports(raw: str) -> Result[list[int], str]`: comma-separated ports, parsed with `collect`.
- `parse_bool(raw: str) -> Result[bool, str]`: case-insensitive. `true/yes/1/on` -> `True`;
  `false/no/0/off` -> `False`; anything else -> `Err("expected boolean, got '...'")`.
- `Config(host: str, port: int, debug: bool = False, workers: int = 4)`
- `parse_config(text: str) -> Result[Config, str]`: never raises.
  - Lines are `key = value`. Blank lines and lines starting with `#` are ignored.
  - A non-blank line without `=` -> `Err("line N: expected 'key = value'")`.
  - A repeated key -> `Err("line N: duplicate key 'k'")`.
  - Keys other than `host`, `port`, `debug`, `workers` -> `Err("unknown key 'k'")`.
    When several are present, the lexically first is reported.
  - Missing `host` or `port` -> `Err("missing required key 'host'")`. The host check runs first.
  - Empty `host` -> `Err("host must not be empty")`.
  - `port` is parsed with `parse_port`. `debug` (default `False`) uses `parse_bool`.
    `workers` (default 4) must be an integer in `1..64`, else `Err("workers must be between 1 and 64")`.

Concepts: Rust-style Result/Either, error values vs exceptions, `and_then` chains,
`functools.reduce`-style short-circuiting, why `try/except` belongs at the boundary.

## Part 6 · Validator combinators

A validator is a callable `value -> list[str]`: an empty list means valid. Every
combinator collects error messages; none raise on bad input.

Primitives (each one judges only its own domain and returns `[]` for values outside it.
Use `is_type` for type errors):

- `is_type(tp)`: `isinstance` check; `bool` is rejected where `int` is expected.
  Error: `"expected int, got bool"`.
- `min_length(n)`: strings only. Error: `"length must be at least n"`.
- `matches(pattern)`: strings only, `re.fullmatch`. Error: `"must match pattern '...'"` (repr of pattern).
- `in_range(lo, hi)`: int or float, not bool, inclusive. Error: `"must be between lo and hi"`.

Combinators:

- `all_of(*validators)`: runs all of them, concatenates every error in order.
- `any_of(*validators)`: passes on the first validator that returns no errors and does not
  call later ones. If none pass, returns all errors concatenated. No validators -> `ValueError`.
- `field(name, validator)`: applies `validator` to `record[name]`, prefixing errors with
  `"name: "`. A missing key -> `["name: missing"]`. A non-`Mapping` record -> `[]`.
- `optional(validator)`: `None` is valid; otherwise delegates.
- `each(validator)`: applies `validator` to every element of a `list`/`tuple`, prefixing
  errors with `"[i] "`. Other values -> `[]`.

Application: `validate_signup(payload) -> list[str]` is built from these:

- must be a `Mapping` (`"expected Mapping, got list"`)
- `username`: `str`, at least 3 characters, matches `[a-z0-9_]+`
- `age`: `int`, between 13 and 120 inclusive
- `email`: `str`, matches `[^@\s]+@[^@\s]+\.[^@\s]+`
- `tags`: optional list of non-empty strings

Errors appear in the order described above. Example:
`{"username": "A", "age": 7, "email": "bad", "tags": ["ok", ""]}` produces five
messages. The first is `"username: length must be at least 3"` and the last is
`"tags: [1] length must be at least 1"`.

Concepts: combinator design, function composition over predicates, error accumulation
vs fail-fast, short-circuit `any`, `re.fullmatch`, the bool-is-int gotcha.

## Part 7 · ETL pipeline and sorting/grouping reports

Input rows are `Mapping[str, str]` with keys `region`, `product`, `units`, `unit_price`
(dollars, e.g. `"12.50"`), `sold_on` (ISO date). Output is built from these types:

- `Sale(region: str, product: str, units: int, unit_price_cents: int, sold_on: date)`

Functions:

- `parse_cents(raw: str) -> int`: exact money parsing with `decimal.Decimal`, no floats.
  `"12.5"` -> `1250`, `"12"` -> `1200`, `"0.01"` -> `1`, `"0"` -> `0`. More than two decimals,
  negative, non-finite, or non-numeric -> `ValueError`.
- `parse_sale(row) -> Sale`: strips fields, lowercases the region, parses `units` as an int
  (negative -> `ValueError`) and `sold_on` with `date.fromisoformat`.
- `clean_sales(rows) -> list[Sale]`: an ETL step built from `filter` and a comprehension.
  It drops rows whose region is blank after stripping and rows whose `units` is `0`, then
  parses the rest in order. It accepts any iterable (including generators). It does not
  mutate the row mappings.
- `revenue_cents(sale) -> int`: `units * unit_price_cents`.
- `totals_by(sales, key) -> dict[str, int]`: sums revenue per `key(sale)`. The returned
  dict's keys are in ascending order.
- `ranked(totals) -> list[tuple[str, int]]`: sorted by total descending, ties broken by name
  ascending. Use `operator.itemgetter` and stable sorts.
- `distinct_regions(sales) -> tuple[str, ...]`: sorted unique regions, uppercased
  (`map` + `methodcaller("upper")`).
- `format_cents(cents, *, currency="$") -> str`: `1234567` -> `"$12,345.67"`. Negative -> `ValueError`.
- `build_report(rows, *, currency="$") -> list[str]`: a `pipe` of `clean_sales`, `totals_by`
  grouped by region (`attrgetter("region")`), `ranked`, and formatting with
  `functools.partial(format_cents, currency=...)`. Lines look like `"us: $200.00"`.

Concepts: ETL as composed steps, `filter`/`map` vs comprehensions, `attrgetter`/`itemgetter`/
`methodcaller`, multi-key sorts via stability, `Decimal` for money, `partial` for config.

## Running

```
uv run pytest exercises/06_functional_programming              # all parts
uv run pytest exercises/06_functional_programming -k part3     # one part
uv run pytest exercises/06_functional_programming --solutions  # reference answer key
```

## Stretch (untested)

- Add `lift(fn)` that turns a plain function into one that takes and returns `Result`.
- Rewrite `replay` with a `Result` accumulator so an invalid log reports which event failed.
- Use `hypothesis` to prove `total_cents` never exceeds `subtotal_cents` for any coupon.
