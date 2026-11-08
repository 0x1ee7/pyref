# 12 — Files & Serialization

## Scenario

You are building the core of a small backup/sync tool. It scans a source tree
honouring ignore globs, records a manifest of `{relative posix path: size, mtime,
sha256}`, diffs two manifests, persists them atomically as JSON with custom types,
exports a CSV report full of awkward values, reads its settings from TOML,
compresses archives in a streaming fashion, applies a diff to a mirror, and
writes a versioned binary snapshot header.

Everything lives in `main.py`. Tests only use `tmp_path`; they never touch the
real filesystem outside it.

## Conventions

- Paths are `pathlib.Path`. Manifest keys are **relative POSIX strings**
  (`"pkg/mod.py"`), even on Windows.
- All text files are UTF-8, written with `newline=""` where the `csv` module is
  involved.
- Times are timezone-aware UTC `datetime`s truncated to microseconds.
- Errors use the exception types named below; do not invent new ones unless the
  spec says so.

## Part 1 — Tree scan and hashing

`sha256_file(path: Path, chunk_size: int = 65_536) -> str`
- Returns the lowercase hex SHA-256 of the file, read in chunks of `chunk_size`
  bytes (streaming, never `read_bytes()`).
- `chunk_size <= 0` → `ValueError`.

```python
@dataclass(frozen=True, slots=True)
class FileEntry:
    size: int
    mtime: datetime   # aware UTC, microsecond precision (floor of st_mtime_ns)
    sha256: str
Manifest = dict[str, FileEntry]
```

`scan_tree(root: Path, ignore: Sequence[str] = ()) -> Manifest`
- `root` missing → `FileNotFoundError`; `root` exists but is not a directory →
  `NotADirectoryError`.
- Walks recursively (`rglob`). Only regular files are included; symlinks
  (to files or directories) are skipped entirely.
- Ignore rules, applied to each file's relative posix path:
  - A pattern **without** `/` matches if **any path component** (file or directory
    name, at any depth) matches it via `PurePosixPath.full_match`. `"*.tmp"` drops
    `a.tmp` and `x/y/b.tmp`; `"__pycache__"` drops everything under any
    `__pycache__` directory.
  - A pattern **with** `/` must match the **whole** relative path with
    `full_match` (so `**` works and the pattern is anchored at the root).
    `"logs/**"` drops `logs/a.log` and `logs/deep/b.log` but keeps
    `other/logs/c.log`.
- `mtime` comes from `st_mtime_ns`, floored to microseconds, as an aware UTC
  datetime.
- Returned dict keys are in sorted order.

## Part 2 — Diff and apply

```python
@dataclass(frozen=True, slots=True)
class ManifestDiff:
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    @property
    def is_empty(self) -> bool: ...
```

`diff_manifests(old: Manifest, new: Manifest) -> ManifestDiff`
- `added`: keys only in `new`; `removed`: keys only in `old`.
- `modified`: keys in both whose `size` or `sha256` differ. An **mtime-only**
  change is not a modification.
- All three tuples are sorted.

```python
@dataclass(frozen=True, slots=True)
class ApplyResult:
    copied: tuple[str, ...]    # paths copied from source to dest
    deleted: tuple[str, ...]   # paths actually removed from dest
```

`apply_diff(diff: ManifestDiff, source: Path, dest: Path) -> ApplyResult`
- Validates every path in `added`, `modified`, `removed` **before any disk
  change**. A path that is absolute, empty, or contains `..` → `ValueError`.
- Copies `added` and `modified` files from `source/<key>` to `dest/<key>`,
  creating parent directories, using `shutil.copy2` (preserves mtime).
- Deletes `removed` files from `dest`. A file that is already absent is not an
  error and is not reported in `deleted`.
- After a deletion, removes parent directories that became empty, climbing up to
  but **not including** `dest`. Non-empty directories are left alone.

## Part 3 — JSON with custom types and atomic writes

`encode_json(obj: object) -> str`
- Produces indented JSON with `sort_keys=True`, non-ASCII characters written as-is.
- Tagged encodings (every tag is a dict with key `"__type__"`):
  - `datetime` → `{"__type__": "datetime", "value": <isoformat>}`. A naive
    datetime → `ValueError`.
  - `Decimal` → `{"__type__": "Decimal", "value": "<str>"}` (exact; no floats).
  - `UUID` → `{"__type__": "UUID", "value": "<str>"}`.
  - `Enum` → `{"__type__": "enum", "class": "<ClassName>", "value": <member value>}`.
  - dataclass instance → `{"__type__": "dataclass", "class": "<ClassName>", "fields": {...}}`
    where nested values are encoded recursively.
- A plain `dict` containing the key `"__type__"` anywhere in the object → `ValueError`
  (it would be ambiguous on decode).
- Any other unsupported type → `TypeError`.

`decode_json(text: str, types: Sequence[type] = ()) -> Any`
- Rebuilds tagged values through `object_hook`. Dataclass and Enum tags are
  resolved by class `__name__` among `types`; a name not present → `LookupError`.
- Unknown tag value → `ValueError`.

`write_bytes_atomic(path: Path, data: bytes) -> None`
- Creates a temporary file in **the same directory** as `path`
  (`tempfile.NamedTemporaryFile(dir=..., delete=False)`), writes, flushes,
  `os.fsync`s, closes, then `os.replace`s it onto `path`.
- If anything fails, the temporary file is removed and the exception propagates;
  an existing `path` is left untouched.

`save_json_atomic(obj: object, path: Path) -> None`
- Encodes first (so an encoding error never touches the destination), then writes
  `encode_json(obj) + "\n"` UTF-8 via `write_bytes_atomic`.

`load_json(path: Path, types: Sequence[type] = ()) -> Any`
- Reads the file as UTF-8 and calls `decode_json`.

## Part 4 — CSV report

`class ReportDialect(csv.excel)`: `delimiter = ";"`, `lineterminator = "\n"`.
Define it as a subclass; never call `csv.register_dialect` (global state).

`REPORT_FIELDS = ("path", "size", "mtime", "sha256", "note")`

`export_report(manifest: Manifest, path: Path, notes: Mapping[str, str] | None = None) -> None`
- Writes UTF-8 (no BOM) with `newline=""` via `csv.DictWriter` using
  `ReportDialect`. Header first, then one row per manifest entry sorted by path.
- `size` is the decimal integer; `mtime` is `datetime.isoformat()`; `note` is
  `notes.get(path, "")`.
- A key in `notes` that is not in `manifest` → `ValueError`.

`import_report(path: Path) -> list[dict[str, str]]`
- Reads with `csv.DictReader` using `ReportDialect`, `newline=""`. The header must
  equal `REPORT_FIELDS` exactly, otherwise `ValueError`. Returns rows in file order
  as plain `str` values.

## Part 5 — Streaming gzip

`compress_file(src: Path, dst: Path, *, level: int = 6) -> int`
- Streams `src` into `dst` in fixed-size chunks (never reads the whole file into
  memory). Returns the number of **uncompressed** bytes read.
- Output must be reproducible: no embedded filename, `mtime` header field 0.
  Compressing identical content to two different destination names produces
  identical bytes.
- Missing `src` → `FileNotFoundError`; `dst` is not created in that case.

`decompress_file(src: Path, dst: Path) -> int`
- Streams to `dst`; returns the number of uncompressed bytes written.
- Corrupt or truncated input (anything `gzip` reports as `BadGzipFile`,
  `EOFError`, or `zlib.error`) → `ValueError` (chained). The partial `dst` is
  removed.
- Missing `src` → `FileNotFoundError`.

`verify_gzip(path: Path) -> bool`
- Streams the whole file, discarding the data. `True` for a complete, valid
  stream; `False` for corruption, truncation, or an empty file. Missing file →
  `FileNotFoundError`.

## Part 6 — TOML settings

```python
@dataclass(frozen=True, slots=True)
class Settings:
    source: Path
    destination: Path
    ignore: tuple[str, ...]
    retention: timedelta
    compression_level: int
```

`load_settings(path: Path) -> Settings`
- Parses with `tomllib` from a binary file handle. Malformed TOML propagates
  `tomllib.TOMLDecodeError` (a `ValueError`).
- Settings live in a `[sync]` table. Missing table → `ValueError`.
- Keys: `source` (required str), `destination` (required str), `ignore`
  (list of str, default `[]`), `retention_days` (int, 0..36500, default 30),
  `compression_level` (int, 1..9, default 6).
- Unknown keys in `[sync]` → `ValueError` naming them (catches typos).
- Booleans are **not** integers here: `true` for an int key → `ValueError`.
- Errors name the offending key, e.g. `"[sync].source is required"`.
- `source` and `destination` are resolved **relative to the config file's
  directory**, not the current working directory. Absolute values are kept.
- `retention` is `timedelta(days=retention_days)`.

## Part 7 — Versioned binary snapshot header

Layout is big-endian (network order), fixed width, explicit with `struct`:

| Version | struct format | Fields |
|---|---|---|
| 1 | `>4sHHI` (12 bytes) | magic, version, flags, entry_count |
| 2 | `>4sHHIq` (20 bytes) | v1 fields + `created` as Unix epoch microseconds (signed 64-bit) |

Magic is `SNAPSHOT_MAGIC = b"PYSN"`. `SNAPSHOT_VERSION = 2` is what gets written.

```python
class SnapshotError(ValueError): ...
class UnsupportedVersionError(SnapshotError): ...

@dataclass(frozen=True, slots=True)
class Snapshot:
    version: int
    flags: int
    entry_count: int
    created_at: datetime | None   # None for version 1
    payload: bytes
```

`write_snapshot(path: Path, *, entry_count: int, created_at: datetime, payload: bytes, flags: int = 0) -> None`
- `created_at` must be timezone-aware → else `ValueError`. `created_at` keeps microseconds.
- `entry_count` must fit in unsigned 32 bits, `flags` in unsigned 16 bits; otherwise `ValueError`.
- Writes header followed by payload, via `write_bytes_atomic`.

`read_snapshot(path: Path) -> Snapshot`
- Fewer bytes than the header requires → `SnapshotError` (message contains `"truncated"`).
- Magic other than `b"PYSN"` → `SnapshotError` (message contains `"magic"`).
- Version 1 or 2 → parsed. Any other version → `UnsupportedVersionError`.
- The payload is everything after the header, byte-for-byte.

## Concepts exercised

- `pathlib`: `rglob`, `relative_to`, `as_posix`, `PurePosixPath.full_match`, `stat`, `is_relative_to`, `joinpath`
- Hashing: chunked streaming digests with `hashlib`
- `datetime` with timezones; `st_mtime_ns` precision
- `json`: `JSONEncoder.default`, `object_hook`, tagging, reserved keys, `sort_keys`
- Atomic writes: `tempfile.NamedTemporaryFile(delete=False)`, same-directory staging, `os.replace`, `fsync`
- `csv`: dialect subclasses, `DictReader`/`DictWriter`, quoting, `newline=""`
- `gzip.GzipFile` streaming, deterministic headers, corruption detection
- `tomllib` with strict validation and config-relative paths
- `struct`: explicit byte order, versioned layouts, magic validation
- `shutil.copy2`, directory pruning, path-traversal guards

## Run

```
uv run pytest exercises/12_files_and_serialization
uv run pytest exercises/12_files_and_serialization -k part3
uv run pytest exercises/12_files_and_serialization --solutions
```

## Stretch (untested)

- Make `scan_tree` prune ignored directories during the walk instead of visiting them.
- Encode `str`-mixin enums (`class X(str, Enum)`): `json` emits them as plain strings before `default` runs. How would you intercept them?
- Add an optional `compresslevel` `gzip` fast path for already-compressed inputs, and measure it.
- Sanitise CSV cells that start with `=`, `+`, `-`, `@` to block spreadsheet formula injection, and decide where that belongs.
