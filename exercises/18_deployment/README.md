# 18 · Deployment: making a Python service shippable

A small HTTP service (`pyref-service`) that is ready to run in a container and
behave well under an orchestrator. Stdlib only: no web framework, no config
library, no packaging helper beyond `tomllib` for linting.

You implement everything in `main.py`. Two other files are yours too: `pyproject.toml`
and `Dockerfile` and `.dockerignore`. All three ship empty.

Module rules: importing `main` must have no side effects (no server, no
logging config, no env reads). Only `main()` and `serve` touch the outside world.

---

## Part 1 · 12-factor configuration

Configuration comes from the environment. An optional `.env` file may supply
defaults, but the real environment always wins.

```python
DISTRIBUTION = "pyref-service"   # also the console script name

class ConfigError(Exception):
    errors: tuple[str, ...]      # one "VAR: problem" entry per bad variable
    def __init__(self, errors: Sequence[str]) -> None: ...
    # str(err) == "invalid configuration: " + "; ".join(errors)

class DotenvError(ValueError): ...   # malformed .env line; message starts "line N: "

def parse_bool(text: str) -> bool: ...
    # true: 1 true yes on   false: 0 false no off   (case-insensitive, whitespace stripped)
    # anything else -> ValueError

def parse_duration(text: str) -> timedelta: ...
    # "500ms", "30s", "5m", "1h", bare "2" (= seconds), "1.5s"
    # ValueError on empty, negative, unknown unit, or prose ("5 minutes")

def parse_list(text: str) -> tuple[str, ...]: ...
    # comma-separated, stripped, empty items dropped

def mask_url_password(url: str) -> str: ...
    # "postgresql://app:hunter2@db:5432/x" -> "postgresql://app:***@db:5432/x"
    # URLs without a password are returned unchanged

@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str            # REQUIRED  DATABASE_URL  scheme must be postgresql or sqlite
    api_token: str               # REQUIRED  API_TOKEN     secret, >= 16 characters
    app_name: str = "pyref-service"          # APP_NAME
    port: int = 8080                         # PORT        1..65535
    debug: bool = False                      # DEBUG
    allowed_hosts: tuple[str, ...] = ()      # ALLOWED_HOSTS  comma list
    request_timeout: timedelta = timedelta(seconds=30)  # REQUEST_TIMEOUT  duration > 0
    log_level: str = "INFO"                  # LOG_LEVEL   DEBUG|INFO|WARNING|ERROR|CRITICAL, any case, stored uppercase
    git_sha: str | None = None               # GIT_SHA     7-40 hex chars, stored lowercase

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Settings": ...
    def __repr__(self) -> str: ...
    # repr shows api_token as '***' and masks the database password; host stays visible

def read_env_file(path: Path) -> dict[str, str]: ...   # FileNotFoundError propagates

def load_dotenv(text: str) -> dict[str, str]: ...

def load_settings(environ: Mapping[str, str], env_file: Path | None = None) -> Settings: ...
    # merged = {**dotenv_values, **environ}  -> Settings.from_env(merged)
```

Rules:

- `from_env` never stops at the first problem. It collects **every** bad or
  missing variable and raises one `ConfigError` listing all of them.
- Empty or whitespace-only value counts as **unset**: optional variables take
  their default; required variables report `"VAR: required but not set"`.
- Error messages never echo secret values (`API_TOKEN`, the database password).
  Echoing other bad input (e.g. `PORT='eighty'`) is fine.
- `Settings` is frozen. `repr` must not contain the token or the password.
- `.env` grammar, one `KEY=VALUE` per line:
  - blank lines and lines starting with `#` are skipped;
  - optional leading `export ` (any whitespace after it);
  - whitespace around `=` is allowed (`PORT = 9000`);
  - `'single'` quotes are literal; `"double"` quotes honour `\n \t \" \\`;
  - unquoted values end at an inline comment (whitespace followed by `#`);
  - after a closing quote only whitespace or a comment may follow;
  - an unterminated quote, a line without `=`, or an invalid key is a `DotenvError`;
  - a later duplicate key overrides an earlier one.

## Part 2 · WSGI app with health and version endpoints

```python
ReadinessCheck = Callable[[], object]   # returns normally = healthy; raises = unhealthy

def get_version(distribution: str = DISTRIBUTION) -> str: ...
    # importlib.metadata.version(distribution), looked up on the module at call time,
    # falling back to "0.0.0+unknown" when the distribution is not installed

def create_app(settings: Settings,
               *,
               readiness_checks: Mapping[str, ReadinessCheck] | None = None
               ) -> WSGIApplication: ...
```

The returned object is a plain WSGI callable `app(environ, start_response) -> Iterable[bytes]`.

| Route | Method | Response |
|---|---|---|
| `/healthz` | GET, HEAD | `200 {"status": "ok"}` always. Never runs checks. |
| `/readyz` | GET, HEAD | Runs every check on every request. All pass: `200 {"status": "ready", "checks": {name: "ok"}}`. Any raises: `503 {"status": "not_ready", "checks": {name: "ok" \| "error: TypeName: message"}}` (every check is still reported). |
| `/version` | GET, HEAD | `200 {"app": app_name, "version": get_version(), "git_sha": git_sha-or-null}`. Version is resolved per request. |
| anything else | any | `404 {"error": "not_found", "path": path}` |
| known route, other method | e.g. POST | `405 {"error": "method_not_allowed"}` with `Allow: GET, HEAD` |

- All JSON responses: `Content-Type: application/json; charset=utf-8` and an
  accurate `Content-Length`.
- HEAD returns the same status and headers, with an empty body.
- Catch `Exception` (not `BaseException`) from checks.

## Part 3 · Structured JSON access logging

```python
REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_KEY = "pyref.request_id"   # environ key where the id is stored

def access_log(app: WSGIApplication,
               *,
               logger: logging.Logger,
               clock: Callable[[], float] = time.perf_counter
               ) -> WSGIApplication: ...
```

Middleware behaviour:

- **Request id.** Take `HTTP_X_REQUEST_ID` when it matches `[A-Za-z0-9._:-]{1,128}`.
  Otherwise generate `uuid.uuid4().hex`. Store it at `environ[REQUEST_ID_KEY]`
  and add it as `X-Request-ID` to the response headers.
- **Timing.** `clock()` is called exactly twice per request: once before the app
  runs, and once when the record is emitted.
- **One record per request**, emitted when the response iterable's `close()`
  runs (WSGI guarantees the server calls it). Calling `close()` more than once
  must not emit a second record. Emit it through `logger.info(...)`, with the
  message set to a JSON object string containing exactly these keys:
  `method`, `path` (no query string), `status` (int), `duration_ms` (float,
  rounded to 3 places), `request_id`.
- **Status.** The integer parsed from the status string passed to
  `start_response`. If the app raised before calling it, the status is 500.
- **App raises.** Emit the record immediately with status 500, then re-raise the
  exception unchanged.
- The middleware must not add handlers or change the level or `propagate` of
  any logger. Logging configuration belongs to the caller.

## Part 4 · CLI entry point

```python
def build_app(settings: Settings) -> WSGIApplication: ...
    # access_log(create_app(settings), logger=logging.getLogger("pyref.access"))

def serve(app: WSGIApplication, host: str, port: int) -> int: ...
    # wsgiref make_server (quiet request handler), serve_forever, returns 0 after shutdown

def build_parser() -> argparse.ArgumentParser: ...

def main(argv: Sequence[str] | None = None) -> int: ...

if __name__ == "__main__":
    raise SystemExit(main())
```

Command surface (`prog="pyref-service"`):

```
pyref-service [--env-file PATH] serve [--host H] [--port P] [--dry-run]
pyref-service [--env-file PATH] check-config
pyref-service version
```

- `main` **returns** an exit code and never raises `SystemExit` (argparse's exit
  is caught and its code returned; usage errors give 2).
- `version`: prints `get_version()` to stdout, exit 0. Needs no configuration.
- `check-config`: prints `configuration ok` to stdout, exit 0. On `ConfigError`,
  prints `error: <entry>` for **each** entry to stderr, exit 2. On `DotenvError`
  or `OSError` (e.g. missing `--env-file`), prints `error: <message>`, exit 2.
- `serve --dry-run`: loads settings the same way, prints
  `dry run: <app_name> would listen on <host>:<port>`, exit 0, and does **not**
  bind a socket. Default `--host` is `127.0.0.1`. `--port` overrides `settings.port`.
- `serve` (no dry run): `logging.basicConfig(level=settings.log_level, format="%(message)s")`,
  then `serve(build_app(settings), host, port)`. Uses the Part 5 signal handling.

## Part 5 · Graceful shutdown on SIGINT / SIGTERM

```python
def install_signal_handlers(
    shutdown: Callable[[int], None],
    *,
    signals: Sequence[signal.Signals] = (signal.SIGINT, signal.SIGTERM),
) -> Callable[[], None]: ...
```

- Installs one handler for each signal in `signals`.
- The **first** delivery of **any** of those signals calls `shutdown(signum)`.
  Later deliveries are ignored: the callback runs exactly once.
- Returns a `restore()` callable that reinstates the handlers that were active
  before. Calling it more than once is harmless. If a previous handler is `None`
  (not installed from Python), restore to `SIG_DFL`.
- Works on Windows: `signal.raise_signal(SIGINT)` and `SIGTERM` are both valid there.
- `shutdown` runs inside the signal handler, on the main thread. Keep it short and
  non-blocking. In `serve`, `server.shutdown()` must run on another thread,
  because it waits for `serve_forever()`, which is currently on the main thread.

## Part 6 · Packaging and container files

Write `pyproject.toml`, `Dockerfile` and `.dockerignore` in this directory. The
tests parse them: `tomllib` for TOML, simple line parsing for the Dockerfile.
Line continuations (`\`) and `#` comments must be handled by a reader.

`pyproject.toml`:

- `[project]`: `name = "pyref-service"`, `version` in `X.Y.Z` form, `requires-python`
  with a floor of `>=3.13`, `dependencies = []` (stdlib only).
- `[project.scripts]`: `pyref-service = "main:main"`.
- `[build-system]`: non-empty `requires` and a `build-backend`.

`Dockerfile` (what the tests check):

- Every `FROM` image is a pinned slim Python: `python:3.1x.y-slim-<suite>`.
  No `latest`, no untagged image.
- The dependency manifest (`COPY pyproject.toml ...`) comes before any `COPY`
  of the source tree (`COPY . ...`).
- `ENV` sets `PYTHONDONTWRITEBYTECODE=1` and `PYTHONUNBUFFERED=1`.
- The last `USER` is not `root` / `0`.
- `EXPOSE` a numeric port.
- A `HEALTHCHECK` that targets `/healthz` (not `NONE`).
- `ENTRYPOINT` and `CMD` both in exec (JSON array) form.

`.dockerignore` must exclude (after stripping `**/` and trailing `/`):
`.git`, `.venv`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.hypothesis`, `.env`.

---

## Concepts exercised

- 12-factor config; typed parsing; aggregated validation errors; secret masking
- `.env` grammar and precedence; frozen dataclasses with a custom `repr`
- WSGI as a plain callable; `wsgiref.util.setup_testing_defaults`; `close()` semantics
- `importlib.metadata` with fallback; JSON APIs; HEAD and 405 handling
- Injected clocks; `logging` without global side effects; request-id propagation
- argparse subcommands returning exit codes; `SystemExit` discipline
- `signal.signal` / `raise_signal` / restoring handlers; Windows signal support
- `tomllib`; Dockerfile best practices (pinning, layer order, non-root, healthcheck)

## Run

```
uv run pytest exercises/18_deployment                 # full set
uv run pytest exercises/18_deployment -k part1        # one part (part1..part6)
uv run pytest exercises/18_deployment --solutions     # validate the reference
```

## Stretch (untested)

- Replace the wsgiref server with `gunicorn`-style pre-fork workers.
- Emit a `Server-Timing` header from the access middleware.
- Add a `SIGHUP` handler that re-reads the env file and swaps settings atomically.
- Build the image with `docker buildx` and check the healthcheck from the host.
