"""Files & serialization: reference backup/sync toolkit."""

from __future__ import annotations

import contextlib
import csv
import dataclasses
import gzip
import hashlib
import json
import os
import shutil
import struct
import tempfile
import tomllib
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

_CHUNK = 64 * 1024
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Part 1: tree scan and hashing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FileEntry:
    """One manifest row: size, UTC mtime (microseconds), and content digest."""

    size: int
    mtime: datetime
    sha256: str


Manifest = dict[str, FileEntry]


def sha256_file(path: Path, chunk_size: int = _CHUNK) -> str:
    """Hex SHA-256 of *path*, streamed in ``chunk_size`` pieces."""
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be positive, got {chunk_size}")
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _is_ignored(rel: PurePosixPath, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        if "/" in pattern:
            # Anchored: the pattern must describe the whole relative path.
            if rel.full_match(pattern):
                return True
        elif any(PurePosixPath(part).full_match(pattern) for part in rel.parts):
            # Unanchored: any component (file or directory name) at any depth.
            return True
    return False


def scan_tree(root: Path, ignore: Sequence[str] = ()) -> Manifest:
    """Build a manifest of regular files under *root*, skipping symlinks and ignored paths."""
    if not root.exists():
        raise FileNotFoundError(f"no such directory: {root}")
    if not root.is_dir():
        raise NotADirectoryError(str(root))

    manifest: Manifest = {}
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        rel = PurePosixPath(path.relative_to(root).as_posix())
        if _is_ignored(rel, ignore):
            continue
        st = path.stat()
        mtime = _EPOCH + timedelta(microseconds=st.st_mtime_ns // 1000)
        manifest[rel.as_posix()] = FileEntry(
            size=st.st_size,
            mtime=mtime,
            sha256=sha256_file(path),
        )
    return dict(sorted(manifest.items()))


# --------------------------------------------------------------------------- #
# Part 2: diff and apply
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ManifestDiff:
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.modified)


def diff_manifests(old: Manifest, new: Manifest) -> ManifestDiff:
    """Compare two manifests by content identity; mtime-only changes are ignored."""
    added = sorted(new.keys() - old.keys())
    removed = sorted(old.keys() - new.keys())
    modified = sorted(
        key
        for key in old.keys() & new.keys()
        if (old[key].size, old[key].sha256) != (new[key].size, new[key].sha256)
    )
    return ManifestDiff(tuple(added), tuple(removed), tuple(modified))


@dataclass(frozen=True, slots=True)
class ApplyResult:
    copied: tuple[str, ...]
    deleted: tuple[str, ...]


def _safe_relative(key: str) -> PurePosixPath:
    rel = PurePosixPath(key)
    if rel.is_absolute() or not rel.parts or ".." in rel.parts:
        raise ValueError(f"unsafe manifest path: {key!r}")
    return rel


def _prune_empty_parents(directory: Path, root: Path) -> None:
    while directory != root and directory.is_relative_to(root):
        try:
            directory.rmdir()
        except OSError:  # not empty, or already gone: stop climbing
            return
        directory = directory.parent


def apply_diff(diff: ManifestDiff, source: Path, dest: Path) -> ApplyResult:
    """Copy added/modified files from *source* to *dest* and remove the removed ones."""
    for key in (*diff.added, *diff.modified, *diff.removed):
        _safe_relative(key)  # validate everything before touching disk

    copied: list[str] = []
    for key in (*diff.added, *diff.modified):
        rel = _safe_relative(key)
        target = dest.joinpath(*rel.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source.joinpath(*rel.parts), target)
        copied.append(key)

    deleted: list[str] = []
    for key in diff.removed:
        target = dest.joinpath(*_safe_relative(key).parts)
        try:
            target.unlink()
        except FileNotFoundError:
            continue
        deleted.append(key)
        _prune_empty_parents(target.parent, dest)

    return ApplyResult(tuple(copied), tuple(deleted))


# --------------------------------------------------------------------------- #
# Part 3: JSON with custom types, atomic writes
# --------------------------------------------------------------------------- #

_TYPE_KEY = "__type__"


def _reject_reserved_keys(obj: object) -> None:
    if isinstance(obj, dict):
        if _TYPE_KEY in obj:
            raise ValueError(f"{_TYPE_KEY!r} is reserved for type tags")
        for value in obj.values():
            _reject_reserved_keys(value)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _reject_reserved_keys(item)
    elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for field in dataclasses.fields(obj):
            _reject_reserved_keys(getattr(obj, field.name))


class _TaggingEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, datetime):
            if o.utcoffset() is None:
                raise ValueError("naive datetime cannot be serialised; attach a timezone")
            return {_TYPE_KEY: "datetime", "value": o.isoformat()}
        if isinstance(o, Decimal):
            return {_TYPE_KEY: "Decimal", "value": str(o)}
        if isinstance(o, UUID):
            return {_TYPE_KEY: "UUID", "value": str(o)}
        if isinstance(o, Enum):
            return {_TYPE_KEY: "enum", "class": type(o).__name__, "value": o.value}
        if dataclasses.is_dataclass(o) and not isinstance(o, type):
            return {
                _TYPE_KEY: "dataclass",
                "class": type(o).__name__,
                "fields": {f.name: getattr(o, f.name) for f in dataclasses.fields(o)},
            }
        return super().default(o)


def encode_json(obj: object) -> str:
    _reject_reserved_keys(obj)
    return json.dumps(obj, cls=_TaggingEncoder, sort_keys=True, indent=2, ensure_ascii=False)


def decode_json(text: str, types: Sequence[type] = ()) -> Any:
    registry = {t.__name__: t for t in types}

    def hook(obj: dict[str, Any]) -> Any:
        tag = obj.get(_TYPE_KEY)
        if tag is None:
            return obj
        match tag:
            case "datetime":
                return datetime.fromisoformat(obj["value"])
            case "Decimal":
                return Decimal(obj["value"])
            case "UUID":
                return UUID(obj["value"])
            case "enum":
                cls = _lookup(registry, obj["class"])
                return cls(obj["value"])
            case "dataclass":
                cls = _lookup(registry, obj["class"])
                return cls(**obj["fields"])
            case _:
                raise ValueError(f"unknown type tag {tag!r}")

    return json.loads(text, object_hook=hook)


def _lookup(registry: Mapping[str, type], name: str) -> type:
    try:
        return registry[name]
    except KeyError:
        raise LookupError(f"no type registered for {name!r}") from None


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Stage *data* beside *path*, fsync, then atomically replace *path*."""
    staged: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as tmp:
            staged = tmp.name
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(staged, path)
    except BaseException:
        if staged is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(staged)
        raise


def save_json_atomic(obj: object, path: Path) -> None:
    write_bytes_atomic(path, (encode_json(obj) + "\n").encode("utf-8"))


def load_json(path: Path, types: Sequence[type] = ()) -> Any:
    return decode_json(path.read_text(encoding="utf-8"), types)


# --------------------------------------------------------------------------- #
# Part 4: CSV report
# --------------------------------------------------------------------------- #


class ReportDialect(csv.excel):
    delimiter = ";"
    lineterminator = "\n"


REPORT_FIELDS = ("path", "size", "mtime", "sha256", "note")


def export_report(
    manifest: Manifest,
    path: Path,
    notes: Mapping[str, str] | None = None,
) -> None:
    notes = dict(notes or {})
    unknown = notes.keys() - manifest.keys()
    if unknown:
        raise ValueError(f"notes for unknown paths: {sorted(unknown)}")
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=REPORT_FIELDS, dialect=ReportDialect)
        writer.writeheader()
        for key in sorted(manifest):
            entry = manifest[key]
            writer.writerow(
                {
                    "path": key,
                    "size": entry.size,
                    "mtime": entry.mtime.isoformat(),
                    "sha256": entry.sha256,
                    "note": notes.get(key, ""),
                }
            )


def import_report(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, dialect=ReportDialect)
        if tuple(reader.fieldnames or ()) != REPORT_FIELDS:
            raise ValueError(f"unexpected report header: {reader.fieldnames!r}")
        return list(reader)


# --------------------------------------------------------------------------- #
# Part 5: streaming gzip
# --------------------------------------------------------------------------- #


def compress_file(src: Path, dst: Path, *, level: int = 6) -> int:
    total = 0
    with src.open("rb") as fin, dst.open("wb") as raw:
        # filename="" drops the embedded name; mtime=0 keeps output reproducible.
        with gzip.GzipFile(
            filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=level
        ) as gz:
            while chunk := fin.read(_CHUNK):
                gz.write(chunk)
                total += len(chunk)
    return total


def decompress_file(src: Path, dst: Path) -> int:
    total = 0
    try:
        with src.open("rb") as raw, dst.open("wb") as out:
            with gzip.GzipFile(fileobj=raw, mode="rb") as gz:
                while chunk := gz.read(_CHUNK):
                    out.write(chunk)
                    total += len(chunk)
    except (EOFError, zlib.error, gzip.BadGzipFile) as exc:
        dst.unlink(missing_ok=True)
        raise ValueError(f"corrupt or truncated gzip: {src}") from exc
    return total


def verify_gzip(path: Path) -> bool:
    with path.open("rb") as raw:
        if raw.read(1) == b"":
            return False
        raw.seek(0)
        try:
            with gzip.GzipFile(fileobj=raw, mode="rb") as gz:
                while gz.read(_CHUNK):
                    pass
        except (EOFError, zlib.error, gzip.BadGzipFile):
            return False
    return True


# --------------------------------------------------------------------------- #
# Part 6: TOML settings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Settings:
    source: Path
    destination: Path
    ignore: tuple[str, ...]
    retention: timedelta
    compression_level: int


_SYNC_KEYS = frozenset(
    {"source", "destination", "ignore", "retention_days", "compression_level"}
)


def _required_str(section: Mapping[str, Any], key: str) -> str:
    if key not in section:
        raise ValueError(f"[sync].{key} is required")
    value = section[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"[sync].{key} must be a non-empty string")
    return value


def _int_setting(section: Mapping[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"[sync].{key} must be an integer, got {value!r}")
    if not low <= value <= high:
        raise ValueError(f"[sync].{key} must be between {low} and {high}, got {value}")
    return value


def _ignore_list(section: Mapping[str, Any]) -> tuple[str, ...]:
    value = section.get("ignore", [])
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        raise ValueError("[sync].ignore must be a list of strings")
    return tuple(value)


def load_settings(path: Path) -> Settings:
    with path.open("rb") as fh:
        document = tomllib.load(fh)
    section = document.get("sync")
    if not isinstance(section, dict):
        raise ValueError("config must contain a [sync] table")
    unknown = set(section) - _SYNC_KEYS
    if unknown:
        raise ValueError(f"unknown keys in [sync]: {sorted(unknown)}")

    base = path.parent
    return Settings(
        source=base / _required_str(section, "source"),
        destination=base / _required_str(section, "destination"),
        ignore=_ignore_list(section),
        retention=timedelta(days=_int_setting(section, "retention_days", 30, 0, 36_500)),
        compression_level=_int_setting(section, "compression_level", 6, 1, 9),
    )


# --------------------------------------------------------------------------- #
# Part 7: versioned binary snapshot header
# --------------------------------------------------------------------------- #

SNAPSHOT_MAGIC = b"PYSN"
SNAPSHOT_VERSION = 2
_HEADER_V1 = struct.Struct(">4sHHI")
_HEADER_V2 = struct.Struct(">4sHHIq")


class SnapshotError(ValueError):
    """Malformed snapshot file."""


class UnsupportedVersionError(SnapshotError):
    """Snapshot written by a format version this build does not understand."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    version: int
    flags: int
    entry_count: int
    created_at: datetime | None
    payload: bytes


def write_snapshot(
    path: Path,
    *,
    entry_count: int,
    created_at: datetime,
    payload: bytes,
    flags: int = 0,
) -> None:
    if created_at.utcoffset() is None:
        raise ValueError("created_at must be timezone-aware")
    if not 0 <= entry_count <= 0xFFFF_FFFF:
        raise ValueError(f"entry_count out of u32 range: {entry_count}")
    if not 0 <= flags <= 0xFFFF:
        raise ValueError(f"flags out of u16 range: {flags}")
    micros = (created_at - _EPOCH) // timedelta(microseconds=1)
    header = _HEADER_V2.pack(SNAPSHOT_MAGIC, SNAPSHOT_VERSION, flags, entry_count, micros)
    write_bytes_atomic(path, header + payload)


def read_snapshot(path: Path) -> Snapshot:
    data = path.read_bytes()
    if len(data) < _HEADER_V1.size:
        raise SnapshotError("truncated header")
    magic, version, flags, entry_count = _HEADER_V1.unpack_from(data)
    if magic != SNAPSHOT_MAGIC:
        raise SnapshotError(f"bad magic: {magic!r}")
    if version == 1:
        return Snapshot(1, flags, entry_count, None, data[_HEADER_V1.size :])
    if version == 2:
        if len(data) < _HEADER_V2.size:
            raise SnapshotError("truncated header")
        micros = _HEADER_V2.unpack_from(data)[4]
        created = _EPOCH + timedelta(microseconds=micros)
        return Snapshot(2, flags, entry_count, created, data[_HEADER_V2.size :])
    raise UnsupportedVersionError(f"unsupported snapshot version {version}")
