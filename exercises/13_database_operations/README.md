# 13 · Database operations

An order-management backend for a small online shop, persisted with the stdlib
`sqlite3` module. You own the persistence layer: connection policy, schema
migrations, transactions, repositories, bulk import, pagination, and reporting
queries. Everything lives in `main.py`; the tests in `test_database_operations.py`
are the executable spec.

Use only the standard library. SQLite is 3.x with window functions and row-value
comparisons available. The code must run on Python 3.13+.

## Ground rules

- Money is `decimal.Decimal` end to end. Never store or return a `float` for a price.
- Timestamps are timezone-aware `datetime`s. Naive datetimes are rejected.
- `sqlite3` adapters and converters are process-global. Registration must be
  idempotent, and tests restore the registry afterwards.
- Every test opens its own database under `tmp_path` (or `:memory:`) and closes it.

## Part 1: connection policy and type round trips

```python
def register_types() -> None: ...
def connect(path: str | os.PathLike[str]) -> sqlite3.Connection: ...
```

- `register_types()` installs:
  - a `Decimal` adapter that writes fixed-point text (`format(v, "f")`, never
    `1E+2`); non-finite values raise `ValueError`;
  - a `Decimal` converter for columns declared `DECIMAL`;
  - an aware-`datetime` adapter that stores UTC text with fixed-width microseconds
    (so lexicographic order equals chronological order); naive values raise `ValueError`;
  - a converter for columns declared `TIMESTAMPTZ` that returns aware datetimes in UTC.
  Calling it twice must be harmless.
- `connect(path)` returns a connection with:
  - `detect_types=sqlite3.PARSE_DECLTYPES`;
  - `isolation_level=None` (you control transactions explicitly);
  - `row_factory=sqlite3.Row`;
  - `PRAGMA foreign_keys = ON`;
  - type registration already applied.

## Part 2: versioned migrations

```python
@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]

MIGRATIONS: tuple[Migration, ...]   # v1: schema; v2: indexes

class MigrationError(Exception): ...

def migrate(conn: sqlite3.Connection, migrations: Sequence[Migration] = MIGRATIONS) -> int: ...
```

- `migrations` must be numbered `1..n` in order; otherwise `ValueError`.
- `migrate` applies every migration with `version > PRAGMA user_version`. Each one runs
  in its own `BEGIN IMMEDIATE` transaction together with the `user_version` bump, so a
  failure leaves the schema and the version consistent.
- A failing migration rolls back, raises `MigrationError` with the driver error chained
  as `__cause__`, and leaves no transaction open.
- A database whose `user_version` is greater than the newest known migration raises
  `MigrationError`.
- Returns the resulting `user_version`. Re-running is a no-op.

## Part 3: unit of work

```python
@contextmanager
def unit_of_work(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]: ...
```

- Outside a transaction: `BEGIN IMMEDIATE`, then `COMMIT` on success or `ROLLBACK` if
  the body raises. The exception propagates unchanged.
- Inside an existing transaction: use a `SAVEPOINT` (`uow_savepoint`), and roll back to
  and release it on failure. The outer transaction must survive an inner failure.
- A released savepoint stays part of the outer transaction; an outer failure discards it.
- Never leave `conn.in_transaction` true when the context exits.

## Part 4: repositories

```python
@dataclass(frozen=True, slots=True)
class Customer:  id: int; email: str; name: str; created_at: datetime
@dataclass(frozen=True, slots=True)
class Product:   sku: str; name: str; unit_price: Decimal
@dataclass(frozen=True, slots=True)
class Order:     id: int; customer_id: int; placed_at: datetime; status: str
@dataclass(frozen=True, slots=True)
class OrderLine: order_id: int; line_no: int; sku: str; quantity: int; unit_price: Decimal

class UnknownProductError(LookupError):
    skus: tuple[str, ...]           # sorted, de-duplicated

class CustomerRepository:
    def __init__(self, conn: sqlite3.Connection) -> None: ...
    def add(self, email: str, name: str, created_at: datetime) -> Customer: ...
    def get(self, customer_id: int) -> Customer | None: ...
    def get_by_email(self, email: str) -> Customer | None: ...   # case-insensitive
    def search_by_name(self, term: str) -> list[Customer]: ...   # literal substring, case-insensitive

class ProductRepository:
    def __init__(self, conn: sqlite3.Connection) -> None: ...
    def upsert(self, sku: str, name: str, unit_price: Decimal) -> None: ...
    def upsert_many(self, rows: Iterable[tuple[str, str, Decimal]]) -> None: ...
    def get(self, sku: str) -> Product | None: ...

class OrderRepository:
    def __init__(self, conn: sqlite3.Connection) -> None: ...
    def place(self, customer_id: int, placed_at: datetime,
              lines: Sequence[tuple[str, int]]) -> int: ...
    def get(self, order_id: int) -> Order | None: ...
    def set_status(self, order_id: int, status: str) -> None: ...
    def lines(self, order_id: int) -> list[OrderLine]: ...
```

- `add` returns the persisted row, including its generated id. Duplicate email (any case)
  raises `sqlite3.IntegrityError`.
- `search_by_name`: an empty term raises `ValueError`. `%` and `_` match literally, not as
  wildcards. All SQL is parameterised.
- `upsert` validates the price: a non-`Decimal` raises `TypeError`; negative or non-finite
  raises `ValueError`. `upsert_many` validates every row before writing, so one bad row
  writes nothing.
- `place`:
  - `lines` must be non-empty and each quantity `>= 1`, else `ValueError`;
  - the unit price is snapshotted from the catalogue at order time, so later price changes
    do not alter the order;
  - line numbers start at 1 in input order;
  - unknown SKUs raise `UnknownProductError` (carries the missing SKUs in its message);
  - an unknown customer raises `sqlite3.IntegrityError` (foreign keys are on);
  - any failure leaves no order header and no lines behind.
- `set_status`: a status outside `pending, paid, shipped, cancelled` raises `ValueError`; an
  unknown order id raises `LookupError`.
- `lines` returns the order's lines ordered by `line_no`.

## Part 5: bulk import

```python
def import_products(conn: sqlite3.Connection,
                    rows: Iterable[tuple[str, str, Decimal]],
                    *, batch_size: int = 500) -> int: ...
```

- Accepts a one-shot iterator. Writes in batches of `batch_size` (`ValueError` if `< 1`),
  each batch in its own transaction. Returns the number of rows processed.
- Rows are upserts, so re-importing updates existing SKUs without duplicates.
- A bad row (for example a negative price) raises `ValueError`. Batches already committed
  stay; the batch containing the bad row is rolled back in full, and later rows are not
  processed.
- Memory use must not grow with the input size.

## Part 6: pagination

```python
@dataclass(frozen=True, slots=True)
class OrderCursor:
    placed_at: datetime
    order_id: int
    def encode(self) -> str: ...          # URL-safe, unpadded token
    @classmethod
    def decode(cls, token: str) -> OrderCursor: ...   # ValueError on anything malformed

@dataclass(frozen=True, slots=True)
class OrderPage:
    items: tuple[Order, ...]
    next_cursor: OrderCursor | None

class OrderRepository:
    def page_keyset(self, after: OrderCursor | None = None, limit: int = 20) -> OrderPage: ...
    def page_offset(self, offset: int, limit: int) -> tuple[Order, ...]: ...
```

- Ordering for both is `(placed_at, id)` ascending.
- `page_keyset` returns the page after `after`. `next_cursor` is set when the page is full
  (`len(items) == limit`), even if no rows follow; the next call then returns an empty page
  with `next_cursor=None`. `limit < 1` raises `ValueError`.
- `page_offset` is the positional alternative. `offset < 0` or `limit < 1` raises `ValueError`.
- `decode` rejects empty input, bad base64, a missing separator, a non-integer id, and a
  timestamp without a timezone, all with `ValueError`.
- Pages from `page_keyset` must not overlap, even when older rows are inserted between
  calls. `page_offset` is expected to shift under the same insert.

## Part 7: loading aggregates without N+1

```python
@dataclass(frozen=True, slots=True)
class OrderWithItems:
    order: Order
    lines: tuple[OrderLine, ...]

class OrderRepository:
    def load_with_items(self, customer_id: int) -> list[OrderWithItems]: ...
```

- Returns the customer's orders (cancelled included) in `(placed_at, id)` order, each with
  its lines ordered by `line_no`. A customer with no orders gets `[]`.
- Uses a bounded number of statements regardless of the order count. A single `LEFT JOIN`
  is one acceptable shape; the test counts statements through `set_trace_callback`.

## Part 8: reporting

```python
@dataclass(frozen=True, slots=True)
class ProductSales:       sku: str; units_sold: int; order_count: int
@dataclass(frozen=True, slots=True)
class CustomerRevenue:    customer_id: int; name: str; order_count: int; total: Decimal
@dataclass(frozen=True, slots=True)
class RankedCustomer:     customer_id: int; name: str; order_count: int; rank: int
@dataclass(frozen=True, slots=True)
class RunningUnits:       order_id: int; placed_at: datetime; units: int; running_units: int

def units_sold_by_product(conn: sqlite3.Connection) -> list[ProductSales]: ...
def revenue_by_customer(conn: sqlite3.Connection) -> list[CustomerRevenue]: ...
def rank_customers_by_orders(conn: sqlite3.Connection) -> list[RankedCustomer]: ...
def customer_running_units(conn: sqlite3.Connection, customer_id: int) -> list[RunningUnits]: ...
```

- Every report excludes orders with status `cancelled`.
- `units_sold_by_product`: sorted by `units_sold` descending, then `sku` ascending. Only SKUs
  with at least one non-cancelled order appear.
- `revenue_by_customer`: `total` is the exact `Decimal` sum of `quantity * unit_price` over
  the customer's non-cancelled lines. Do not let SQLite's `SUM()` produce a float. Sorted by
  `customer_id`. Customers with no non-cancelled orders are omitted.
- `rank_customers_by_orders`: every customer, including those with zero orders, ranked by
  non-cancelled order count descending. Use SQL `RANK()`: ties share a rank and the next rank
  skips. Sorted by rank, then name.
- `customer_running_units`: one row per non-cancelled order of the customer, ordered by
  `(placed_at, order_id)`, with the order's total units and a running total.

## Concepts exercised

- `sqlite3` connection policy: `PARSE_DECLTYPES`, `isolation_level=None`, `row_factory`,
  foreign-key enforcement
- Custom adapters and converters, and the process-global registry
- Exact decimal storage and why SQLite `SUM()` is not safe for money
- Versioned schema migrations with `PRAGMA user_version`
- `BEGIN IMMEDIATE`, `COMMIT`/`ROLLBACK`, and nested `SAVEPOINT`s
- Parameterised SQL, `LIKE` escaping, and injection-proof queries
- Upserts with `ON CONFLICT`, and batch imports with bounded memory
- Keyset versus `OFFSET` pagination, opaque cursor tokens, and untrusted input
- N+1 avoidance with joins and `set_trace_callback` as a statement counter
- Window functions: `RANK()` and running `SUM() OVER`

## Run

```
uv run pytest exercises/13_database_operations
uv run pytest exercises/13_database_operations -k part2
uv run pytest exercises/13_database_operations --solutions
```

## Stretch (untested)

- Add a `customers.deleted_at` soft-delete column with a migration, and make every
  repository query filter it. Think about what a partial unique index on `email` needs.
- Replace `page_offset` with a `WITH` query that uses a covering index, and check the plan
  with `EXPLAIN QUERY PLAN`.
- Make `place` retry on `sqlite3.OperationalError: database is locked` with bounded backoff,
  and decide whether that retry belongs in `unit_of_work` or in the caller.
- Stream `revenue_by_customer` through `Cursor` iteration instead of `fetchall()`, and
  measure the difference on 1M rows.
