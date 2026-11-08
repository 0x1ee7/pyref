"""Tests for the production logging exercise (see README.md)."""

from __future__ import annotations

import io
import json
import logging
import logging.config
import logging.handlers
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

SHOP = "shop"


@pytest.fixture(autouse=True)
def _restore_logging_state() -> Iterator[None]:
    """Undo every global logging change a test (or the solution) makes."""
    root = logging.getLogger()
    root_handlers = list(root.handlers)
    root_level = root.level
    yield
    for name in list(logging.Logger.manager.loggerDict):
        if name == SHOP or name.startswith(SHOP + "."):
            lg = logging.getLogger(name)
            for handler in list(lg.handlers):
                lg.removeHandler(handler)
                handler.close()
            lg.setLevel(logging.NOTSET)
            lg.propagate = True
            lg.disabled = False
    for handler in list(root.handlers):
        if handler not in root_handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(root_level)


class _Collector(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class _BlockingHandler(_Collector):
    def __init__(self, release: threading.Event) -> None:
        super().__init__()
        self.release = release

    def emit(self, record: logging.LogRecord) -> None:
        self.release.wait(10)
        super().emit(record)


class _Tracked:
    """Counts how often it is rendered as a string."""

    def __init__(self, value: str) -> None:
        self.value = value
        self.str_calls = 0

    def __str__(self) -> str:
        self.str_calls += 1
        return self.value


class GatewayError(Exception):
    pass


def make_record(
    name: str = "shop.orders",
    level: int = logging.WARNING,
    msg: str = "low stock",
    args: tuple[object, ...] = (),
    created: float = 1767225600.123456,
) -> logging.LogRecord:
    record = logging.LogRecord(name, level, __file__, 1, msg, args, None)
    record.created = created
    return record


def _json_lines(buf: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in buf.getvalue().splitlines()]


def _json_stream(main: ModuleType) -> tuple[logging.Logger, io.StringIO]:
    """Logger under ``shop.requests`` writing JSON + request ids into a buffer."""
    lg = logging.getLogger("shop.requests")
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(main.JsonFormatter())
    handler.addFilter(main.RequestIdFilter())
    lg.addHandler(handler)
    lg.setLevel(logging.DEBUG)
    return lg, buf


def _redacted_stream(main: ModuleType) -> tuple[logging.Logger, io.StringIO]:
    """Logger under ``shop.auth`` whose handler applies the redaction filter."""
    lg = logging.getLogger("shop.auth")
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(main.RedactingFilter())
    lg.addHandler(handler)
    lg.setLevel(logging.DEBUG)
    return lg, buf


# ---------------------------------------------------------------------------
# Part 1: hierarchy, library etiquette, per-module levels
# ---------------------------------------------------------------------------


class TestPart1Hierarchy:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("payments", "shop.payments"),
            ("shop.db", "shop.db"),
            ("shop", "shop"),
            ("shopping", "shop.shopping"),
        ],
    )
    def test_get_logger_namespaces_names(self, main: ModuleType, given: str, expected: str) -> None:
        lg = main.get_logger(given)
        assert isinstance(lg, logging.Logger), "get_logger must return a logging.Logger"
        assert lg.name == expected, f"get_logger({given!r}) should name the logger {expected!r}"

    def test_get_logger_rejects_empty_name(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.get_logger("")

    def test_get_logger_returns_the_stdlib_cached_instance(self, main: ModuleType) -> None:
        assert main.get_logger("payments") is logging.getLogger("shop.payments")

    def test_library_logging_attaches_exactly_one_null_handler(self, main: ModuleType) -> None:
        for _ in range(3):
            main.configure_library_logging()
        nulls = [h for h in logging.getLogger("shop").handlers if isinstance(h, logging.NullHandler)]
        assert len(nulls) == 1, f"expected one NullHandler on 'shop', found {len(nulls)}"

    def test_library_logging_leaves_root_and_level_alone(self, main: ModuleType) -> None:
        root = logging.getLogger()
        handlers_before = list(root.handlers)
        level_before = root.level
        main.configure_library_logging()
        assert root.handlers == handlers_before, "a library must not add handlers to root"
        assert root.level == level_before, "a library must not change the root level"
        assert logging.getLogger("shop").level == logging.NOTSET, "a library must not set a level"

    def test_records_propagate_to_root_handlers(self, main: ModuleType, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO)
        main.get_logger("payments").info("captured via propagation")
        assert [r.name for r in caplog.records] == ["shop.payments"], "record should propagate up to root"

    def test_effective_level_is_inherited_from_namespace(self, main: ModuleType) -> None:
        logging.getLogger("shop").setLevel(logging.WARNING)
        assert main.effective_level("db") == logging.WARNING

    def test_set_module_levels_overrides_only_named_modules(self, main: ModuleType) -> None:
        logging.getLogger("shop").setLevel(logging.INFO)
        main.set_module_levels({"shop.db": "debug"})
        assert main.effective_level("db") == logging.DEBUG, "level names are case-insensitive"
        assert main.effective_level("http") == logging.INFO, "siblings keep the namespace level"

    def test_set_module_levels_accepts_int_levels(self, main: ModuleType) -> None:
        main.set_module_levels({"shop.http": logging.ERROR})
        assert logging.getLogger("shop.http").level == logging.ERROR

    def test_set_module_levels_is_all_or_nothing(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.set_module_levels({"shop.db": "DEBUG", "payments": "DEBUG"})
        assert logging.getLogger("shop.db").level == logging.NOTSET, (
            "validation must happen before any logger is changed"
        )

    def test_set_module_levels_rejects_unknown_level(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.set_module_levels({"shop.db": "LOUD"})


# ---------------------------------------------------------------------------
# Part 2: idempotent setup and dictConfig
# ---------------------------------------------------------------------------


class TestPart2SetupAndDictConfig:
    def test_setup_logging_sets_level_and_returns_shop_logger(self, main: ModuleType) -> None:
        lg = main.setup_logging("DEBUG", stream=io.StringIO())
        assert lg is logging.getLogger("shop")
        assert lg.level == logging.DEBUG
        assert lg.propagate is False, "shop records must not also reach root handlers"

    def test_setup_logging_is_idempotent(self, main: ModuleType) -> None:
        stream = io.StringIO()
        for _ in range(3):
            main.setup_logging("INFO", stream=stream)
        handlers = [h for h in logging.getLogger("shop").handlers if not isinstance(h, logging.NullHandler)]
        assert len(handlers) == 1, f"repeated setup must not stack handlers, found {len(handlers)}"
        main.get_logger("payments").info("once")
        assert stream.getvalue().count("once") == 1, "each record must be written exactly once"

    def test_setup_logging_defaults_to_current_sys_stderr(
        self, main: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main.setup_logging("INFO")
        main.get_logger("payments").info("order shipped")
        err = capsys.readouterr().err
        assert "INFO" in err and "shop.payments" in err and "order shipped" in err, (
            "default stream must be sys.stderr as it is when setup runs"
        )

    def test_setup_logging_replaces_stream_on_reconfigure(self, main: ModuleType) -> None:
        first, second = io.StringIO(), io.StringIO()
        main.setup_logging("INFO", stream=first)
        main.setup_logging("INFO", stream=second)
        main.get_logger("payments").info("hello")
        assert first.getvalue() == "", "the replaced handler must no longer receive records"
        assert "hello" in second.getvalue()

    def test_setup_logging_does_not_touch_root(self, main: ModuleType) -> None:
        root = logging.getLogger()
        before = list(root.handlers)
        main.setup_logging("INFO", stream=io.StringIO())
        assert root.handlers == before

    def test_setup_logging_rejects_unknown_level(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.setup_logging("LOUD", stream=io.StringIO())

    def test_make_dict_config_is_a_valid_config_that_skips_root(self, main: ModuleType) -> None:
        cfg = main.make_dict_config(level="WARNING")
        assert cfg["version"] == 1
        assert cfg["disable_existing_loggers"] is False, "existing module loggers must stay enabled"
        assert "root" not in cfg, "the dict must never configure the root logger"
        logging.config.dictConfig(cfg)
        assert logging.getLogger("shop").level == logging.WARNING

    def test_dictconfig_writes_to_file_and_applies_level(self, main: ModuleType, tmp_path: Path) -> None:
        log_file = tmp_path / "app.log"
        logging.config.dictConfig(main.make_dict_config(level="WARNING", log_file=log_file))
        lg = main.get_logger("payments")
        lg.info("quiet")
        lg.warning("loud %s", "one")
        for handler in logging.getLogger("shop").handlers:
            handler.flush()
        text = log_file.read_text(encoding="utf-8")
        assert "loud one" in text and "quiet" not in text
        assert "WARNING" in text and "shop.payments" in text

    def test_dictconfig_module_level_override(self, main: ModuleType, tmp_path: Path) -> None:
        log_file = tmp_path / "app.log"
        cfg = main.make_dict_config(level="INFO", log_file=log_file, module_levels={"shop.db": "DEBUG"})
        logging.config.dictConfig(cfg)
        main.get_logger("db").debug("query took 3ms")
        main.get_logger("http").debug("request body")
        main.get_logger("http").info("served")
        for handler in logging.getLogger("shop").handlers:
            handler.flush()
        text = log_file.read_text(encoding="utf-8")
        assert "query took 3ms" in text, "shop.db was overridden to DEBUG"
        assert "request body" not in text, "shop.http keeps the INFO namespace level"
        assert "served" in text

    def test_dictconfig_applied_twice_does_not_duplicate_handlers(self, main: ModuleType, tmp_path: Path) -> None:
        log_file = tmp_path / "app.log"
        logging.config.dictConfig(main.make_dict_config(level="INFO", log_file=log_file))
        logging.config.dictConfig(main.make_dict_config(level="INFO", log_file=log_file))
        handlers = [h for h in logging.getLogger("shop").handlers if not isinstance(h, logging.NullHandler)]
        assert len(handlers) == 2, f"expected console + file once each, found {len(handlers)}"
        main.get_logger("payments").info("once-only-marker")
        for handler in logging.getLogger("shop").handlers:
            handler.flush()
        text = log_file.read_text(encoding="utf-8")
        assert text.count("once-only-marker") == 1

    def test_make_dict_config_rejects_keys_outside_namespace(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.make_dict_config(module_levels={"payments": "DEBUG"})


# ---------------------------------------------------------------------------
# Part 3: lazy arguments, exception logging, adapters
# ---------------------------------------------------------------------------


class TestPart3LazyArgsAndExceptions:
    def test_charge_returns_txn_and_logs_info(self, main: ModuleType, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="shop.payments")
        lg = logging.getLogger("shop.payments")
        txn = main.charge(lg, "ORD-1", 1250, lambda order, cents: "T-9")
        assert txn == "T-9"
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 1
        assert infos[0].getMessage() == "charged order ORD-1 txn=T-9"

    def test_debug_message_keeps_percent_args_unrendered(
        self, main: ModuleType, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="shop.payments")
        lg = logging.getLogger("shop.payments")
        main.charge(lg, "ORD-1", 1250, lambda order, cents: "T-9")
        debug = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert debug and debug[0].getMessage() == "charging order ORD-1 for 1250 cents"
        assert debug[0].args == ("ORD-1", 1250), "args must be passed to the logger, not pre-formatted"

    def test_arguments_are_not_rendered_when_level_is_disabled(
        self, main: ModuleType, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="shop.payments")
        lg = logging.getLogger("shop.payments")
        order = _Tracked("ORD-1")
        main.charge(lg, order, 1250, lambda o, c: "T-9")  # type: ignore[arg-type]
        assert order.str_calls == 0, "%-args must not be formatted for disabled levels"

    def test_gateway_failure_logs_traceback_and_reraises_same_exception(
        self, main: ModuleType, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="shop.payments")
        lg = logging.getLogger("shop.payments")
        boom = GatewayError("card declined")

        def gateway(order: str, cents: int) -> str:
            raise boom

        with pytest.raises(GatewayError) as excinfo:
            main.charge(lg, "ORD-2", 500, gateway)
        assert excinfo.value is boom, "the original exception must propagate unchanged"
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert errors[0].getMessage() == "charge failed for order ORD-2"
        assert errors[0].exc_info is not None and errors[0].exc_info[0] is GatewayError
        assert "Traceback (most recent call last)" in caplog.text
        assert "GatewayError: card declined" in caplog.text

    def test_non_positive_amount_is_rejected_before_gateway_call(self, main: ModuleType) -> None:
        calls: list[tuple[str, int]] = []

        def gateway(order: str, cents: int) -> str:
            calls.append((order, cents))
            return "x"

        with pytest.raises(ValueError):
            main.charge(logging.getLogger("shop.payments"), "ORD-3", 0, gateway)
        assert calls == []

    def test_bind_fields_adds_fields_to_every_record(self, main: ModuleType, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="shop.payments")
        adapter = main.bind_fields(logging.getLogger("shop.payments"), order_id="ORD-1")
        assert isinstance(adapter, logging.LoggerAdapter)
        adapter.info("paid", extra={"txn": "T-9"})
        record = caplog.records[-1]
        assert record.order_id == "ORD-1", "bound field must appear on the record"  # type: ignore[attr-defined]
        assert record.txn == "T-9", "per-call extra must be merged in"  # type: ignore[attr-defined]

    def test_bind_fields_nests_merges_and_never_mutates_parent(
        self, main: ModuleType, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="shop.payments")
        base = main.bind_fields(logging.getLogger("shop.payments"), service="billing", region="eu")
        child = main.bind_fields(base, region="us", order_id="ORD-7")
        child.info("child")
        base.info("parent")
        child_rec, parent_rec = caplog.records
        assert (child_rec.service, child_rec.region, child_rec.order_id) == (  # type: ignore[attr-defined]
            "billing",
            "us",
            "ORD-7",
        )
        assert (parent_rec.service, parent_rec.region) == ("billing", "eu")  # type: ignore[attr-defined]
        assert not hasattr(parent_rec, "order_id"), "binding a child must not mutate the parent"

    def test_per_call_extra_overrides_bound_field(self, main: ModuleType, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="shop.payments")
        adapter = main.bind_fields(logging.getLogger("shop.payments"), order_id="ORD-1")
        adapter.info("retry", extra={"order_id": "ORD-2"})
        assert caplog.records[-1].order_id == "ORD-2"  # type: ignore[attr-defined]

    @pytest.mark.parametrize("field", ["message", "levelname", "msg", "args"])
    def test_bind_fields_rejects_logrecord_attribute_names(self, main: ModuleType, field: str) -> None:
        with pytest.raises(ValueError):
            main.bind_fields(logging.getLogger("shop.payments"), **{field: "x"})


# ---------------------------------------------------------------------------
# Part 4: structured JSON and request ids
# ---------------------------------------------------------------------------


class TestPart4JsonAndRequestId:
    def test_core_fields_with_utc_millisecond_timestamp(self, main: ModuleType) -> None:
        payload = json.loads(main.JsonFormatter().format(make_record()))
        assert payload == {
            "timestamp": "2026-01-01T00:00:00.123+00:00",
            "level": "WARNING",
            "logger": "shop.orders",
            "message": "low stock",
        }

    def test_timestamp_parses_as_utc(self, main: ModuleType) -> None:
        payload = json.loads(main.JsonFormatter().format(make_record()))
        parsed = datetime.fromisoformat(payload["timestamp"])
        assert parsed.utcoffset() == timedelta(0), "timestamp must carry a UTC offset"

    def test_message_is_rendered_from_args(self, main: ModuleType) -> None:
        payload = json.loads(main.JsonFormatter().format(make_record(msg="%d items", args=(3,))))
        assert payload["message"] == "3 items"

    def test_extra_fields_are_top_level_keys(self, main: ModuleType) -> None:
        logger, buf = _json_stream(main)
        logger.info("order placed", extra={"order_id": 42, "tags": ["vip", "eu"]})
        payload = _json_lines(buf)[-1]
        assert payload["order_id"] == 42
        assert payload["tags"] == ["vip", "eu"]
        assert payload["message"] == "order placed"

    def test_non_serialisable_extra_falls_back_to_str(self, main: ModuleType) -> None:
        class Opaque:
            def __str__(self) -> str:
                return "opaque-object"

        logger, buf = _json_stream(main)
        logger.info("blob", extra={"blob": Opaque()})
        assert _json_lines(buf)[-1]["blob"] == "opaque-object"

    def test_exception_is_one_json_line_with_traceback(self, main: ModuleType) -> None:
        logger, buf = _json_stream(main)
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            logger.exception("crashed")
        lines = buf.getvalue().splitlines()
        assert len(lines) == 1, "a record with a traceback must still be a single JSON line"
        payload = json.loads(lines[0])
        assert payload["level"] == "ERROR"
        assert "Traceback (most recent call last)" in payload["exc_info"]
        assert payload["exc_info"].rstrip().endswith("RuntimeError: boom")

    def test_extras_cannot_override_core_keys(self, main: ModuleType) -> None:
        logger, buf = _json_stream(main)
        logger.info("real", extra={"level": "FAKE", "timestamp": "1999"})
        payload = _json_lines(buf)[-1]
        assert payload["level"] == "INFO"
        assert payload["timestamp"] != "1999"
        assert payload["timestamp"].endswith("+00:00")

    def test_bound_request_id_is_injected_outside_is_dash(self, main: ModuleType) -> None:
        logger, buf = _json_stream(main)
        with main.bind_request_id("req-42"):
            logger.info("inside")
        logger.info("outside")
        inside, outside = _json_lines(buf)
        assert inside["request_id"] == "req-42"
        assert outside["request_id"] == "-"

    def test_nested_request_ids_restore_outer_value(self, main: ModuleType) -> None:
        logger, buf = _json_stream(main)
        with main.bind_request_id("outer"):
            with main.bind_request_id("inner"):
                logger.info("a")
            logger.info("b")
        assert [p["request_id"] for p in _json_lines(buf)] == ["inner", "outer"]
        assert main.request_id_var.get() is None, "the context must be reset after the block"

    def test_handler_filter_covers_child_loggers(self, main: ModuleType) -> None:
        _, buf = _json_stream(main)
        child = logging.getLogger("shop.requests.worker")
        with main.bind_request_id("req-7"):
            child.info("from child")
        assert _json_lines(buf)[-1]["request_id"] == "req-7", (
            "a filter on the handler sees records propagated from children"
        )

    def test_explicit_extra_request_id_wins(self, main: ModuleType) -> None:
        logger, buf = _json_stream(main)
        with main.bind_request_id("ctx"):
            logger.info("x", extra={"request_id": "explicit"})
        assert _json_lines(buf)[-1]["request_id"] == "explicit"


# ---------------------------------------------------------------------------
# Part 5: secret redaction
# ---------------------------------------------------------------------------


class TestPart5Redaction:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("password=hunter2", "password=***"),
            ('password: "hunter2"', 'password: "***"'),
            ("user=alice token=abc123 status=ok", "user=alice token=*** status=ok"),
            ("api_key=XYZ-9", "api_key=***"),
            ("client_secret='s3cr3t value'", "client_secret='***'"),
            ("Authorization: Bearer eyJhbGciOi.payload.sig", "Authorization: ***"),
            ("{'Authorization': 'Basic dXNlcjpwYXNz'}", "{'Authorization': '***'}"),
            ('{"password": "hunter2", "user": "bob"}', '{"password": "***", "user": "bob"}'),
        ],
    )
    def test_redact_masks_secret_values(self, main: ModuleType, raw: str, expected: str) -> None:
        assert main.redact(raw) == expected

    def test_redact_leaves_ordinary_text_alone(self, main: ModuleType) -> None:
        text = "order 42 shipped to tokens-bucket, 3 tokens left, secretary=ok"
        assert main.redact(text) == text, "whole-word keys only; no false positives"

    def test_filter_redacts_rendered_args(self, main: ModuleType) -> None:
        lg, buf = _redacted_stream(main)
        lg.info("login attempt %s", "password=hunter2")
        out = buf.getvalue()
        assert "hunter2" not in out
        assert "login attempt password=***" in out

    def test_filter_redacts_authorization_header_in_dict_args(self, main: ModuleType) -> None:
        lg, buf = _redacted_stream(main)
        lg.debug("outgoing headers %s", {"Authorization": "Bearer abc.def"})
        out = buf.getvalue()
        assert "abc.def" not in out
        assert "{'Authorization': '***'}" in out

    def test_filter_passes_non_secret_args_through(self, main: ModuleType) -> None:
        lg, buf = _redacted_stream(main)
        lg.info("%d items for %s", 3, "alice")
        assert "INFO 3 items for alice" in buf.getvalue()

    def test_filter_masks_sensitive_named_attributes_and_never_drops(self, main: ModuleType) -> None:
        record = logging.LogRecord("shop.auth", logging.INFO, __file__, 1, "hello", (), None)
        record.api_key = "k-123"  # type: ignore[attr-defined]
        record.user = "bob"  # type: ignore[attr-defined]
        assert main.RedactingFilter().filter(record) is True
        assert record.api_key == "***"  # type: ignore[attr-defined]
        assert record.user == "bob"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Part 6: rotating files, installed idempotently
# ---------------------------------------------------------------------------


class TestPart6RotatingFile:
    def test_install_returns_configured_handler_without_creating_file(self, main: ModuleType, tmp_path: Path) -> None:
        path = tmp_path / "shop.log"
        lg = logging.getLogger("shop.files")
        handler = main.install_rotating_file(lg, path, max_bytes=500, backup_count=2)
        assert isinstance(handler, logging.handlers.RotatingFileHandler)
        assert handler.maxBytes == 500 and handler.backupCount == 2
        assert handler in lg.handlers
        assert not path.exists(), "the file should not be created until the first record"

    def test_records_land_in_the_file(self, main: ModuleType, tmp_path: Path) -> None:
        path = tmp_path / "shop.log"
        lg = logging.getLogger("shop.files")
        lg.setLevel(logging.INFO)
        handler = main.install_rotating_file(lg, path, max_bytes=1000, backup_count=1)
        lg.info("shipped %s", "o-1")
        handler.flush()
        assert path.read_text(encoding="utf-8").splitlines() == ["INFO shop.files shipped o-1"]

    def test_rotation_keeps_at_most_backup_count_files(self, main: ModuleType, tmp_path: Path) -> None:
        path = tmp_path / "shop.log"
        lg = logging.getLogger("shop.files")
        lg.setLevel(logging.INFO)
        handler = main.install_rotating_file(lg, path, max_bytes=200, backup_count=2)
        for i in range(60):
            lg.info("event %03d", i)
        handler.flush()
        first = tmp_path / "shop.log.1"
        second = tmp_path / "shop.log.2"
        assert path.exists() and first.exists() and second.exists()
        assert not (tmp_path / "shop.log.3").exists(), "backup_count=2 must cap the backups"
        for file in (path, first, second):
            assert file.stat().st_size < 200
        assert path.read_text(encoding="utf-8").splitlines()[-1].endswith("event 059")

    def test_install_is_idempotent_per_path(self, main: ModuleType, tmp_path: Path) -> None:
        lg = logging.getLogger("shop.files")
        path = tmp_path / "shop.log"
        first = main.install_rotating_file(lg, path, max_bytes=500, backup_count=1)
        second = main.install_rotating_file(lg, path, max_bytes=500, backup_count=1)
        assert second is first
        rotating = [h for h in lg.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(rotating) == 1
        other = main.install_rotating_file(lg, tmp_path / "other.log", max_bytes=500, backup_count=1)
        assert other is not first, "a different path needs its own handler"

    @pytest.mark.parametrize(("max_bytes", "backup_count"), [(0, 1), (-5, 1), (100, -1)])
    def test_rejects_invalid_limits(self, main: ModuleType, tmp_path: Path, max_bytes: int, backup_count: int) -> None:
        with pytest.raises(ValueError):
            main.install_rotating_file(
                logging.getLogger("shop.files"),
                tmp_path / "x.log",
                max_bytes=max_bytes,
                backup_count=backup_count,
            )


# ---------------------------------------------------------------------------
# Part 7: non-blocking logging via QueueHandler / QueueListener
# ---------------------------------------------------------------------------


class TestPart7BackgroundLogging:
    def test_delivers_all_records_in_order_on_exit(self, main: ModuleType) -> None:
        lg = logging.getLogger("shop.async")
        lg.setLevel(logging.DEBUG)
        sink = _Collector()
        with main.BackgroundLogging(lg, [sink], queue_size=1000) as bg:
            assert bg.running
            for i in range(100):
                lg.info("event %d", i)
        assert sink.messages == [f"event {i}" for i in range(100)]
        assert bg.dropped == 0
        assert not bg.running
        assert not any(isinstance(h, logging.handlers.QueueHandler) for h in lg.handlers), (
            "the queue handler must be detached on exit"
        )

    def test_full_queue_drops_instead_of_blocking(self, main: ModuleType) -> None:
        lg = logging.getLogger("shop.async")
        lg.setLevel(logging.DEBUG)
        release = threading.Event()
        sink = _BlockingHandler(release)
        # The worker takes at most one record and then blocks in emit(); the queue holds two.
        # So between 7 and 8 of the 10 records are dropped, deterministically.
        with main.BackgroundLogging(lg, [sink], queue_size=2) as bg:
            for i in range(10):
                lg.info("event %d", i)
            blocked_drops = bg.dropped
            release.set()
        assert 7 <= blocked_drops <= 8, f"expected 7-8 drops while the worker was blocked, got {blocked_drops}"
        assert len(sink.messages) == 10 - bg.dropped, "every accepted record must be delivered"
        indexes = [int(message.split()[1]) for message in sink.messages]
        assert indexes == sorted(indexes), "accepted records must keep their order"

    def test_records_are_rendered_at_enqueue_time(self, main: ModuleType) -> None:
        lg = logging.getLogger("shop.async")
        lg.setLevel(logging.DEBUG)
        sink = _Collector()
        payload = ["a"]
        with main.BackgroundLogging(lg, [sink]):
            lg.info("items=%s", payload)
            payload.append("b")
        assert sink.messages == ["items=['a']"], "mutation after logging must not leak into the record"

    def test_listener_respects_handler_levels(self, main: ModuleType) -> None:
        lg = logging.getLogger("shop.async")
        lg.setLevel(logging.DEBUG)
        sink = _Collector()
        sink.setLevel(logging.WARNING)
        with main.BackgroundLogging(lg, [sink]):
            lg.info("quiet")
            lg.warning("loud")
        assert sink.messages == ["loud"]

    def test_exception_in_block_still_stops_listener_and_propagates(self, main: ModuleType) -> None:
        lg = logging.getLogger("shop.async")
        lg.setLevel(logging.DEBUG)
        sink = _Collector()
        bg = main.BackgroundLogging(lg, [sink])
        with pytest.raises(RuntimeError, match="kaboom"):
            with bg:
                lg.info("before crash")
                raise RuntimeError("kaboom")
        assert not bg.running
        assert sink.messages == ["before crash"], "records queued before the crash are still delivered"
        assert not any(isinstance(h, logging.handlers.QueueHandler) for h in lg.handlers)

    def test_entering_a_running_instance_is_rejected(self, main: ModuleType) -> None:
        lg = logging.getLogger("shop.async")
        sink = _Collector()
        bg = main.BackgroundLogging(lg, [sink])
        with bg:
            with pytest.raises(RuntimeError):
                bg.__enter__()

    def test_rejects_non_positive_queue_size(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.BackgroundLogging(logging.getLogger("shop.async"), [], queue_size=0)
