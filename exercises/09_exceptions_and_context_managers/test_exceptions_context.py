"""Tests for exceptions and context managers."""

import builtins
import errno
import json
import os
import pickle
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


class FakeClock:
    """Clock whose reading is set explicitly by the test."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Recorder:
    """Resource that remembers whether it was closed."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _task(ran: list[str], name: str, outcome: object) -> Callable[[], object]:
    """Build a batch task that records its name and either returns or raises ``outcome``."""

    def run() -> object:
        ran.append(name)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return run


ENV_EXISTING = "PYREF_EXC_TEST_EXISTING"
ENV_NEW = "PYREF_EXC_TEST_NEW"


class TestPart1Hierarchy:
    """Domain exception hierarchy and translation of OS / JSON / encoding errors."""

    def test_config_errors_share_one_hierarchy(self, main: ModuleType) -> None:
        for name in ("ConfigNotFound", "ConfigUnreadable", "ConfigInvalid", "SettingMissing"):
            cls = getattr(main, name)
            assert issubclass(cls, main.ConfigError), f"{name} must subclass ConfigError"
            assert issubclass(cls, main.DomainError), f"{name} must subclass DomainError"
        assert issubclass(main.ConfigError, main.DomainError)
        assert not issubclass(main.ConfigNotFound, main.ConfigUnreadable), "siblings must be unrelated"

    def test_missing_file_becomes_config_not_found_with_oserror_cause(
        self, main: ModuleType, tmp_path: Path
    ) -> None:
        missing = tmp_path / "absent.json"
        with pytest.raises(main.ConfigNotFound) as info:
            main.load_config(missing)
        assert isinstance(info.value.__cause__, FileNotFoundError), "original OSError must be __cause__"
        assert str(missing) in str(info.value), "message should name the file"

    def test_directory_becomes_config_unreadable_not_not_found(self, main: ModuleType, tmp_path: Path) -> None:
        with pytest.raises(main.ConfigUnreadable) as info:
            main.load_config(tmp_path)
        cause = info.value.__cause__
        assert isinstance(cause, OSError), "the OSError must be preserved as __cause__"
        assert not isinstance(cause, FileNotFoundError), "a directory is not a missing file"

    def test_malformed_json_becomes_config_invalid(self, main: ModuleType, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(main.ConfigInvalid) as info:
            main.load_config(path)
        assert isinstance(info.value.__cause__, json.JSONDecodeError)
        assert "line 1" in str(info.value), "message should carry the decoder's line number"

    def test_invalid_utf8_becomes_config_invalid(self, main: ModuleType, tmp_path: Path) -> None:
        path = tmp_path / "latin.json"
        path.write_bytes(b"\xff\xfe{}")
        with pytest.raises(main.ConfigInvalid) as info:
            main.load_config(path)
        assert isinstance(info.value.__cause__, UnicodeDecodeError)

    def test_non_object_root_is_invalid_without_cause(self, main: ModuleType, tmp_path: Path) -> None:
        path = tmp_path / "list.json"
        path.write_text("[1, 2]", encoding="utf-8")
        with pytest.raises(main.ConfigInvalid) as info:
            main.load_config(path)
        assert info.value.__cause__ is None, "no underlying exception exists to chain"

    def test_valid_config_returns_dict(self, main: ModuleType, tmp_path: Path) -> None:
        path = tmp_path / "ok.json"
        path.write_text('{"db": {"url": "sqlite://"}}', encoding="utf-8")
        assert main.load_config(str(path)) == {"db": {"url": "sqlite://"}}

    def test_setting_missing_survives_pickle(self, main: ModuleType) -> None:
        restored = pickle.loads(pickle.dumps(main.SettingMissing("db.url")))
        assert isinstance(restored, main.SettingMissing)
        assert restored.key == "db.url", "custom __init__ must keep pickling round-trip intact"
        assert "db.url" in str(restored)


class TestPart2Chaining:
    """Cause/context semantics, notes, and walking the exception chain."""

    def test_get_setting_walks_dotted_path(self, main: ModuleType) -> None:
        config = {"db": {"pool": {"size": 10}}}
        assert main.get_setting(config, "db.pool.size") == 10
        assert main.get_setting(config, "db.pool") == {"size": 10}

    def test_missing_key_raises_setting_missing_from_none(self, main: ModuleType) -> None:
        with pytest.raises(main.SettingMissing) as info:
            main.get_setting({"db": {}}, "db.url")
        err = info.value
        assert err.key == "db.url"
        assert err.__cause__ is None, "from None must leave no cause"
        assert err.__suppress_context__ is True, "from None must suppress the implicit context"
        assert isinstance(err.__context__, KeyError), "the KeyError stays reachable as __context__"

    def test_scalar_intermediate_counts_as_missing(self, main: ModuleType) -> None:
        with pytest.raises(main.SettingMissing) as info:
            main.get_setting({"db": "sqlite"}, "db.url")
        assert info.value.key == "db.url"

    def test_cast_is_applied_to_found_value(self, main: ModuleType) -> None:
        assert main.get_setting({"db": {"port": "5432"}}, "db.port", cast=int) == 5432

    def test_bad_cast_becomes_config_invalid_with_cause_and_note(self, main: ModuleType) -> None:
        with pytest.raises(main.ConfigInvalid) as info:
            main.get_setting({"db": {"port": "abc"}}, "db.port", cast=int)
        assert isinstance(info.value.__cause__, ValueError)
        notes = getattr(info.value, "__notes__", [])
        assert any("db.port" in note for note in notes), f"expected a note naming the key, got {notes!r}"

    def test_domain_error_from_cast_passes_through_untouched(self, main: ModuleType) -> None:
        original = main.SettingMissing("inner")

        def cast(_: object) -> object:
            raise original

        with pytest.raises(main.SettingMissing) as info:
            main.get_setting({"x": 1}, "x", cast=cast)
        assert info.value is original, "only ValueError/TypeError are translated"

    def test_describe_chain_follows_cause(self, main: ModuleType) -> None:
        try:
            try:
                raise OSError("disk")
            except OSError as err:
                raise main.ConfigUnreadable("cannot read") from err
        except main.ConfigUnreadable as err:
            chain = main.describe_chain(err)
        assert chain == ["ConfigUnreadable: cannot read", "OSError: disk"]

    def test_describe_chain_skips_suppressed_context_and_terminates_on_cycles(self, main: ModuleType) -> None:
        try:
            try:
                {}["k"]
            except KeyError:
                raise main.SettingMissing("k") from None
        except main.SettingMissing as err:
            chain = main.describe_chain(err)
        assert len(chain) == 1, f"suppressed context must not appear, got {chain!r}"
        assert chain[0].startswith("SettingMissing")

        first = ValueError("a")
        second = KeyError("b")
        first.__context__ = second
        second.__context__ = first
        assert len(main.describe_chain(first)) == 2, "cycle must terminate"
        assert main.describe_chain(ValueError()) == ["ValueError"], "empty messages render as the type name"


class TestPart3TryElseFinally:
    """try / else / finally ordering and wrapping rules for run_checked."""

    def test_success_logs_start_ok_end_and_returns_value(self, main: ModuleType) -> None:
        log: list[str] = []
        assert main.run_checked("load", lambda: 41 + 1, log) == 42
        assert log == ["start load", "ok load", "end load"]

    def test_failure_raises_step_failed_chained_and_logs_failed(self, main: ModuleType) -> None:
        def step() -> None:
            raise ValueError("bad row")

        log: list[str] = []
        with pytest.raises(main.StepFailed) as info:
            main.run_checked("load", step, log)
        assert isinstance(info.value, main.DomainError)
        assert info.value.step_name == "load"
        assert isinstance(info.value.__cause__, ValueError)
        assert log == ["start load", "failed load", "end load"], "ok must never be logged on failure"

    def test_base_exceptions_bypass_wrapping_but_still_run_finally(self, main: ModuleType) -> None:
        def step() -> None:
            raise KeyboardInterrupt

        log: list[str] = []
        with pytest.raises(KeyboardInterrupt):
            main.run_checked("load", step, log)
        assert log == ["start load", "end load"], "finally must run even for BaseException"

    def test_log_is_appended_to_not_replaced(self, main: ModuleType) -> None:
        log = ["previous"]
        main.run_checked("a", lambda: None, log)
        assert log == ["previous", "start a", "ok a", "end a"]


class TestPart4Transaction:
    """Class-based context manager: commit, rollback, savepoints, reentrancy."""

    def test_commit_applies_sets_and_deletes_on_clean_exit(self, main: ModuleType) -> None:
        store = {"a": 1, "b": 2}
        with main.Transaction(store) as tx:
            tx.set("a", 10)
            tx.set("c", 3)
            tx.delete("b")
        assert store == {"a": 10, "c": 3}

    def test_writes_are_invisible_to_store_until_commit(self, main: ModuleType) -> None:
        store = {"a": 1}
        with main.Transaction(store) as tx:
            tx.set("a", 5)
            assert store == {"a": 1}, "store must not change before commit"
            assert tx.get("a") == 5, "reads inside the transaction see pending writes"
        assert store == {"a": 5}

    def test_exception_rolls_back_and_propagates_same_instance(self, main: ModuleType) -> None:
        store = {"a": 1}
        err = RuntimeError("boom")
        with pytest.raises(RuntimeError) as info:
            with main.Transaction(store) as tx:
                tx.set("a", 2)
                raise err
        assert info.value is err, "__exit__ must not suppress or replace the exception"
        assert store == {"a": 1}

    def test_missing_and_deleted_keys_raise_keyerror(self, main: ModuleType) -> None:
        store = {"a": 1}
        with main.Transaction(store) as tx:
            with pytest.raises(KeyError):
                tx.get("zzz")
            with pytest.raises(KeyError):
                tx.delete("zzz")
            tx.delete("a")
            with pytest.raises(KeyError):
                tx.get("a")
        assert store == {}

    def test_savepoint_rollback_is_suppressed_and_keeps_earlier_writes(self, main: ModuleType) -> None:
        store = {"a": 0}
        with main.Transaction(store) as tx:
            tx.set("a", 1)
            with tx.savepoint():
                tx.set("a", 2)
                tx.set("b", 3)
                raise main.SavepointRollback("discard this block")
            assert tx.get("a") == 1, "writes before the savepoint survive"
            with pytest.raises(KeyError):
                tx.get("b")
        assert store == {"a": 1}

    def test_other_exception_in_savepoint_propagates_but_outer_can_continue(self, main: ModuleType) -> None:
        store: dict[str, int] = {}
        with main.Transaction(store) as tx:
            tx.set("keep", 1)
            try:
                with tx.savepoint():
                    tx.set("drop", 2)
                    raise ValueError("boom")
            except ValueError:
                pass
            tx.set("after", 3)
        assert store == {"keep": 1, "after": 3}

    def test_nested_savepoints_roll_back_only_the_inner_block(self, main: ModuleType) -> None:
        store = {"a": 0}
        with main.Transaction(store) as tx:
            tx.set("a", 1)
            with tx.savepoint():
                tx.set("a", 2)
                with tx.savepoint():
                    tx.set("a", 3)
                    raise main.SavepointRollback()
                assert tx.get("a") == 2
        assert store == {"a": 2}

    def test_reentering_active_transaction_raises_and_outer_stays_usable(self, main: ModuleType) -> None:
        store: dict[str, int] = {}
        tx = main.Transaction(store)
        with tx:
            with pytest.raises(RuntimeError):
                with tx:
                    pass
            tx.set("x", 1)
        assert store == {"x": 1}

    def test_savepoint_requires_active_and_transaction_is_reusable(self, main: ModuleType) -> None:
        store: dict[str, int] = {}
        tx = main.Transaction(store)
        with tx:
            tx.set("a", 1)
        with pytest.raises(RuntimeError):
            tx.savepoint()
        with tx:
            tx.set("b", 2)
        assert store == {"a": 1, "b": 2}


class TestPart5ContextmanagerUtilities:
    """@contextmanager generators, ExitStack, suppress, and an injectable-clock timer."""

    def test_temporary_env_sets_then_restores(self, main: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_EXISTING, "original")
        with main.temporary_env(**{ENV_EXISTING: "temp"}):
            assert os.environ[ENV_EXISTING] == "temp"
        assert os.environ[ENV_EXISTING] == "original"

    def test_temporary_env_removes_variables_it_created(self, main: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_NEW, raising=False)
        with main.temporary_env(**{ENV_NEW: "x"}):
            assert os.environ[ENV_NEW] == "x"
        assert ENV_NEW not in os.environ

    def test_temporary_env_none_unsets_inside_and_restores_after(
        self, main: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_EXISTING, "orig")
        with main.temporary_env(**{ENV_EXISTING: None}):
            assert ENV_EXISTING not in os.environ
        assert os.environ[ENV_EXISTING] == "orig"

    def test_temporary_env_restores_on_exception(self, main: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_EXISTING, "original")
        monkeypatch.delenv(ENV_NEW, raising=False)
        with pytest.raises(ValueError):
            with main.temporary_env(**{ENV_EXISTING: "temp", ENV_NEW: "x"}):
                raise ValueError("boom")
        assert os.environ[ENV_EXISTING] == "original"
        assert ENV_NEW not in os.environ

    def test_open_all_yields_files_in_order_and_closes_them(self, main: ModuleType, tmp_path: Path) -> None:
        first = tmp_path / "one.txt"
        second = tmp_path / "two.txt"
        first.write_text("alpha", encoding="utf-8")
        second.write_text("beta", encoding="utf-8")
        with main.open_all([first, second]) as files:
            assert [f.read() for f in files] == ["alpha", "beta"]
        assert all(f.closed for f in files), "every file must be closed on exit"

    def test_open_all_closes_already_opened_files_when_a_later_open_fails(
        self, main: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        good = tmp_path / "good.txt"
        good.write_text("ok", encoding="utf-8")
        missing = tmp_path / "missing.txt"

        real_open = builtins.open
        opened: list[Any] = []

        def recording_open(*args: object, **kwargs: object) -> Any:
            handle = real_open(*args, **kwargs)  # type: ignore[call-overload]
            opened.append(handle)
            return handle

        monkeypatch.setattr(builtins, "open", recording_open)
        with pytest.raises(FileNotFoundError):
            with main.open_all([good, missing]):
                pass
        good_handles = [f for f in opened if os.fspath(f.name) == str(good)]
        assert len(good_handles) == 1, "the first file must have been opened"
        assert good_handles[0].closed, "already-opened file must be closed when a later open fails"

    def test_unlink_all_counts_deleted_and_ignores_missing(self, main: ModuleType, tmp_path: Path) -> None:
        a = tmp_path / "a.tmp"
        b = tmp_path / "b.tmp"
        a.write_text("", encoding="utf-8")
        b.write_text("", encoding="utf-8")
        assert main.unlink_all([a, b, tmp_path / "gone.tmp"]) == 2
        assert not a.exists() and not b.exists()

    def test_timer_elapsed_is_live_inside_and_frozen_after(self, main: ModuleType) -> None:
        clock = FakeClock()
        with main.timer("job", clock=clock) as t:
            assert t.label == "job"
            clock.now = 2.5
            assert t.elapsed == 2.5, "elapsed is live while the block runs"
            clock.now = 4.0
        clock.now = 100.0
        assert t.elapsed == 4.0, "elapsed must freeze when the block exits"

    def test_timer_reports_to_sink_even_when_block_raises(self, main: ModuleType) -> None:
        clock = FakeClock()
        events: list[tuple[str, float]] = []
        with pytest.raises(ValueError):
            with main.timer("job", clock=clock, sink=lambda label, seconds: events.append((label, seconds))):
                clock.now = 1.5
                raise ValueError("x")
        assert events == [("job", 1.5)]


class TestPart6ContextlibStdlib:
    """closing, nullcontext, redirect_stdout, chdir."""

    def test_with_resource_closes_after_consume(self, main: ModuleType) -> None:
        created: list[Recorder] = []

        def factory() -> Recorder:
            resource = Recorder()
            created.append(resource)
            return resource

        assert main.with_resource(factory, lambda r: "done") == "done"
        assert created[0].closed, "resource must be closed after consume returns"

    def test_with_resource_closes_when_consume_raises(self, main: ModuleType) -> None:
        resource = Recorder()

        def consume(_: Recorder) -> None:
            raise ValueError("fail")

        with pytest.raises(ValueError):
            main.with_resource(lambda: resource, consume)
        assert resource.closed

    def test_lock_or_null_returns_the_lock_when_given(self, main: ModuleType) -> None:
        lock = threading.Lock()
        ctx = main.lock_or_null(lock)
        assert ctx is lock
        with ctx:
            assert lock.locked()
        assert not lock.locked()

    def test_lock_or_null_none_is_a_noop_context(self, main: ModuleType) -> None:
        with main.lock_or_null(None):
            value = 1
        assert value == 1
        with pytest.raises(ZeroDivisionError):
            with main.lock_or_null(None):
                1 / 0

    def test_capture_stdout_returns_result_and_text(self, main: ModuleType) -> None:
        def emit(n: int) -> int:
            print("hello", n)
            return n * 2

        assert main.capture_stdout(emit, 3) == (6, "hello 3\n")

    def test_capture_stdout_restores_stdout_and_notes_partial_output(self, main: ModuleType) -> None:
        before = sys.stdout

        def boom() -> None:
            print("partial")
            raise ValueError("kaput")

        with pytest.raises(ValueError) as info:
            main.capture_stdout(boom)
        assert sys.stdout is before, "stdout must be restored after an error"
        notes = getattr(info.value, "__notes__", [])
        assert any("partial" in note for note in notes), f"captured text should be a note, got {notes!r}"

    def test_list_names_in_is_sorted_and_restores_cwd(self, main: ModuleType, tmp_path: Path) -> None:
        (tmp_path / "b.txt").write_text("x", encoding="utf-8")
        (tmp_path / "a.txt").write_text("", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        before = Path.cwd()
        assert main.list_names_in(tmp_path) == ["a.txt", "b.txt", "sub"]
        assert Path.cwd() == before

    def test_list_names_in_restores_cwd_on_error(self, main: ModuleType, tmp_path: Path) -> None:
        before = Path.cwd()
        with pytest.raises(FileNotFoundError):
            main.list_names_in(tmp_path / "missing")
        assert Path.cwd() == before


class TestPart7BatchRetry:
    """ExceptionGroup batches, except* handling, transient classification, retries."""

    def test_run_batch_returns_results_in_task_order(self, main: ModuleType) -> None:
        ran: list[str] = []
        results = main.run_batch([("a", _task(ran, "a", 1)), ("b", _task(ran, "b", 2))])
        assert results == {"a": 1, "b": 2}
        assert list(results) == ["a", "b"]

    def test_run_batch_runs_every_task_and_groups_failures(self, main: ModuleType) -> None:
        ran: list[str] = []
        tasks = [
            ("a", _task(ran, "a", 1)),
            ("b", _task(ran, "b", ValueError("bad"))),
            ("c", _task(ran, "c", 3)),
            ("d", _task(ran, "d", KeyError("missing"))),
        ]
        with pytest.raises(ExceptionGroup) as info:
            main.run_batch(tasks)
        assert ran == ["a", "b", "c", "d"], "a failure must not stop later tasks"
        group = info.value
        assert [type(e) for e in group.exceptions] == [ValueError, KeyError]
        assert "2 of 4" in group.message

    def test_run_batch_members_carry_task_name_note(self, main: ModuleType) -> None:
        ran: list[str] = []
        with pytest.raises(ExceptionGroup) as info:
            main.run_batch([("load", _task(ran, "load", ValueError("x")))])
        notes = getattr(info.value.exceptions[0], "__notes__", [])
        assert any("load" in note for note in notes), f"expected task name in notes, got {notes!r}"

    def test_run_batch_base_exception_stops_immediately(self, main: ModuleType) -> None:
        ran: list[str] = []
        tasks = [
            ("a", _task(ran, "a", 1)),
            ("b", _task(ran, "b", KeyboardInterrupt())),
            ("c", _task(ran, "c", 3)),
        ]
        with pytest.raises(KeyboardInterrupt):
            main.run_batch(tasks)
        assert ran == ["a", "b"], "BaseException must not be collected and must stop the batch"

    def test_run_batch_rejects_duplicate_names_before_running(self, main: ModuleType) -> None:
        ran: list[str] = []
        with pytest.raises(ValueError):
            main.run_batch([("a", _task(ran, "a", 1)), ("a", _task(ran, "a", 2))])
        assert ran == [], "validation must happen before any task runs"

    def test_caller_splits_group_with_except_star(self, main: ModuleType) -> None:
        ran: list[str] = []
        seen: list[tuple[str, int]] = []
        try:
            main.run_batch(
                [
                    ("a", _task(ran, "a", ValueError("v"))),
                    ("b", _task(ran, "b", KeyError("k"))),
                ]
            )
        except* ValueError as eg:
            seen.append(("value", len(eg.exceptions)))
        except* KeyError as eg:
            seen.append(("key", len(eg.exceptions)))
        assert seen == [("value", 1), ("key", 1)]

    def test_is_transient_classifies_errors(self, main: ModuleType) -> None:
        cases: list[tuple[BaseException, bool]] = [
            (TimeoutError("t"), True),
            (ConnectionResetError(), True),
            (InterruptedError(), True),
            (OSError(errno.EBUSY, "busy"), True),
            (FileNotFoundError("x"), False),
            (PermissionError("p"), False),
            (ValueError("v"), False),
        ]
        for exc, expected in cases:
            assert main.is_transient(exc) is expected, f"{type(exc).__name__} classified wrongly"

    def test_retry_call_recovers_with_exponential_backoff(self, main: ModuleType) -> None:
        calls: list[int] = []

        def flaky() -> str:
            calls.append(1)
            if len(calls) < 3:
                raise TimeoutError("slow")
            return "ok"

        sleeps: list[float] = []
        assert main.retry_call(flaky, attempts=4, base_delay=0.5, sleep=sleeps.append) == "ok"
        assert len(calls) == 3
        assert sleeps == [0.5, 1.0], "delays double after each transient failure"

    def test_retry_call_does_not_retry_permanent_errors(self, main: ModuleType) -> None:
        calls: list[int] = []

        def missing() -> None:
            calls.append(1)
            raise FileNotFoundError("gone")

        sleeps: list[float] = []
        with pytest.raises(FileNotFoundError):
            main.retry_call(missing, attempts=5, base_delay=0.5, sleep=sleeps.append)
        assert len(calls) == 1, "permanent errors must not be retried"
        assert sleeps == []

    def test_retry_call_exhaustion_raises_retry_exhausted_from_last_error(self, main: ModuleType) -> None:
        calls: list[int] = []

        def down() -> None:
            calls.append(1)
            raise ConnectionError("refused")

        sleeps: list[float] = []
        with pytest.raises(main.RetryExhausted) as info:
            main.retry_call(down, attempts=3, base_delay=0.5, sleep=sleeps.append)
        assert info.value.attempts == 3
        assert len(calls) == 3
        assert sleeps == [0.5, 1.0], "no sleep after the final attempt"
        assert isinstance(info.value.__cause__, ConnectionError)

    def test_retry_call_accepts_custom_classifier(self, main: ModuleType) -> None:
        calls: list[int] = []

        def lookup() -> str:
            calls.append(1)
            if len(calls) == 1:
                raise KeyError("cold")
            return "v"

        sleeps: list[float] = []
        result = main.retry_call(
            lookup,
            attempts=2,
            base_delay=0.1,
            sleep=sleeps.append,
            classify=lambda exc: isinstance(exc, KeyError),
        )
        assert result == "v"
        assert len(sleeps) == 1

    def test_triage_splits_group_into_transient_and_permanent(self, main: ModuleType) -> None:
        group = ExceptionGroup("batch", [TimeoutError("t"), ValueError("v"), ConnectionError("c")])
        transient, permanent = main.triage(group)
        assert transient is not None
        assert sorted(type(e).__name__ for e in transient.exceptions) == ["ConnectionError", "TimeoutError"]
        assert permanent is not None
        assert [type(e) for e in permanent.exceptions] == [ValueError]

        all_transient = ExceptionGroup("g", [TimeoutError()])
        _, none_left = main.triage(all_transient)
        assert none_left is None, "an empty side must be None"

