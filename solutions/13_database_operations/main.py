"""Order-management persistence on top of stdlib sqlite3 (DB-API 2.0).

Layers, bottom to top:

* ``connect`` / ``register_types``: connection policy and Decimal/UTC datetime
  adapters so money and instants survive the round trip exactly.
* ``migrate``: versioned, atomic schema migrations tracked in ``user_version``.
* ``unit_of_work``: explicit transactions with SAVEPOINT-based nesting.
* Repositories: parameterized SQL mapped onto frozen dataclasses.
* Module-level functions: bulk import and reporting queries.
"""

from __future__ import annotations

import base64
import itertools
import os
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

__all__ = [
    "CustomerRepository",
    "Customer",
    "CustomerRevenue",
    "Migration",
    "MigrationError",
    "MIGRATIONS",
    "OrderCursor",
    "OrderLine",
    "OrderPage",
    "OrderRepository",
    "Order",
    "OrderWithItems",
    "Product",
    "ProductRepository",
    "ProductSales",
    "RankedCustomer",
    "RunningUnits",
    "UnknownProductError",
    "connect",
    "customer_running_units",
    "import_products",
    "migrate",
    "rank_customers_by_orders",
    "register_types",
    "revenue_by_customer",
    "units_sold_by_product",
    "unit_of_work",
]

_STATUSES = frozenset({"pending", "paid", "shipped", "cancelled"})
_SAVEPOINT = "uow_savepoint"


# ---------------------------------------------------------------------------
# Connection and type policy
# ---------------------------------------------------------------------------


def _adapt_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError(f"non-finite Decimal cannot be stored: {value!r}")
    # Fixed-point text: str() may emit exponent notation such as '1E+2'.
    return format(value, "f")


def _convert_decimal(raw: bytes) -> Decimal:
    return Decimal(raw.decode("ascii"))


def _adapt_datetime(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("naive datetime cannot be stored; use an aware datetime")
    # Fixed-width microseconds keep lexicographic order equal to chronological order.
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _convert_timestamptz(raw: bytes) -> datetime:
    return datetime.fromisoformat(raw.decode("ascii"))


def register_types() -> None:
    """Install Decimal and timezone-aware datetime adapters/converters (idempotent).

    Registration is process-global. Columns declared ``DECIMAL TEXT`` and
    ``TIMESTAMPTZ`` are converted on read when ``PARSE_DECLTYPES`` is enabled.
    """
    sqlite3.register_adapter(Decimal, _adapt_decimal)
    sqlite3.register_adapter(datetime, _adapt_datetime)
    sqlite3.register_converter("DECIMAL", _convert_decimal)
    sqlite3.register_converter("TIMESTAMPTZ", _convert_timestamptz)


def connect(path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Open a connection with the project's policy applied.

    * ``detect_types=PARSE_DECLTYPES`` so declared column types drive converters.
    * ``isolation_level=None``: the driver never issues implicit BEGIN; every
      transaction is explicit (see :func:`unit_of_work`).
    * ``row_factory=sqlite3.Row`` for name-based access.
    * ``PRAGMA foreign_keys=ON``: SQLite leaves FK enforcement off per connection.
    """
    register_types()
    conn = sqlite3.connect(
        os.fspath(path),
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


class MigrationError(Exception):
    """A migration failed (rolled back) or the database is newer than the code."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="create core tables",
        statements=(
            """
            CREATE TABLE customers (
                id         INTEGER PRIMARY KEY,
                email      TEXT NOT NULL UNIQUE COLLATE NOCASE,
                name       TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            CREATE TABLE products (
                sku        TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                unit_price DECIMAL TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE orders (
                id          INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(id),
                placed_at   TIMESTAMPTZ NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'paid', 'shipped', 'cancelled'))
            )
            """,
            """
            CREATE TABLE order_items (
                order_id   INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                line_no    INTEGER NOT NULL,
                sku        TEXT NOT NULL REFERENCES products(sku),
                quantity   INTEGER NOT NULL CHECK (quantity > 0),
                unit_price DECIMAL TEXT NOT NULL,
                PRIMARY KEY (order_id, line_no)
            )
            """,
        ),
    ),
    Migration(
        version=2,
        name="indexes for pagination and joins",
        statements=(
            "CREATE INDEX idx_orders_placed_at ON orders (placed_at, id)",
            "CREATE INDEX idx_orders_customer ON orders (customer_id)",
            "CREATE INDEX idx_order_items_sku ON order_items (sku)",
        ),
    ),
)


def _validate_migrations(migrations: Sequence[Migration]) -> None:
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise ValueError(f"migration versions must be 1..n in order, got {versions}")


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection, migrations: Sequence[Migration] = MIGRATIONS) -> int:
    """Apply pending migrations; return the resulting ``user_version``.

    Each migration runs in its own ``BEGIN IMMEDIATE`` transaction together with
    the ``user_version`` bump, so a failure leaves the schema and version at the
    last good state. ``PRAGMA user_version`` is stored in the database header and
    is therefore transactional.
    """
    _validate_migrations(migrations)
    latest = len(migrations)
    current = _user_version(conn)
    if current > latest:
        raise MigrationError(
            f"database schema version {current} is newer than this code ({latest})"
        )
    for migration in migrations[current:]:
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in migration.statements:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {migration.version}")
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(
                f"migration {migration.version} ({migration.name}) failed: {exc}"
            ) from exc
    return _user_version(conn)


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


@contextmanager
def unit_of_work(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block atomically.

    Outside a transaction this issues ``BEGIN IMMEDIATE`` and commits or rolls
    back. Inside an open transaction it uses a SAVEPOINT, so an inner failure
    undoes only the inner block and the outer transaction stays usable.
    """
    if conn.in_transaction:
        conn.execute(f"SAVEPOINT {_SAVEPOINT}")
        try:
            yield conn
            conn.execute(f"RELEASE {_SAVEPOINT}")
        except BaseException:
            # The engine may already have aborted the transaction; only unwind if still active.
            if conn.in_transaction:
                conn.execute(f"ROLLBACK TO {_SAVEPOINT}")
                conn.execute(f"RELEASE {_SAVEPOINT}")
            raise
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Customer:
    id: int
    email: str
    name: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Product:
    sku: str
    name: str
    unit_price: Decimal


@dataclass(frozen=True, slots=True)
class Order:
    id: int
    customer_id: int
    placed_at: datetime
    status: str


@dataclass(frozen=True, slots=True)
class OrderLine:
    order_id: int
    line_no: int
    sku: str
    quantity: int
    unit_price: Decimal


@dataclass(frozen=True, slots=True)
class OrderWithItems:
    order: Order
    lines: tuple[OrderLine, ...]


@dataclass(frozen=True, slots=True)
class OrderCursor:
    placed_at: datetime
    order_id: int

    def encode(self) -> str:
        raw = f"{self.placed_at.isoformat(timespec='microseconds')}|{self.order_id}"
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, token: str) -> OrderCursor:
        """Parse an opaque token from a client. Untrusted input: raise ValueError on anything malformed."""
        try:
            padded = token + "=" * (-len(token) % 4)
            raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
            iso, sep, id_text = raw.rpartition("|")
            if not sep:
                raise ValueError("missing separator")
            placed_at = datetime.fromisoformat(iso)
            order_id = int(id_text)
        except ValueError as exc:  # binascii.Error, UnicodeError are ValueError subclasses
            raise ValueError(f"malformed cursor token: {token!r}") from exc
        if placed_at.utcoffset() is None:
            raise ValueError("cursor timestamp must be timezone-aware")
        return cls(placed_at=placed_at, order_id=order_id)


@dataclass(frozen=True, slots=True)
class OrderPage:
    items: tuple[Order, ...]
    next_cursor: OrderCursor | None


@dataclass(frozen=True, slots=True)
class CustomerRevenue:
    customer_id: int
    name: str
    order_count: int
    total: Decimal


@dataclass(frozen=True, slots=True)
class RankedCustomer:
    customer_id: int
    name: str
    order_count: int
    rank: int


@dataclass(frozen=True, slots=True)
class RunningUnits:
    order_id: int
    placed_at: datetime
    units: int
    running_units: int


@dataclass(frozen=True, slots=True)
class ProductSales:
    sku: str
    units_sold: int
    order_count: int


class UnknownProductError(LookupError):
    """An order referenced SKUs that are not in the catalogue."""

    def __init__(self, skus: Iterable[str]) -> None:
        self.skus = tuple(sorted(set(skus)))
        super().__init__(f"unknown SKU(s): {', '.join(self.skus)}")


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------


def _validate_price(price: object) -> Decimal:
    if not isinstance(price, Decimal):
        raise TypeError(f"unit_price must be Decimal, not {type(price).__name__}")
    if not price.is_finite() or price < 0:
        raise ValueError(f"unit_price must be finite and >= 0, got {price}")
    return price


def _customer(row: sqlite3.Row) -> Customer:
    return Customer(row["id"], row["email"], row["name"], row["created_at"])


class CustomerRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, email: str, name: str, created_at: datetime) -> Customer:
        cur = self._conn.execute(
            "INSERT INTO customers (email, name, created_at) VALUES (?, ?, ?)",
            (email, name, created_at),
        )
        customer = self.get(int(cur.lastrowid or 0))
        assert customer is not None
        return customer

    def get(self, customer_id: int) -> Customer | None:
        row = self._conn.execute(
            "SELECT id, email, name, created_at FROM customers WHERE id = ?",
            (customer_id,),
        ).fetchone()
        return _customer(row) if row else None

    def get_by_email(self, email: str) -> Customer | None:
        # The column is declared COLLATE NOCASE, so = is case-insensitive.
        row = self._conn.execute(
            "SELECT id, email, name, created_at FROM customers WHERE email = ?",
            (email,),
        ).fetchone()
        return _customer(row) if row else None

    def search_by_name(self, term: str) -> list[Customer]:
        if not term:
            raise ValueError("search term must not be empty")
        # Escape LIKE metacharacters so the term is matched literally.
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self._conn.execute(
            "SELECT id, email, name, created_at FROM customers "
            "WHERE name LIKE ? ESCAPE '\\' ORDER BY name, id",
            (f"%{escaped}%",),
        ).fetchall()
        return [_customer(r) for r in rows]


_UPSERT_PRODUCT = """
INSERT INTO products (sku, name, unit_price) VALUES (?, ?, ?)
ON CONFLICT (sku) DO UPDATE SET
    name = excluded.name,
    unit_price = excluded.unit_price
"""


class ProductRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(self, sku: str, name: str, unit_price: Decimal) -> None:
        self._conn.execute(_UPSERT_PRODUCT, (sku, name, _validate_price(unit_price)))

    def upsert_many(self, rows: Iterable[tuple[str, str, Decimal]]) -> None:
        # Validate everything before executemany so one bad row fails the whole batch cleanly.
        checked = [(sku, name, _validate_price(price)) for sku, name, price in rows]
        self._conn.executemany(_UPSERT_PRODUCT, checked)

    def get(self, sku: str) -> Product | None:
        row = self._conn.execute(
            "SELECT sku, name, unit_price FROM products WHERE sku = ?", (sku,)
        ).fetchone()
        return Product(row["sku"], row["name"], row["unit_price"]) if row else None


def _order(row: sqlite3.Row) -> Order:
    return Order(row["id"], row["customer_id"], row["placed_at"], row["status"])


_ORDER_COLUMNS = "id, customer_id, placed_at, status"


class OrderRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def place(
        self,
        customer_id: int,
        placed_at: datetime,
        lines: Sequence[tuple[str, int]],
    ) -> int:
        """Create an order with price snapshots taken from the catalogue. Returns the order id."""
        if not lines:
            raise ValueError("an order needs at least one line")
        for sku, quantity in lines:
            if quantity < 1:
                raise ValueError(f"quantity for {sku!r} must be >= 1, got {quantity}")

        with unit_of_work(self._conn) as conn:
            skus = sorted({sku for sku, _ in lines})
            placeholders = ", ".join("?" * len(skus))
            prices: dict[str, Decimal] = {
                row["sku"]: row["unit_price"]
                for row in conn.execute(
                    f"SELECT sku, unit_price FROM products WHERE sku IN ({placeholders})",
                    skus,
                )
            }
            missing = [sku for sku in skus if sku not in prices]
            if missing:
                raise UnknownProductError(missing)

            cur = conn.execute(
                "INSERT INTO orders (customer_id, placed_at) VALUES (?, ?)",
                (customer_id, placed_at),
            )
            order_id = int(cur.lastrowid or 0)
            conn.executemany(
                "INSERT INTO order_items (order_id, line_no, sku, quantity, unit_price) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    (order_id, n, sku, quantity, prices[sku])
                    for n, (sku, quantity) in enumerate(lines, start=1)
                ),
            )
        return order_id

    def get(self, order_id: int) -> Order | None:
        row = self._conn.execute(
            f"SELECT {_ORDER_COLUMNS} FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        return _order(row) if row else None

    def set_status(self, order_id: int, status: str) -> None:
        if status not in _STATUSES:
            raise ValueError(f"unknown status {status!r}")
        cur = self._conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
        if cur.rowcount == 0:
            raise LookupError(f"no order with id {order_id}")

    def lines(self, order_id: int) -> list[OrderLine]:
        rows = self._conn.execute(
            "SELECT order_id, line_no, sku, quantity, unit_price FROM order_items "
            "WHERE order_id = ? ORDER BY line_no",
            (order_id,),
        ).fetchall()
        return [OrderLine(*tuple(r)) for r in rows]

    def page_keyset(self, after: OrderCursor | None = None, limit: int = 20) -> OrderPage:
        """Orders ordered by (placed_at, id). Stable under concurrent inserts."""
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if after is None:
            rows = self._conn.execute(
                f"SELECT {_ORDER_COLUMNS} FROM orders ORDER BY placed_at, id LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT {_ORDER_COLUMNS} FROM orders "
                "WHERE (placed_at, id) > (?, ?) ORDER BY placed_at, id LIMIT ?",
                (after.placed_at, after.order_id, limit),
            ).fetchall()
        items = tuple(_order(r) for r in rows)
        next_cursor = None
        if len(items) == limit:
            last = items[-1]
            next_cursor = OrderCursor(placed_at=last.placed_at, order_id=last.id)
        return OrderPage(items=items, next_cursor=next_cursor)

    def page_offset(self, offset: int, limit: int) -> tuple[Order, ...]:
        """Same ordering as keyset pagination, but positional: O(offset) scan, shifts under inserts."""
        if offset < 0 or limit < 1:
            raise ValueError("offset must be >= 0 and limit >= 1")
        rows = self._conn.execute(
            f"SELECT {_ORDER_COLUMNS} FROM orders ORDER BY placed_at, id LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return tuple(_order(r) for r in rows)

    def load_with_items(self, customer_id: int) -> list[OrderWithItems]:
        """All orders for a customer with their lines, in a single statement (no N+1)."""
        rows = self._conn.execute(
            """
            SELECT o.id AS order_id, o.customer_id, o.placed_at, o.status,
                   i.line_no, i.sku, i.quantity, i.unit_price
            FROM orders AS o
            LEFT JOIN order_items AS i ON i.order_id = o.id
            WHERE o.customer_id = ?
            ORDER BY o.placed_at, o.id, i.line_no
            """,
            (customer_id,),
        ).fetchall()

        grouped: dict[int, tuple[Order, list[OrderLine]]] = {}
        for r in rows:
            entry = grouped.get(r["order_id"])
            if entry is None:
                order = Order(r["order_id"], r["customer_id"], r["placed_at"], r["status"])
                entry = grouped[r["order_id"]] = (order, [])
            if r["line_no"] is not None:
                entry[1].append(
                    OrderLine(
                        order_id=r["order_id"],
                        line_no=r["line_no"],
                        sku=r["sku"],
                        quantity=r["quantity"],
                        unit_price=r["unit_price"],
                    )
                )
        return [OrderWithItems(order, tuple(lines)) for order, lines in grouped.values()]


# ---------------------------------------------------------------------------
# Bulk import
# ---------------------------------------------------------------------------


def import_products(
    conn: sqlite3.Connection,
    rows: Iterable[tuple[str, str, Decimal]],
    *,
    batch_size: int = 500,
) -> int:
    """Upsert ``(sku, name, unit_price)`` rows in batches; return the number of rows processed.

    Each batch is its own unit of work: a bad row rolls back only its batch, and
    earlier batches stay committed. ``rows`` may be a one-shot iterator.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    repo = ProductRepository(conn)
    total = 0
    for batch in itertools.batched(rows, batch_size):
        with unit_of_work(conn):
            repo.upsert_many(batch)
        total += len(batch)
    return total


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def units_sold_by_product(conn: sqlite3.Connection) -> list[ProductSales]:
    """Units and distinct orders per SKU, excluding cancelled orders. Pure SQL aggregation."""
    rows = conn.execute(
        """
        SELECT i.sku, SUM(i.quantity) AS units_sold, COUNT(DISTINCT i.order_id) AS order_count
        FROM order_items AS i
        JOIN orders AS o ON o.id = i.order_id
        WHERE o.status <> 'cancelled'
        GROUP BY i.sku
        ORDER BY units_sold DESC, i.sku
        """
    ).fetchall()
    return [ProductSales(r["sku"], r["units_sold"], r["order_count"]) for r in rows]


@dataclass(slots=True)
class _RevenueAcc:
    name: str
    order_ids: set[int] = field(default_factory=set)
    total: Decimal = Decimal("0")


def revenue_by_customer(conn: sqlite3.Connection) -> list[CustomerRevenue]:
    """Exact per-customer revenue, excluding cancelled orders.

    SQLite's SUM() coerces to REAL, so line totals are multiplied and summed as
    Decimal in Python instead.
    """
    rows = conn.execute(
        """
        SELECT c.id AS customer_id, c.name, o.id AS order_id, i.quantity, i.unit_price
        FROM customers AS c
        JOIN orders AS o ON o.customer_id = c.id
        JOIN order_items AS i ON i.order_id = o.id
        WHERE o.status <> 'cancelled'
        """
    ).fetchall()
    accs: dict[int, _RevenueAcc] = {}
    for r in rows:
        acc = accs.setdefault(r["customer_id"], _RevenueAcc(name=r["name"]))
        acc.order_ids.add(r["order_id"])
        acc.total += r["quantity"] * r["unit_price"]
    result = [
        CustomerRevenue(cid, acc.name, len(acc.order_ids), acc.total) for cid, acc in accs.items()
    ]
    result.sort(key=lambda c: (-c.total, c.customer_id))
    return result


def rank_customers_by_orders(conn: sqlite3.Connection) -> list[RankedCustomer]:
    """Every customer ranked by non-cancelled order count (RANK(): ties share a rank, gaps follow)."""
    rows = conn.execute(
        """
        WITH counts AS (
            SELECT c.id AS customer_id, c.name, COUNT(o.id) AS order_count
            FROM customers AS c
            LEFT JOIN orders AS o
                   ON o.customer_id = c.id AND o.status <> 'cancelled'
            GROUP BY c.id, c.name
        )
        SELECT customer_id, name, order_count,
               RANK() OVER (ORDER BY order_count DESC) AS rank
        FROM counts
        ORDER BY rank, name, customer_id
        """
    ).fetchall()
    return [RankedCustomer(r["customer_id"], r["name"], r["order_count"], r["rank"]) for r in rows]


def customer_running_units(conn: sqlite3.Connection, customer_id: int) -> list[RunningUnits]:
    """Per-order units and a running total in (placed_at, order id) order, excluding cancelled orders."""
    rows = conn.execute(
        """
        WITH per_order AS (
            SELECT o.id AS order_id, o.placed_at, SUM(i.quantity) AS units
            FROM orders AS o
            JOIN order_items AS i ON i.order_id = o.id
            WHERE o.customer_id = ? AND o.status <> 'cancelled'
            GROUP BY o.id, o.placed_at
        )
        SELECT order_id, placed_at, units,
               SUM(units) OVER (
                   ORDER BY placed_at, order_id
                   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
               ) AS running_units
        FROM per_order
        ORDER BY placed_at, order_id
        """,
        (customer_id,),
    ).fetchall()
    return [
        RunningUnits(r["order_id"], r["placed_at"], r["units"], r["running_units"])
        for r in rows
    ]
