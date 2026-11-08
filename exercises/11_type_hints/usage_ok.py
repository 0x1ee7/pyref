"""Idiomatic usage of every typed API in main.py. Must type-check cleanly under mypy --strict."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Self

from main import (
    DEFAULT_HTTP_TIMEOUT,
    DatabaseSettings,
    Err,
    Event,
    EventBus,
    MemorySink,
    Ok,
    OrderPlaced,
    PaymentError,
    Port,
    QueryBuilder,
    Range,
    Repo,
    Result,
    Settings,
    Sink,
    TenantId,
    Unsubscribe,
    UserCreated,
    UserId,
    all_str,
    collect,
    current_tenant,
    describe_event,
    field_range,
    first,
    first_or,
    fan_out,
    http_status,
    is_ok,
    load_settings,
    normalize_key,
    render_result,
    retry,
    tenant_scope,
    user_topic,
    with_tenant,
)


@dataclass(frozen=True)
class User:
    id: UserId
    email: str


class UserQuery(QueryBuilder):
    def active_only(self) -> Self:
        return self.where("active", True)


class ListSink:
    """Structural sink: never subclasses Sink."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def write(self, item: str) -> None:
        self.seen.append(item)


def repo_usage() -> None:
    repo: Repo[User] = Repo()
    repo.add(User(UserId(1), "a@example.com"))
    found: User | None = repo.get(1)
    total: int = len(repo)
    emails: list[str] = [user.email for user in repo.where(lambda user: user.id > 0)]
    key: str = normalize_key(" KEY ")
    raw_key: bytes = normalize_key(b" KEY ")
    head: int = first([1, 2, 3])
    fallback: int | str = first_or([1], "none")
    _ = (found, total, emails, key, raw_key, head, fallback)


def event_usage() -> None:
    bus = EventBus()

    def on_user(event: UserCreated) -> None:
        _ = event["email"]

    def on_order(event: OrderPlaced) -> None:
        _ = event.get("coupon")

    unsubscribe: Unsubscribe = bus.subscribe("user.created", on_user)
    bus.subscribe("order.placed", on_order)
    event: Event = {"kind": "order.placed", "order_id": 7, "total_cents": 1250}
    delivered: int = bus.publish(event)
    unsubscribe()
    description: str = describe_event(event)
    topic: str = user_topic(UserId(3))
    _ = (delivered, description, topic)


def settings_usage() -> None:
    db: DatabaseSettings = {"host": "db", "port": 5432, "name": "orders"}
    partial: Settings = {"service_name": "orders", "database": db}
    loaded: Settings = load_settings({"SERVICE_NAME": "orders", "DB_HOST": "h", "DB_PORT": "1", "DB_NAME": "n"})
    timeout: float = DEFAULT_HTTP_TIMEOUT
    port_range: Range | None = field_range(Port)
    _ = (partial, loaded, timeout, port_range)


@retry(attempts=3, on=(OSError,))
def fetch(url: str) -> bytes:
    return url.encode()


@with_tenant
def list_orders(tenant: TenantId, status: str) -> list[str]:
    return [f"{tenant}:{status}"]


def decorator_usage() -> None:
    body: bytes = fetch("http://example.test")
    with tenant_scope(TenantId("acme")):
        orders: list[str] = list_orders("open")
        now: TenantId = current_tenant()
        _ = (orders, now)
    _ = body


def query_usage() -> None:
    query: UserQuery = UserQuery("users").where("active", True).order_by("email").limit(10)
    narrowed: UserQuery = query.active_only()
    sql, params = narrowed.build()
    _ = (sql, params)


def charge(amount: int) -> Result[int, PaymentError]:
    if amount > 0:
        return Ok(amount)
    return Err("declined")


def result_usage() -> None:
    outcome = charge(5)
    if is_ok(outcome):
        doubled: int = outcome.value * 2
        _ = doubled
    else:
        status: int = http_status(outcome.error)
        _ = status
    text: str = render_result(outcome)
    combined: Result[list[int], PaymentError] = collect(charge(a) for a in (1, 2))
    _ = (text, combined)


def first_name(values: Sequence[object]) -> str | None:
    if all_str(values):
        return values[0] if values else None
    return None


def sink_usage() -> None:
    memory: MemorySink[str] = MemorySink()
    listing = ListSink()
    sinks: list[Sink[str]] = [memory, listing]
    written: int = fan_out("hello", sinks)
    _ = (written, first_name(["a"]))
