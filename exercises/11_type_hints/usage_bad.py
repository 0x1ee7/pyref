"""Misuse that mypy --strict must reject. Each marked line must produce exactly the marked error code."""

from dataclasses import dataclass
from typing import assert_never

from main import (
    DEFAULT_HTTP_TIMEOUT,
    DatabaseSettings,
    Event,
    EventBus,
    Ok,
    OrderPlaced,
    PaymentError,
    Repo,
    Result,
    Settings,
    QueryBuilder,
    Sink,
    TenantId,
    UserCreated,
    UserId,
    fan_out,
    is_ok,
    normalize_key,
    retry,
    user_topic,
    with_tenant,
)


@dataclass
class Plain:
    name: str


class UserQuery(QueryBuilder):
    pass


class NotASink:
    def send(self, item: str) -> None:
        pass


bad_repo: Repo[Plain] = Repo()  # expect: type-var
user_topic(42)  # expect: arg-type
normalize_key(3)  # expect: type-var

bus = EventBus()


def on_order(event: OrderPlaced) -> None:
    pass


bus.subscribe("user.created", on_order)  # expect: call-overload

bad_event: Event = {"kind": "order.placed", "order_id": 1}  # expect: assignment
bad_user: UserCreated = {"kind": "user.created", "user_id": 42, "email": "a@b.c"}  # expect: typeddict-item
bad_db: DatabaseSettings = {"host": "db", "port": "5432", "name": "orders"}  # expect: typeddict-item
partial: Settings = {"database": {"host": "h", "port": 1, "name": "n"}}  # expect: typeddict-item

DEFAULT_HTTP_TIMEOUT = 5.0  # expect: misc


@retry(attempts=2)
def ping(host: str) -> bool:
    return bool(host)


ping(3)  # expect: arg-type


@with_tenant
def load(tenant: TenantId, order_id: int) -> str:
    return f"{tenant}:{order_id}"


load("42")  # expect: arg-type


def label(error: PaymentError) -> int:
    match error:
        case "declined":
            return 1
        case "insufficient_funds":
            return 2
        case _:
            assert_never(error)  # expect: arg-type


outcome: Result[int, PaymentError] = Ok(1)
if is_ok(outcome):
    wrong: str = outcome.value  # expect: assignment

narrow: UserQuery = QueryBuilder("users")  # expect: assignment

sinks = [NotASink()]
fan_out("x", sinks)  # expect: arg-type
typed_sinks: list[Sink[str]] = [NotASink()]  # expect: list-item
