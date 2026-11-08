"""Tests for the order-management persistence layer built on sqlite3."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

T0 = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
PLUS_TWO = timezone(timedelta(hours=2))


@pytest.fixture(autouse=True)
def _isolate_sqlite_type_registry() -> Iterator[None]:
    """Adapters and converters are process-global; restore them after every test."""
    adapters = dict(sqlite3.adapters)
    converters = dict(sqlite3.converters)
    try:
        yield
    finally:
        sqlite3.adapters.clear()
        sqlite3.adapters.update(adapters)
        sqlite3.converters.clear()
        sqlite3.converters.update(converters)


@contextmanager
def open_db(main: ModuleType, path: Path) -> Iterator[sqlite3.Connection]:
    """Connect, migrate to head, and always close (Windows holds file locks until closed)."""
    conn = main.connect(path)
    try:
        main.migrate(conn)
        yield conn
    finally:
        conn.close()


@contextmanager
def count_statements(conn: sqlite3.Connection) -> Iterator[list[str]]:
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        yield statements
    finally:
        conn.set_trace_callback(None)


def seed_shop(conn: sqlite3.Connection) -> None:
    """Deterministic fixture data written with raw SQL so repositories are not exercised here.

    Customers: Ada (1), Grace (2), Alan (3), Edsger (4, no orders).
    Non-cancelled revenue: Ada 45.28 over 2 orders, Grace 20.00, Alan 19.99.
    """
    conn.executemany(
        "INSERT INTO customers (id, email, name, created_at) VALUES (?, ?, ?, ?)",
        [
            (1, "ada@example.com", "Ada Lovelace", datetime(2025, 12, 1, 9, tzinfo=UTC)),
            (2, "grace@example.com", "Grace Hopper", datetime(2025, 12, 2, 9, tzinfo=UTC)),
            (3, "alan@example.com", "Alan Turing", datetime(2025, 12, 3, 9, tzinfo=UTC)),
            (4, "edsger@example.com", "Edsger Dijkstra", datetime(2025, 12, 4, 9, tzinfo=UTC)),
        ],
    )
    conn.executemany(
        "INSERT INTO products (sku, name, unit_price) VALUES (?, ?, ?)",
        [
            ("WIDGET", "Widget", Decimal("19.99")),
            ("GADGET", "Gadget", Decimal("5.00")),
            ("GIZMO", "Gizmo 100% Pure", Decimal("0.10")),
        ],
    )
    orders = [
        (1, 1, datetime(2026, 1, 5, 10, tzinfo=UTC), "pending"),
        (2, 1, datetime(2026, 1, 6, 10, tzinfo=UTC), "paid"),
        (3, 2, datetime(2026, 1, 6, 12, tzinfo=UTC), "cancelled"),
        (4, 2, datetime(2026, 1, 7, 9, tzinfo=UTC), "shipped"),
        (5, 3, datetime(2026, 1, 8, 8, tzinfo=UTC), "paid"),
    ]
    conn.executemany(
        "INSERT INTO orders (id, customer_id, placed_at, status) VALUES (?, ?, ?, ?)", orders
    )
    items = [
        (1, 1, "WIDGET", 2, Decimal("19.99")),
        (1, 2, "GADGET", 1, Decimal("5.00")),
        (2, 1, "GIZMO", 3, Decimal("0.10")),
        (3, 1, "WIDGET", 10, Decimal("19.99")),
        (4, 1, "GADGET", 4, Decimal("5.00")),
        (5, 1, "WIDGET", 1, Decimal("19.99")),
    ]
    conn.executemany(
        "INSERT INTO order_items (order_id, line_no, sku, quantity, unit_price) "
        "VALUES (?, ?, ?, ?, ?)",
        items,
    )


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")
    return {row[0] for row in rows}


def row_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


# ---------------------------------------------------------------------------
# Part 1: connection policy and type adapters/converters
# ---------------------------------------------------------------------------


class TestPart1Connection:
    def test_foreign_keys_are_enforced(self, main: ModuleType, tmp_path: Path) -> None:
        with closing(main.connect(tmp_path / "fk.db")) as conn:
            main.migrate(conn)
            pragma = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            assert pragma == 1, "connect() must enable PRAGMA foreign_keys"
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO orders (customer_id, placed_at) VALUES (999, ?)", (T0,)
                )

    def test_rows_support_access_by_name(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "rows.db") as conn:
            assert conn.row_factory is sqlite3.Row, "row_factory must be sqlite3.Row"
            seed_shop(conn)
            row = conn.execute("SELECT name, email FROM customers WHERE id = 1").fetchone()
            assert row["email"] == "ada@example.com", "rows must be addressable by column name"

    def test_manual_transaction_control_autocommits_plain_statements(
        self, main: ModuleType, tmp_path: Path
    ) -> None:
        path = tmp_path / "autocommit.db"
        with open_db(main, path) as conn:
            assert conn.isolation_level is None, "isolation_level must be None (explicit transactions)"
            conn.execute(
                "INSERT INTO products (sku, name, unit_price) VALUES ('X', 'x', '1.00')"
            )
            assert not conn.in_transaction, "a plain INSERT must not open an implicit transaction"
            with closing(sqlite3.connect(path)) as other:
                visible = other.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            assert visible == 1, "a plain INSERT must be committed immediately"

    def test_decimal_round_trips_exactly(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "decimal.db") as conn:
            price = Decimal("0.10")
            conn.execute(
                "INSERT INTO products (sku, name, unit_price) VALUES ('P', 'p', ?)", (price,)
            )
            stored = conn.execute("SELECT unit_price FROM products WHERE sku = 'P'").fetchone()[0]
            assert isinstance(stored, Decimal), "DECIMAL columns must convert back to Decimal"
            assert stored == Decimal("0.10")
            total = stored + Decimal("0.20")
            assert total == Decimal("0.30"), "decimal arithmetic must be exact after the round trip"

    def test_aware_datetime_is_stored_as_utc_and_returned_aware(
        self, main: ModuleType, tmp_path: Path
    ) -> None:
        local = datetime(2026, 3, 1, 12, 30, 15, 123456, tzinfo=PLUS_TWO)
        with open_db(main, tmp_path / "tz.db") as conn:
            conn.execute(
                "INSERT INTO customers (email, name, created_at) VALUES ('t@x.io', 'T', ?)",
                (local,),
            )
            raw = conn.execute("SELECT created_at FROM customers").fetchone()[0]
            assert isinstance(raw, datetime), "TIMESTAMPTZ columns must convert back to datetime"
            assert raw == local, "the instant must be preserved"
            assert raw.utcoffset() == timedelta(0), "values are stored and returned in UTC"

    def test_naive_datetime_is_rejected(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "naive.db") as conn:
            with pytest.raises(ValueError):
                conn.execute(
                    "INSERT INTO customers (email, name, created_at) VALUES ('n@x.io', 'N', ?)",
                    (datetime(2026, 1, 1, 12, 0),),
                )

    def test_registration_is_idempotent(self, main: ModuleType, tmp_path: Path) -> None:
        main.register_types()
        main.register_types()
        with open_db(main, tmp_path / "idem.db") as conn:
            conn.execute("INSERT INTO products (sku, name, unit_price) VALUES ('Q', 'q', ?)", (Decimal("2.50"),))
            assert conn.execute("SELECT unit_price FROM products").fetchone()[0] == Decimal("2.50")


# ---------------------------------------------------------------------------
# Part 2: versioned migrations
# ---------------------------------------------------------------------------


class TestPart2Migrations:
    def test_fresh_database_reaches_head_version(self, main: ModuleType, tmp_path: Path) -> None:
        with closing(main.connect(tmp_path / "fresh.db")) as conn:
            version = main.migrate(conn)
            assert version == len(main.MIGRATIONS), "migrate() must return the new user_version"
            assert conn.execute("PRAGMA user_version").fetchone()[0] == len(main.MIGRATIONS)
            names = table_names(conn)
            for table in ("customers", "products", "orders", "order_items"):
                assert table in names, f"table {table!r} missing after migration"

    def test_migrate_is_idempotent(self, main: ModuleType, tmp_path: Path) -> None:
        log_step = main.Migration(
            version=1,
            name="create log",
            statements=(
                "CREATE TABLE applied (note TEXT)",
                "INSERT INTO applied VALUES ('ran')",
            ),
        )
        with closing(main.connect(tmp_path / "idem.db")) as conn:
            assert main.migrate(conn, [log_step]) == 1
            assert main.migrate(conn, [log_step]) == 1, "second run must be a no-op"
            assert row_count(conn, "applied") == 1, "a migration must not run twice"

    def test_failed_migration_rolls_back_and_keeps_previous_version(
        self, main: ModuleType, tmp_path: Path
    ) -> None:
        good = main.Migration(1, "t1", ("CREATE TABLE t1 (x INTEGER)",))
        broken = main.Migration(
            2,
            "broken",
            ("CREATE TABLE t2 (x INTEGER)", "INSERT INTO no_such_table VALUES (1)"),
        )
        with closing(main.connect(tmp_path / "rollback.db")) as conn:
            with pytest.raises(main.MigrationError) as info:
                main.migrate(conn, [good, broken])
            assert isinstance(info.value.__cause__, sqlite3.Error), "the driver error must be chained"
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 1, (
                "user_version must stay at the last successful migration"
            )
            names = table_names(conn)
            assert "t1" in names, "the successful migration must remain applied"
            assert "t2" not in names, "the failed migration's DDL must be rolled back"
            assert not conn.in_transaction, "no transaction may be left open after failure"

    def test_database_newer_than_code_is_rejected(self, main: ModuleType, tmp_path: Path) -> None:
        with closing(main.connect(tmp_path / "newer.db")) as conn:
            conn.execute("PRAGMA user_version = 99")
            with pytest.raises(main.MigrationError):
                main.migrate(conn)

    def test_upgrade_from_v1_adds_indexes(self, main: ModuleType, tmp_path: Path) -> None:
        with closing(main.connect(tmp_path / "upgrade.db")) as conn:
            main.migrate(conn, main.MIGRATIONS[:1])
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
            assert main.migrate(conn) == len(main.MIGRATIONS)
            names = table_names(conn)
            for index in ("idx_orders_placed_at", "idx_orders_customer", "idx_order_items_sku"):
                assert index in names, f"index {index!r} missing after upgrade to head"

    @pytest.mark.parametrize("versions", [(1, 3), (2,), (1, 1)])
    def test_non_contiguous_migration_versions_are_rejected(
        self, main: ModuleType, tmp_path: Path, versions: tuple[int, ...]
    ) -> None:
        steps = [main.Migration(v, f"m{v}", ("SELECT 1",)) for v in versions]
        with closing(main.connect(tmp_path / "seq.db")) as conn:
            with pytest.raises(ValueError):
                main.migrate(conn, steps)


# ---------------------------------------------------------------------------
# Part 3: unit of work and savepoints
# ---------------------------------------------------------------------------


class TestPart3UnitOfWork:
    def test_commits_on_success(self, main: ModuleType, tmp_path: Path) -> None:
        path = tmp_path / "commit.db"
        with open_db(main, path) as conn:
            with main.unit_of_work(conn):
                conn.execute("INSERT INTO products VALUES ('A', 'a', '1.00')")
            assert not conn.in_transaction
            with closing(sqlite3.connect(path)) as other:
                assert other.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1

    def test_rolls_back_and_reraises_on_exception(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "rb.db") as conn:
            with pytest.raises(RuntimeError, match="boom"):
                with main.unit_of_work(conn):
                    conn.execute("INSERT INTO products VALUES ('A', 'a', '1.00')")
                    raise RuntimeError("boom")
            assert not conn.in_transaction, "the transaction must be closed after rollback"
            assert row_count(conn, "products") == 0, "the body's writes must be undone"

    def test_nested_inner_failure_rolls_back_only_inner_block(
        self, main: ModuleType, tmp_path: Path
    ) -> None:
        with open_db(main, tmp_path / "nested.db") as conn:
            with main.unit_of_work(conn):
                conn.execute("INSERT INTO products VALUES ('OUTER', 'o', '1.00')")
                with pytest.raises(ValueError):
                    with main.unit_of_work(conn):
                        conn.execute("INSERT INTO products VALUES ('INNER', 'i', '1.00')")
                        raise ValueError("inner failure")
                assert conn.in_transaction, "outer transaction must survive an inner rollback"
                conn.execute("INSERT INTO products VALUES ('AFTER', 'a', '1.00')")
            skus = {r[0] for r in conn.execute("SELECT sku FROM products")}
            assert skus == {"OUTER", "AFTER"}, f"only the inner block should be undone, got {skus}"

    def test_outer_failure_discards_committed_inner_block(
        self, main: ModuleType, tmp_path: Path
    ) -> None:
        with open_db(main, tmp_path / "outer.db") as conn:
            with pytest.raises(RuntimeError):
                with main.unit_of_work(conn):
                    with main.unit_of_work(conn):
                        conn.execute("INSERT INTO products VALUES ('INNER', 'i', '1.00')")
                    raise RuntimeError("outer failure")
            assert row_count(conn, "products") == 0, (
                "a released savepoint is still part of the outer transaction"
            )


# ---------------------------------------------------------------------------
# Part 4: repositories, parameterized SQL, upserts, order placement
# ---------------------------------------------------------------------------


class TestPart4Repositories:
    def test_add_customer_returns_persisted_row(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "cust.db") as conn:
            customer = main.CustomerRepository(conn).add("new@example.com", "New Person", T0)
            assert isinstance(customer.id, int) and customer.id > 0
            assert customer.created_at == T0
            assert customer.created_at.utcoffset() == timedelta(0)
            assert main.CustomerRepository(conn).get(customer.id) == customer

    def test_email_lookup_is_case_insensitive_and_duplicates_rejected(
        self, main: ModuleType, tmp_path: Path
    ) -> None:
        with open_db(main, tmp_path / "email.db") as conn:
            seed_shop(conn)
            repo = main.CustomerRepository(conn)
            found = repo.get_by_email("ADA@Example.COM")
            assert found is not None and found.id == 1, "email lookup must be case-insensitive"
            with pytest.raises(sqlite3.IntegrityError):
                repo.add("grace@EXAMPLE.com", "Impostor", T0)

    def test_name_search_matches_literally_including_wildcards(
        self, main: ModuleType, tmp_path: Path
    ) -> None:
        with open_db(main, tmp_path / "like.db") as conn:
            seed_shop(conn)
            repo = main.CustomerRepository(conn)
            assert [c.id for c in repo.search_by_name("lovelace")] == [1]
            assert repo.search_by_name("%") == [], "% must not act as a wildcard"
            assert repo.search_by_name("_") == [], "_ must not act as a wildcard"
            with pytest.raises(ValueError):
                repo.search_by_name("")

    def test_sql_injection_attempts_are_inert(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "inject.db") as conn:
            seed_shop(conn)
            repo = main.CustomerRepository(conn)
            assert repo.search_by_name("x' OR '1'='1") == []
            assert repo.get_by_email("a' OR 1=1 --") is None
            assert repo.search_by_name("'; DROP TABLE customers; --") == []
            assert row_count(conn, "customers") == 4, "injected SQL must never execute"

    def test_product_upsert_inserts_then_updates(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "upsert.db") as conn:
            repo = main.ProductRepository(conn)
            repo.upsert("SKU-1", "Old name", Decimal("1.00"))
            repo.upsert("SKU-1", "New name", Decimal("1.25"))
            product = repo.get("SKU-1")
            assert product == main.Product("SKU-1", "New name", Decimal("1.25"))
            assert row_count(conn, "products") == 1, "upsert must not create duplicate rows"

    def test_product_price_validation(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "price.db") as conn:
            repo = main.ProductRepository(conn)
            with pytest.raises(TypeError):
                repo.upsert("F", "float price", 1.1)  # type: ignore[arg-type]
            with pytest.raises(ValueError):
                repo.upsert("N", "negative", Decimal("-0.01"))
            assert row_count(conn, "products") == 0

    def test_place_order_snapshots_prices(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "snap.db") as conn:
            seed_shop(conn)
            products = main.ProductRepository(conn)
            orders = main.OrderRepository(conn)
            order_id = orders.place(1, T0, [("WIDGET", 2), ("GADGET", 1)])
            products.upsert("WIDGET", "Widget", Decimal("99.00"))
            assert orders.get(order_id) == main.Order(order_id, 1, T0, "pending")
            lines = orders.lines(order_id)
            assert [ln.line_no for ln in lines] == [1, 2], "line numbers start at 1 in input order"
            assert lines[0] == main.OrderLine(order_id, 1, "WIDGET", 2, Decimal("19.99")), (
                "the price must be snapshotted at order time"
            )

    def test_place_order_unknown_sku_leaves_no_trace(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "unknown.db") as conn:
            seed_shop(conn)
            before = row_count(conn, "orders")
            with pytest.raises(main.UnknownProductError) as info:
                main.OrderRepository(conn).place(1, T0, [("GADGET", 1), ("NOPE", 1)])
            assert isinstance(info.value, LookupError)
            assert "NOPE" in str(info.value)
            assert row_count(conn, "orders") == before, "a failed order must not persist a header"
            assert row_count(conn, "order_items") == 6, "a failed order must not persist lines"

    def test_place_order_unknown_customer_is_rolled_back(
        self, main: ModuleType, tmp_path: Path
    ) -> None:
        with open_db(main, tmp_path / "nocust.db") as conn:
            seed_shop(conn)
            with pytest.raises(sqlite3.IntegrityError):
                main.OrderRepository(conn).place(777, T0, [("GADGET", 1)])
            assert row_count(conn, "orders") == 5
            assert row_count(conn, "order_items") == 6

    def test_place_order_validates_input(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "validate.db") as conn:
            seed_shop(conn)
            repo = main.OrderRepository(conn)
            with pytest.raises(ValueError):
                repo.place(1, T0, [])
            with pytest.raises(ValueError):
                repo.place(1, T0, [("GADGET", 0)])

    def test_set_status_validates_status_and_order_id(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "status.db") as conn:
            seed_shop(conn)
            repo = main.OrderRepository(conn)
            repo.set_status(1, "shipped")
            assert repo.get(1).status == "shipped"  # type: ignore[union-attr]
            with pytest.raises(ValueError):
                repo.set_status(1, "lost-in-the-post")
            with pytest.raises(LookupError):
                repo.set_status(9999, "paid")


# ---------------------------------------------------------------------------
# Part 5: bulk import in batches
# ---------------------------------------------------------------------------


def _catalogue_rows(count: int, *, price_cents: int = 250) -> Iterator[tuple[str, str, Decimal]]:
    for i in range(count):
        yield (f"SKU-{i:05d}", f"Item {i}", Decimal(price_cents + i).scaleb(-2))


class TestPart5BulkImport:
    def test_imports_one_shot_iterator_in_batches(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "bulk.db") as conn:
            total = main.import_products(conn, _catalogue_rows(2500), batch_size=1000)
            assert total == 2500, "import_products must return the number of rows processed"
            assert row_count(conn, "products") == 2500
            assert main.ProductRepository(conn).get("SKU-00007") == main.Product(
                "SKU-00007", "Item 7", Decimal("2.57")
            )

    def test_reimport_upserts_without_duplicates(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "reimport.db") as conn:
            main.import_products(conn, _catalogue_rows(100), batch_size=30)
            main.import_products(conn, _catalogue_rows(100, price_cents=1000), batch_size=30)
            assert row_count(conn, "products") == 100
            assert main.ProductRepository(conn).get("SKU-00000") == main.Product(
                "SKU-00000", "Item 0", Decimal("10.00")
            )

    def test_failing_batch_rolls_back_only_itself(self, main: ModuleType, tmp_path: Path) -> None:
        def rows() -> Iterator[tuple[str, str, Decimal]]:
            yield from _catalogue_rows(650)
            yield ("BAD", "negative", Decimal("-1"))
            yield from _catalogue_rows(350, price_cents=5000)

        with open_db(main, tmp_path / "partial.db") as conn:
            with pytest.raises(ValueError):
                main.import_products(conn, rows(), batch_size=300)
            # Batches [0:300] and [300:600] committed; batch [600:900] held the bad row and was undone.
            assert row_count(conn, "products") == 600
            assert not conn.in_transaction

    def test_batch_size_must_be_positive(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "bs.db") as conn:
            with pytest.raises(ValueError):
                main.import_products(conn, _catalogue_rows(3), batch_size=0)


# ---------------------------------------------------------------------------
# Part 6: keyset vs OFFSET pagination
# ---------------------------------------------------------------------------


def _place_many(main: ModuleType, conn: sqlite3.Connection, customer_id: int, count: int) -> list[int]:
    """Ten orders share T0 (tie on placed_at, broken by id); the rest are one minute apart."""
    repo = main.OrderRepository(conn)
    ids = []
    for i in range(count):
        when = T0 if i < 10 else T0 + timedelta(minutes=i)
        ids.append(repo.place(customer_id, when, [("GADGET", i + 1)]))
    return ids


class TestPart6Pagination:
    def test_keyset_walk_visits_every_order_once_in_order(
        self, main: ModuleType, tmp_path: Path
    ) -> None:
        with open_db(main, tmp_path / "walk.db") as conn:
            seed_shop(conn)
            _place_many(main, conn, 1, 25)
            repo = main.OrderRepository(conn)
            seen: list[int] = []
            cursor = None
            pages = 0
            while True:
                page = repo.page_keyset(cursor, limit=7)
                seen.extend(o.id for o in page.items)
                pages += 1
                cursor = page.next_cursor
                if cursor is None:
                    break
            assert pages == 5, "30 rows at limit 7 need 5 pages (7, 7, 7, 7, 2)"
            expected = [row[0] for row in conn.execute("SELECT id FROM orders ORDER BY placed_at, id")]
            assert len(seen) == 30, "every order must be visited"
            assert seen == expected, "keyset pagination must follow (placed_at, id) and skip nothing"

    def test_keyset_full_page_still_offers_a_cursor(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "exact.db") as conn:
            seed_shop(conn)
            _place_many(main, conn, 1, 5)
            repo = main.OrderRepository(conn)
            first = repo.page_keyset(limit=5)
            assert len(first.items) == 5 and first.next_cursor is not None
            second = repo.page_keyset(first.next_cursor, limit=5)
            assert len(second.items) == 5
            assert second.next_cursor is not None, "a full page always offers a cursor"
            third = repo.page_keyset(second.next_cursor, limit=5)
            assert third.items == () and third.next_cursor is None

    def test_keyset_is_stable_when_older_rows_arrive(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "stable.db") as conn:
            seed_shop(conn)
            _place_many(main, conn, 1, 12)
            repo = main.OrderRepository(conn)
            first = repo.page_keyset(limit=5)
            repo.place(1, T0 - timedelta(days=1), [("GADGET", 1)])  # lands before everything
            second = repo.page_keyset(first.next_cursor, limit=5)
            first_ids = {o.id for o in first.items}
            assert not first_ids & {o.id for o in second.items}, "keyset pages must not overlap"
            assert (second.items[0].placed_at, second.items[0].id) > (
                first.items[-1].placed_at,
                first.items[-1].id,
            ), "the next page must start strictly after the previous page's last (placed_at, id)"

    def test_offset_pagination_shifts_under_inserts(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "offset.db") as conn:
            seed_shop(conn)
            _place_many(main, conn, 1, 12)
            repo = main.OrderRepository(conn)
            first = repo.page_offset(0, 5)
            repo.place(1, T0 - timedelta(days=1), [("GADGET", 1)])
            second = repo.page_offset(5, 5)
            overlap = {o.id for o in first} & {o.id for o in second}
            assert overlap, "OFFSET should duplicate a row after an earlier insert (the gotcha)"

    def test_page_limits_are_validated(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "limits.db") as conn:
            repo = main.OrderRepository(conn)
            with pytest.raises(ValueError):
                repo.page_keyset(limit=0)
            with pytest.raises(ValueError):
                repo.page_offset(-1, 5)

    def test_cursor_round_trips_through_token(self, main: ModuleType) -> None:
        cursor = main.OrderCursor(placed_at=datetime(2026, 2, 3, 4, 5, 6, 7, tzinfo=PLUS_TWO), order_id=42)
        token = cursor.encode()
        assert "=" not in token and "|" not in token, "tokens should be URL-safe and unpadded"
        assert main.OrderCursor.decode(token) == cursor

    @pytest.mark.parametrize(
        "token",
        ["", "not base64 !!", "bm9zZXBhcmF0b3I", "MjAyNi0wMS0wMXwxMA", "' OR 1=1 --"],
    )
    def test_malformed_cursor_tokens_raise_value_error(self, main: ModuleType, token: str) -> None:
        with pytest.raises(ValueError):
            main.OrderCursor.decode(token)


# ---------------------------------------------------------------------------
# Part 7: avoiding N+1 when loading aggregates
# ---------------------------------------------------------------------------


class TestPart7NoNPlusOne:
    def test_orders_load_with_their_lines(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "load.db") as conn:
            seed_shop(conn)
            loaded = main.OrderRepository(conn).load_with_items(1)
            assert [entry.order.id for entry in loaded] == [1, 2], "customer 1 owns orders 1 and 2"
            assert [len(entry.lines) for entry in loaded] == [2, 1], "lines are grouped per order"

    def test_statement_count_is_constant_in_order_count(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "nplus1.db") as conn:
            seed_shop(conn)
            repo = main.OrderRepository(conn)
            small_customer = main.CustomerRepository(conn).add("small@example.com", "Small", T0).id
            _place_many(main, conn, small_customer, 3)
            big_customer = main.CustomerRepository(conn).add("big@example.com", "Big", T0).id
            _place_many(main, conn, big_customer, 40)

            with count_statements(conn) as small_log:
                small = repo.load_with_items(small_customer)
            with count_statements(conn) as big_log:
                big = repo.load_with_items(big_customer)

            assert len(small) == 3 and len(big) == 40
            assert small_log, "trace callback must observe statements (sanity check)"
            assert len(big_log) == len(small_log), "statement count must not grow with order count"
            assert len(big_log) <= 2, f"expected a bounded number of statements, saw {len(big_log)}"
            assert all(len(entry.lines) == 1 for entry in big)

    def test_customer_without_orders_loads_empty(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "empty.db") as conn:
            seed_shop(conn)
            assert main.OrderRepository(conn).load_with_items(4) == []


# ---------------------------------------------------------------------------
# Part 8: reporting queries
# ---------------------------------------------------------------------------


class TestPart8Reporting:
    def test_units_sold_excludes_cancelled_and_sorts(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "units.db") as conn:
            seed_shop(conn)
            rows = main.units_sold_by_product(conn)
            assert rows == [
                main.ProductSales("GADGET", 5, 2),
                main.ProductSales("GIZMO", 3, 1),
                main.ProductSales("WIDGET", 3, 2),
            ], "units desc, then sku asc; cancelled order 3 (10 widgets) must not count"

    def test_revenue_is_exact_decimal_and_excludes_customers_without_orders(
        self, main: ModuleType, tmp_path: Path
    ) -> None:
        with open_db(main, tmp_path / "rev.db") as conn:
            seed_shop(conn)
            rows = main.revenue_by_customer(conn)
            assert [r.customer_id for r in rows] == [1, 2, 3]
            ada = rows[0]
            assert ada == main.CustomerRevenue(1, "Ada Lovelace", 2, Decimal("45.28"))
            assert isinstance(ada.total, Decimal)
            assert str(ada.total) == "45.28", "0.10 x 3 must not drift into float error"

    def test_rank_uses_rank_window_with_ties_and_zero_order_customers(
        self, main: ModuleType, tmp_path: Path
    ) -> None:
        with open_db(main, tmp_path / "rank.db") as conn:
            seed_shop(conn)
            rows = main.rank_customers_by_orders(conn)
            assert [(r.name, r.order_count, r.rank) for r in rows] == [
                ("Ada Lovelace", 2, 1),
                ("Alan Turing", 1, 2),
                ("Grace Hopper", 1, 2),
                ("Edsger Dijkstra", 0, 4),
            ], "ties share a rank and the next rank skips; cancelled orders must not count"

    def test_running_units_per_customer(self, main: ModuleType, tmp_path: Path) -> None:
        with open_db(main, tmp_path / "running.db") as conn:
            seed_shop(conn)
            repo_rows = main.customer_running_units(conn, 1)
            assert [(r.order_id, r.units, r.running_units) for r in repo_rows] == [
                (1, 3, 3),
                (2, 3, 6),
            ]
            assert repo_rows[0].placed_at == datetime(2026, 1, 5, 10, tzinfo=UTC)
            assert repo_rows[0].placed_at.utcoffset() == timedelta(0)
            assert [(r.order_id, r.running_units) for r in main.customer_running_units(conn, 2)] == [
                (4, 4)
            ], "cancelled order 3 must be excluded from the running total"
            assert main.customer_running_units(conn, 4) == []


# ---------------------------------------------------------------------------
# Property check: exact decimal round trip for any cents value
# ---------------------------------------------------------------------------


@settings(
    derandomize=True,
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(cents=st.integers(min_value=0, max_value=10**12))
def test_any_cents_value_round_trips_exactly(main: ModuleType, cents: int) -> None:
    price = Decimal(cents).scaleb(-2)
    with closing(main.connect(":memory:")) as conn:
        main.migrate(conn)
        main.ProductRepository(conn).upsert("P", "p", price)
        stored = main.ProductRepository(conn).get("P")
        assert stored is not None
        assert stored.unit_price == price, f"{price!r} changed on the way through storage"
