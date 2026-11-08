"""Tests for the backup/sync toolkit: scanning, diffing, JSON, CSV, gzip, TOML, struct."""

from __future__ import annotations

import gzip
import hashlib
import os
import struct
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import ModuleType
from uuid import UUID

import pytest

UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class Color(Enum):
    RED = "red"
    BLUE = "blue"


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry(main: ModuleType, data: bytes, mtime: datetime = EPOCH):
    return main.FileEntry(size=len(data), mtime=mtime, sha256=_sha(data))


# --------------------------------------------------------------------------- #
# Part 1: tree scan and hashing
# --------------------------------------------------------------------------- #


class TestPart1ScanAndHash:
    def test_sha256_file_matches_hashlib_for_any_chunk_size(self, main: ModuleType, tmp_path: Path) -> None:
        payload = bytes(range(256)) * 12_289 + b"tail"
        target = _write(tmp_path / "big.bin", payload)
        expected = _sha(payload)
        assert main.sha256_file(target) == expected, "default chunking produced a wrong digest"
        assert main.sha256_file(target, chunk_size=7) == expected, "tiny chunks must not change the digest"

    def test_sha256_file_rejects_non_positive_chunk_size(self, main: ModuleType, tmp_path: Path) -> None:
        target = _write(tmp_path / "a.bin", b"x")
        with pytest.raises(ValueError):
            main.sha256_file(target, chunk_size=0)

    def test_scan_keys_are_sorted_relative_posix_paths(self, main: ModuleType, tmp_path: Path) -> None:
        _write(tmp_path / "top.txt", b"t")
        _write(tmp_path / "pkg" / "sub" / "mod.py", b"m")
        _write(tmp_path / "Zed.txt", b"z")
        manifest = main.scan_tree(tmp_path)
        assert list(manifest) == ["Zed.txt", "pkg/sub/mod.py", "top.txt"], "keys must be sorted posix paths"

    def test_scan_entry_fields(self, main: ModuleType, tmp_path: Path) -> None:
        target = _write(tmp_path / "doc.txt", b"hello")
        mtime_ns = 1_700_000_000_123_456_789
        os.utime(target, ns=(mtime_ns, mtime_ns))
        entry = main.scan_tree(tmp_path)["doc.txt"]
        assert entry.size == 5, "size must be the byte count"
        assert entry.sha256 == _sha(b"hello"), "sha256 must be the hex digest of the content"
        assert entry.mtime == datetime(2023, 11, 14, 22, 13, 20, 123456, tzinfo=UTC), (
            "mtime must be aware UTC, floored to microseconds"
        )

    def test_scan_skips_directories_and_symlinks(self, main: ModuleType, tmp_path: Path) -> None:
        _write(tmp_path / "real" / "a.txt", b"a")
        (tmp_path / "empty_dir").mkdir()
        try:
            (tmp_path / "link.txt").symlink_to(tmp_path / "real" / "a.txt")
        except OSError:
            pass  # no symlink privilege here; the assertion still checks the regular files
        assert list(main.scan_tree(tmp_path)) == ["real/a.txt"], "only regular files belong in the manifest"

    def test_scan_bare_pattern_matches_at_any_depth(self, main: ModuleType, tmp_path: Path) -> None:
        for rel in ("a.tmp", "x/b.tmp", "x/keep.txt"):
            _write(tmp_path / rel, b"?")
        assert list(main.scan_tree(tmp_path, ignore=["*.tmp"])) == ["x/keep.txt"]

    def test_scan_component_pattern_prunes_whole_directory(self, main: ModuleType, tmp_path: Path) -> None:
        for rel in (
            "pkg/__pycache__/m.cpython-313.pyc",
            "pkg/__pycache__/n.pyc",
            "pkg/mod.py",
            "__pycache__/top.pyc",
        ):
            _write(tmp_path / rel, b"?")
        assert list(main.scan_tree(tmp_path, ignore=["__pycache__"])) == ["pkg/mod.py"]

    def test_scan_slash_pattern_is_anchored_to_root(self, main: ModuleType, tmp_path: Path) -> None:
        for rel in ("logs/a.log", "logs/deep/b.log", "other/logs/c.log", "src/main.py"):
            _write(tmp_path / rel, b"?")
        assert list(main.scan_tree(tmp_path, ignore=["logs/**"])) == ["other/logs/c.log", "src/main.py"]

    def test_scan_rejects_missing_and_non_directory_roots(self, main: ModuleType, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            main.scan_tree(tmp_path / "nope")
        file_root = _write(tmp_path / "file.txt", b"x")
        with pytest.raises(NotADirectoryError):
            main.scan_tree(file_root)


# --------------------------------------------------------------------------- #
# Part 2: diff and apply
# --------------------------------------------------------------------------- #


class TestPart2DiffAndApply:
    def test_diff_classifies_added_removed_modified(self, main: ModuleType) -> None:
        old = {"a.txt": _entry(main, b"1"), "b.txt": _entry(main, b"2"), "gone.txt": _entry(main, b"3")}
        new = {"a.txt": _entry(main, b"1"), "b.txt": _entry(main, b"22"), "new/c.txt": _entry(main, b"4")}
        diff = main.diff_manifests(old, new)
        assert diff.added == ("new/c.txt",)
        assert diff.removed == ("gone.txt",)
        assert diff.modified == ("b.txt",)
        assert not diff.is_empty

    def test_diff_ignores_mtime_only_changes(self, main: ModuleType) -> None:
        old = {"a": _entry(main, b"x", EPOCH)}
        new = {"a": _entry(main, b"x", EPOCH + timedelta(hours=1))}
        assert main.diff_manifests(old, new).is_empty, "same size and digest is not a modification"

    def test_diff_of_empty_and_unsorted_inputs(self, main: ModuleType) -> None:
        assert main.diff_manifests({}, {}).is_empty
        new = {"z": _entry(main, b"z"), "m": _entry(main, b"m"), "a": _entry(main, b"a")}
        assert main.diff_manifests({}, new).added == ("a", "m", "z"), "tuples must be sorted"

    def test_apply_diff_copies_added_and_modified_and_keeps_mtime(self, main: ModuleType, tmp_path: Path) -> None:
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        _write(src / "new" / "c.txt", b"c")
        changed = _write(src / "b.txt", b"22")
        _write(dst / "b.txt", b"2")
        stamp = 1_600_000_000_000_000_000
        os.utime(changed, ns=(stamp, stamp))

        diff = main.ManifestDiff(added=("new/c.txt",), modified=("b.txt",))
        result = main.apply_diff(diff, src, dst)

        assert set(result.copied) == {"new/c.txt", "b.txt"}
        assert result.deleted == ()
        assert (dst / "new" / "c.txt").read_bytes() == b"c", "added file must be copied, parents created"
        assert (dst / "b.txt").read_bytes() == b"22", "modified file must be overwritten"
        assert (dst / "b.txt").stat().st_mtime_ns == stamp, "copy must preserve the source mtime"

    def test_apply_diff_removes_files_prunes_empty_parents_but_not_root(self, main: ModuleType, tmp_path: Path) -> None:
        dst = tmp_path / "dst"
        src = tmp_path / "src"
        src.mkdir()
        _write(dst / "a" / "b" / "c.txt", b"1")
        _write(dst / "a" / "keep" / "d.txt", b"2")
        _write(dst / "top.txt", b"3")

        first = main.apply_diff(main.ManifestDiff(removed=("a/b/c.txt", "never-existed.txt")), src, dst)
        assert first.deleted == ("a/b/c.txt",), "only files actually removed are reported"
        assert not (dst / "a" / "b").exists(), "emptied directory must be pruned"
        assert (dst / "a" / "keep" / "d.txt").exists(), "non-empty parents must survive"

        second = main.apply_diff(main.ManifestDiff(removed=("a/keep/d.txt",)), src, dst)
        assert second.deleted == ("a/keep/d.txt",)
        assert not (dst / "a").exists(), "chain of emptied directories must be pruned"
        assert (dst / "top.txt").exists() and dst.is_dir(), "destination root is never removed"

    def test_apply_diff_rejects_escaping_paths_before_touching_disk(self, main: ModuleType, tmp_path: Path) -> None:
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        _write(src / "ok.txt", b"ok")
        for bad in ("../escape.txt", "a/../../escape.txt", "/abs.txt"):
            with pytest.raises(ValueError):
                main.apply_diff(main.ManifestDiff(added=("ok.txt", bad)), src, dst)
            assert not (dst / "ok.txt").exists(), f"nothing may be copied when {bad!r} is in the diff"


# --------------------------------------------------------------------------- #
# Part 3: JSON with custom types, atomic writes
# --------------------------------------------------------------------------- #


class TestPart3JsonAndAtomicWrites:
    def test_datetime_round_trip_keeps_offset_and_microseconds(self, main: ModuleType) -> None:
        value = datetime(2026, 11, 8, 9, 30, 15, 123456, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        back = main.decode_json(main.encode_json({"when": value}))["when"]
        assert back == value
        assert back.utcoffset() == value.utcoffset(), "UTC offset must survive the round trip"

    def test_decimal_and_uuid_round_trip_exactly(self, main: ModuleType) -> None:
        identifier = UUID("12345678-1234-5678-1234-567812345678")
        text = main.encode_json({"amount": Decimal("12345.6789000"), "id": identifier})
        back = main.decode_json(text)
        assert str(back["amount"]) == "12345.6789000", "Decimal must not pass through float"
        assert back["id"] == identifier

    def test_dataclass_and_enum_round_trip_with_registered_types(self, main: ModuleType) -> None:
        manifest = {"a.txt": main.FileEntry(size=1, mtime=EPOCH, sha256="ab")}
        payload = {"kind": Color.BLUE, "manifest": manifest}
        back = main.decode_json(main.encode_json(payload), types=(main.FileEntry, Color))
        assert back["kind"] is Color.BLUE
        assert back["manifest"] == manifest, "nested dataclass with datetime must rebuild exactly"

    def test_unregistered_type_names_raise_lookup_error(self, main: ModuleType) -> None:
        text = main.encode_json({"kind": Color.RED})
        with pytest.raises(LookupError):
            main.decode_json(text)
        with pytest.raises(LookupError):
            main.decode_json(text, types=(main.FileEntry,))

    def test_plain_json_data_round_trips_and_keys_are_sorted(self, main: ModuleType) -> None:
        data = {"b": [1, 2.5, None, True], "a": {"nested": "x"}}
        text = main.encode_json(data)
        assert main.decode_json(text) == data
        assert text.index('"a"') < text.index('"b"'), "keys must be sorted for stable diffs"

    def test_encode_rejects_naive_datetime_unsupported_types_and_reserved_key(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.encode_json({"when": datetime(2024, 1, 1)})
        with pytest.raises(TypeError):
            main.encode_json({"x": object()})
        with pytest.raises(ValueError):
            main.encode_json({"__type__": "datetime", "value": "2024-01-01T00:00:00+00:00"})

    def test_save_and_load_json_round_trip_utf8(self, main: ModuleType, tmp_path: Path) -> None:
        target = tmp_path / "manifest.json"
        data = {"résumé/日本.txt": main.FileEntry(size=3, mtime=EPOCH, sha256="00")}
        main.save_json_atomic(data, target)
        raw = target.read_bytes().decode("utf-8")
        assert "日本" in raw, "non-ASCII text must be written as UTF-8"
        assert raw.endswith("\n"), "file must end with a newline"
        assert main.load_json(target, types=(main.FileEntry,)) == data

    def test_failed_save_keeps_previous_file_and_leaves_no_temp_files(self, main: ModuleType, tmp_path: Path) -> None:
        target = tmp_path / "manifest.json"
        main.save_json_atomic({"ok": 1}, target)
        before = target.read_bytes()
        with pytest.raises(TypeError):
            main.save_json_atomic({"bad": object()}, target)
        assert target.read_bytes() == before, "an encoding failure must not touch the destination"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["manifest.json"], "no temp files may remain"

    def test_write_bytes_atomic_stages_beside_target_then_replaces(
        self, main: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_replace = os.replace
        calls: list[tuple[Path, Path]] = []

        def spy(src: str, dst: str) -> None:
            calls.append((Path(src), Path(dst)))
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", spy)
        target = tmp_path / "data.bin"
        main.write_bytes_atomic(target, b"payload")

        assert len(calls) == 1, "exactly one os.replace must publish the file"
        staged, published = calls[0]
        assert staged.parent == tmp_path, "temp file must live in the destination directory"
        assert published == target
        assert target.read_bytes() == b"payload"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["data.bin"]

    def test_write_bytes_atomic_failure_keeps_target_and_removes_temp(
        self, main: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = _write(tmp_path / "data.bin", b"old")

        def boom(src: str, dst: str) -> None:
            raise OSError("disk exploded")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError, match="disk exploded"):
            main.write_bytes_atomic(target, b"new")
        assert target.read_bytes() == b"old", "original file must survive a failed publish"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["data.bin"], "staged temp file must be removed"


# --------------------------------------------------------------------------- #
# Part 4: CSV report
# --------------------------------------------------------------------------- #


class TestPart4CsvReport:
    def test_export_uses_semicolons_lf_and_sorted_rows(self, main: ModuleType, tmp_path: Path) -> None:
        manifest = {"b.txt": _entry(main, b"bb"), "a.txt": _entry(main, b"a")}
        out = tmp_path / "report.csv"
        main.export_report(manifest, out)
        raw = out.read_bytes()
        assert raw.startswith(b"path;size;mtime;sha256;note\n"), "header uses ';' and LF, no BOM"
        assert b"\r" not in raw, "lineterminator must be LF"
        rows = main.import_report(out)
        assert [r["path"] for r in rows] == ["a.txt", "b.txt"], "rows must be sorted by path"

    def test_tricky_values_round_trip(self, main: ModuleType, tmp_path: Path) -> None:
        manifest = {
            "a,b.txt": _entry(main, b"1"),
            "résumé 日本.txt": _entry(main, b"2"),
        }
        notes = {
            "a,b.txt": "fixed, retry; see ticket",
            "résumé 日本.txt": 'he said "hi"\nthen left — ✓',
        }
        out = tmp_path / "report.csv"
        main.export_report(manifest, out, notes=notes)
        by_path = {row["path"]: row for row in main.import_report(out)}
        assert by_path["a,b.txt"]["note"] == "fixed, retry; see ticket"
        assert by_path["résumé 日本.txt"]["note"] == 'he said "hi"\nthen left — ✓', (
            "embedded quotes and newlines must survive"
        )

    def test_raw_quoting_follows_csv_rules_for_custom_delimiter(self, main: ModuleType, tmp_path: Path) -> None:
        manifest = {"f.txt": _entry(main, b"f")}
        out = tmp_path / "report.csv"
        main.export_report(manifest, out, notes={"f.txt": 'say "hi"; ok'})
        raw = out.read_bytes()
        assert b'"say ""hi"""' not in raw, "wrong quoting: value must be quoted with doubled quotes"
        assert b'"say ""hi""; ok"' in raw, "value containing the delimiter and quotes must be quoted"
        out2 = tmp_path / "plain.csv"
        main.export_report(manifest, out2, notes={"f.txt": "fixed, retry"})
        assert out2.read_bytes().endswith(b";fixed, retry\n"), "commas are not special under ';'"

    def test_defaults_and_numeric_columns(self, main: ModuleType, tmp_path: Path) -> None:
        out = tmp_path / "report.csv"
        main.export_report({"f.txt": _entry(main, b"hello")}, out)
        (row,) = main.import_report(out)
        assert row["note"] == "", "missing notes export as empty strings"
        assert row["size"] == "5", "size is written as a plain decimal integer"

    def test_mtime_column_is_iso8601_round_trip(self, main: ModuleType, tmp_path: Path) -> None:
        mtime = datetime(2025, 3, 1, 12, 0, 0, 500, tzinfo=UTC)
        out = tmp_path / "report.csv"
        main.export_report({"f.txt": _entry(main, b"x", mtime)}, out)
        (row,) = main.import_report(out)
        assert datetime.fromisoformat(row["mtime"]) == mtime

    def test_import_rejects_wrong_header_and_export_rejects_unknown_note_keys(
        self, main: ModuleType, tmp_path: Path
    ) -> None:
        bad = _write(tmp_path / "bad.csv", b"name;size\nx;1\n")
        with pytest.raises(ValueError):
            main.import_report(bad)
        with pytest.raises(ValueError):
            main.export_report({}, tmp_path / "r.csv", notes={"ghost.txt": "boo"})


# --------------------------------------------------------------------------- #
# Part 5: streaming gzip
# --------------------------------------------------------------------------- #


class TestPart5Gzip:
    def test_round_trip_streams_large_payload(self, main: ModuleType, tmp_path: Path) -> None:
        payload = bytes(range(256)) * 9_000 + b"end"
        src = _write(tmp_path / "data.bin", payload)
        archive = tmp_path / "data.bin.gz"
        restored = tmp_path / "restored.bin"
        assert main.compress_file(src, archive) == len(payload), "returns uncompressed byte count"
        assert archive.stat().st_size < len(payload), "repetitive input should shrink"
        assert main.decompress_file(archive, restored) == len(payload)
        assert restored.read_bytes() == payload

    def test_compression_levels_extremes_round_trip(self, main: ModuleType, tmp_path: Path) -> None:
        payload = b"level-check " * 4000
        src = _write(tmp_path / "s.bin", payload)
        for level in (1, 9):
            archive = tmp_path / f"s{level}.gz"
            main.compress_file(src, archive, level=level)
            out = tmp_path / f"s{level}.out"
            main.decompress_file(archive, out)
            assert out.read_bytes() == payload, f"level {level} must round-trip"

    def test_output_is_reproducible_without_name_or_mtime(self, main: ModuleType, tmp_path: Path) -> None:
        data = b"same bytes" * 1000
        first = _write(tmp_path / "one.txt", data)
        second = _write(tmp_path / "two.txt", data)
        a = tmp_path / "a.gz"
        b = tmp_path / "b.gz"
        main.compress_file(first, a)
        main.compress_file(second, b)
        raw = a.read_bytes()
        assert raw == b.read_bytes(), "same content must compress to identical bytes"
        assert raw[:2] == b"\x1f\x8b", "gzip magic"
        assert raw[3] & 0x08 == 0, "FNAME flag must be clear: no embedded filename"
        assert raw[4:8] == b"\x00\x00\x00\x00", "MTIME header field must be zero"

    def test_corrupt_or_truncated_input_raises_value_error_and_removes_output(
        self, main: ModuleType, tmp_path: Path
    ) -> None:
        src = _write(tmp_path / "s.bin", b"x" * 100_000)
        good = tmp_path / "good.gz"
        main.compress_file(src, good)
        raw = good.read_bytes()
        cases = {"garbage.gz": b"definitely not gzip", "truncated.gz": raw[: len(raw) // 2]}
        for name, data in cases.items():
            bad = _write(tmp_path / name, data)
            out = tmp_path / f"{name}.out"
            with pytest.raises(ValueError):
                main.decompress_file(bad, out)
            assert not out.exists(), f"{name}: partial output must be removed"

    def test_verify_gzip_reports_validity_without_writing_output(self, main: ModuleType, tmp_path: Path) -> None:
        src = _write(tmp_path / "s.bin", b"payload" * 5000)
        archive = tmp_path / "s.gz"
        main.compress_file(src, archive)
        assert main.verify_gzip(archive) is True
        truncated = _write(tmp_path / "t.gz", archive.read_bytes()[:-10])
        assert main.verify_gzip(truncated) is False, "truncated stream must fail verification"
        assert main.verify_gzip(_write(tmp_path / "g.gz", b"nope")) is False
        assert main.verify_gzip(_write(tmp_path / "e.gz", b"")) is False, "empty file is not a gzip stream"
        assert gzip.decompress(archive.read_bytes()) == b"payload" * 5000, "archive itself must be intact"

    def test_verify_gzip_missing_file_raises(self, main: ModuleType, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            main.verify_gzip(tmp_path / "absent.gz")


# --------------------------------------------------------------------------- #
# Part 6: TOML settings
# --------------------------------------------------------------------------- #


def _config(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "config" / "sync.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(body, encoding="utf-8")
    return cfg


class TestPart6TomlSettings:
    def test_relative_paths_resolve_against_config_file_directory(self, main: ModuleType, tmp_path: Path) -> None:
        cfg = _config(
            tmp_path,
            """
[sync]
source = "data/src"
destination = "backup"
ignore = ["*.tmp", ".git/**"]
retention_days = 14
compression_level = 9
""",
        )
        settings = main.load_settings(cfg)
        assert settings.source == cfg.parent / "data" / "src"
        assert settings.destination == cfg.parent / "backup"
        assert settings.ignore == ("*.tmp", ".git/**")
        assert settings.retention == timedelta(days=14)
        assert settings.compression_level == 9

    def test_absolute_paths_are_kept(self, main: ModuleType, tmp_path: Path) -> None:
        absolute = (tmp_path / "elsewhere").as_posix()
        cfg = _config(tmp_path, f'[sync]\nsource = "{absolute}"\ndestination = "{absolute}"\n')
        settings = main.load_settings(cfg)
        assert settings.source == tmp_path / "elsewhere"
        assert settings.destination == tmp_path / "elsewhere"

    def test_defaults_apply_when_optional_keys_are_missing(self, main: ModuleType, tmp_path: Path) -> None:
        cfg = _config(tmp_path, '[sync]\nsource = "s"\ndestination = "d"\n')
        settings = main.load_settings(cfg)
        assert settings.ignore == ()
        assert settings.retention == timedelta(days=30)
        assert settings.compression_level == 6

    def test_missing_required_key_is_named_in_error(self, main: ModuleType, tmp_path: Path) -> None:
        cfg = _config(tmp_path, '[sync]\ndestination = "d"\n')
        with pytest.raises(ValueError, match="source"):
            main.load_settings(cfg)

    def test_unknown_key_is_rejected_to_catch_typos(self, main: ModuleType, tmp_path: Path) -> None:
        cfg = _config(tmp_path, '[sync]\nsource = "s"\ndestination = "d"\nsorce = "typo"\n')
        with pytest.raises(ValueError, match="sorce"):
            main.load_settings(cfg)

    def test_booleans_are_not_integers(self, main: ModuleType, tmp_path: Path) -> None:
        cfg = _config(tmp_path, '[sync]\nsource = "s"\ndestination = "d"\ncompression_level = true\n')
        with pytest.raises(ValueError, match="compression_level"):
            main.load_settings(cfg)
        cfg2 = _config(tmp_path / "other", '[sync]\nsource = "s"\ndestination = "d"\nretention_days = true\n')
        with pytest.raises(ValueError, match="retention_days"):
            main.load_settings(cfg2)

    def test_numeric_ranges_and_list_types_are_validated(self, main: ModuleType, tmp_path: Path) -> None:
        base = '[sync]\nsource = "s"\ndestination = "d"\n'
        for extra in ("compression_level = 0\n", "compression_level = 10\n"):
            with pytest.raises(ValueError, match="compression_level"):
                main.load_settings(_config(tmp_path / extra.strip(), base + extra))
        with pytest.raises(ValueError, match="ignore"):
            main.load_settings(_config(tmp_path / "ign1", base + 'ignore = "*.tmp"\n'))
        with pytest.raises(ValueError, match="ignore"):
            main.load_settings(_config(tmp_path / "ign2", base + "ignore = [1]\n"))

    def test_missing_table_and_malformed_toml_are_value_errors(self, main: ModuleType, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            main.load_settings(_config(tmp_path / "no_table", 'other = "x"\n'))
        with pytest.raises(ValueError):
            main.load_settings(_config(tmp_path / "broken", "[sync\nsource ="))


# --------------------------------------------------------------------------- #
# Part 7: versioned binary snapshot header
# --------------------------------------------------------------------------- #


class TestPart7SnapshotHeader:
    def test_round_trip_preserves_header_fields_and_payload(self, main: ModuleType, tmp_path: Path) -> None:
        created = datetime(2026, 11, 8, 12, 0, 0, 654321, tzinfo=UTC)
        target = tmp_path / "snap.bin"
        main.write_snapshot(target, entry_count=42, created_at=created, payload=b"payload-bytes", flags=3)
        snap = main.read_snapshot(target)
        assert snap.version == 2
        assert snap.flags == 3
        assert snap.entry_count == 42
        assert snap.created_at == created, "created_at must keep microseconds"
        assert snap.payload == b"payload-bytes"

    def test_on_disk_layout_is_big_endian_and_fixed_width(self, main: ModuleType, tmp_path: Path) -> None:
        target = tmp_path / "snap.bin"
        main.write_snapshot(target, entry_count=42, created_at=EPOCH, payload=b"XYZ")
        raw = target.read_bytes()
        assert raw[:4] == b"PYSN", "magic occupies the first four bytes"
        assert raw[4:6] == b"\x00\x02", "version is big-endian uint16"
        assert struct.unpack(">I", raw[8:12])[0] == 42, "entry_count is big-endian uint32 at offset 8"
        assert len(raw) == 20 + 3, "v2 header is 20 bytes followed by the payload"
        assert raw[-3:] == b"XYZ"

    def test_bad_magic_is_rejected(self, main: ModuleType, tmp_path: Path) -> None:
        target = _write(tmp_path / "bad.bin", struct.pack(">4sHHIq", b"NOPE", 2, 0, 0, 0))
        with pytest.raises(main.SnapshotError, match="magic"):
            main.read_snapshot(target)

    def test_unsupported_version_raises_specific_error(self, main: ModuleType, tmp_path: Path) -> None:
        target = _write(tmp_path / "future.bin", struct.pack(">4sHHIq", b"PYSN", 3, 0, 0, 0))
        with pytest.raises(main.UnsupportedVersionError):
            main.read_snapshot(target)
        assert issubclass(main.UnsupportedVersionError, main.SnapshotError)

    def test_reads_legacy_version_one_header_without_timestamp(self, main: ModuleType, tmp_path: Path) -> None:
        target = _write(tmp_path / "v1.bin", struct.pack(">4sHHI", b"PYSN", 1, 7, 9) + b"legacy")
        snap = main.read_snapshot(target)
        assert snap.version == 1
        assert snap.flags == 7
        assert snap.entry_count == 9
        assert snap.created_at is None, "v1 carries no timestamp"
        assert snap.payload == b"legacy"

    def test_truncated_and_empty_files_are_rejected(self, main: ModuleType, tmp_path: Path) -> None:
        with pytest.raises(main.SnapshotError):
            main.read_snapshot(_write(tmp_path / "empty.bin", b""))
        full = struct.pack(">4sHHIq", b"PYSN", 2, 0, 1, 0)
        with pytest.raises(main.SnapshotError, match="truncated"):
            main.read_snapshot(_write(tmp_path / "short.bin", full[:15]))

    def test_write_rejects_naive_timestamp_and_out_of_range_counts(self, main: ModuleType, tmp_path: Path) -> None:
        target = tmp_path / "snap.bin"
        with pytest.raises(ValueError):
            main.write_snapshot(target, entry_count=1, created_at=datetime(2024, 1, 1), payload=b"")
        with pytest.raises(ValueError):
            main.write_snapshot(target, entry_count=2**32, created_at=EPOCH, payload=b"")
        assert not target.exists(), "rejected writes must not create the file"
