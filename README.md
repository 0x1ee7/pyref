# pyref — Python refresher, test-driven

Real-world exercises for an experienced Python engineer. Every topic is a
directory under `exercises/` containing a spec (`README.md`), an empty entry
point (`main.py`) and a test suite that fails until you implement the spec.

## Setup

```powershell
uv sync                      # Python 3.14 venv + pytest, pytest-timeout, hypothesis, mypy, tzdata
uv run pytest                # everything (all red at first)
```

## Workflow

```powershell
uv run pytest exercises/10_logging            # one topic
uv run pytest exercises/10_logging -k part2   # one part (test classes are TestPart<N>...)
uv run pytest exercises/10_logging -x --lf    # stop at first failure, rerun last failures
uv run pytest exercises/10_logging --solutions  # grade the reference answer instead of yours
```

1. Read `exercises/<topic>/README.md` — it, together with the tests, is the spec.
2. Write code in `exercises/<topic>/main.py` (some topics own extra files; the README says which).
3. Iterate until green. Then diff against `solutions/<topic>/` and compare approaches.

## How the harness works

Tests never `import main` directly. They take the `main` fixture from the root
[`conftest.py`](conftest.py), which imports a **fresh** copy of your `main.py`
for every test, so:

- a missing function fails only the tests that use it, not the whole file;
- module-level state never leaks between tests;
- objects defined in `main.py` stay picklable for `ProcessPoolExecutor` workers.

`--solutions` points the same fixture at `solutions/<topic>/`. That is also how
the suite itself is validated: with empty `main.py` files every test fails, with
`--solutions` every test passes.

> `solutions/` is the answer key. Don't open it until your own version is green.

## Exercises

| # | Topic | Scenario / what you build | Tests |
|---|---|---|---|
| 01 | [Built-in data structures](exercises/01_builtin_data_structures) | TTL+LRU cache, sliding-window rate limiter, streaming top-K, layered config (ChainMap), inventory reconciliation, bisect price tiers | 80 |
| 02 | [Function arguments](exercises/02_function_arguments) | HTTP request builder (`/`, `*`), query builder, PATCH semantics with sentinels, signature-based DI, event bus, signature-aware cache keys | 73 |
| 03 | [Namespaces & scope](exercises/03_namespaces_and_scope) | closures/nonlocal, late-binding bugs, module registries, class-body scope & mangling, `scoped_override`, exec'd config, restricted formula evaluator | 38 |
| 04 | [Functions deep dive](exercises/04_functions_deep_dive) | `@timed`/`@retry` with optional args, runtime `validate_args`, method decorators, `ttl_cache`, `singledispatch` serializer, command registry | 75 |
| 05 | [Iterators & generators](exercises/05_iterators_and_generators) | lazy log pipeline, on-demand pagination, `yield from` return values, resource-owning generators, `send()` stats, itertools/heapq merges | 61 |
| 06 | [Functional programming](exercises/06_functional_programming) | compose/pipe/curry, immutable `assoc_in`, Redux-style cart reducer, event-sourced account, `Result` type, validator combinators, ETL report | 96 |
| 07 | [OOP fundamentals](exercises/07_oop_fundamentals) | `Money` value object, validated properties, account hierarchy, container dunders, dataclass constructors, notifier ABCs, semver `Version` | 60 |
| 08 | [OOP advanced](exercises/08_oop_advanced) | descriptor-based mini ORM, `__init_subclass__` registry, lazy proxy, cooperative mixins/MRO, `__slots__` + copy protocol, plugin system | 50 |
| 09 | [Exceptions & context managers](exercises/09_exceptions_and_context_managers) | error translation with causes, notes, `Transaction` + savepoints, `ExitStack`, contextlib toolbox, `ExceptionGroup`/`except*`, retry | 58 |
| 10 | [Logging](exercises/10_logging) | JSON formatter, request-id context filter, secret redaction, idempotent `dictConfig`, rotation, `QueueHandler`/`QueueListener` | 75 |
| 11 | [Type hints](exercises/11_type_hints) | PEP 695 generic repo, typed event bus, `ParamSpec` decorators, `Self` builder, `TypeIs`; graded by runtime tests **and** `mypy --strict` | 97 |
| 12 | [Files & serialization](exercises/12_files_and_serialization) | backup tool: tree scan + sha256 manifest, diff/apply, JSON round-trip of custom types, atomic writes, CSV, gzip, TOML, `struct` header | 52 |
| 13 | [Database operations](exercises/13_database_operations) | sqlite3 order backend: migrations, unit of work + savepoints, repositories, bulk upserts, keyset pagination, N+1 avoidance, window-function reports | 53 |
| 14 | [Threads & processes](exercises/14_threads_and_processes) | link checker, bounded downloader, lock ordering, single-flight cache, queue pipelines, periodic scheduler, process-pool CPU work | 56 |
| 15 | [asyncio](exercises/15_asyncio) | aggregator with timeouts, TaskGroup fan-out, cancellation/shield, worker pool, async pagination & resource pool, circuit breaker, token bucket | 58 |
| 16 | [Unit testing](exercises/16_unit_testing) | **inverted**: write the test suite for `billing.py` in `main.py`; graded by mutation testing (your suite must kill every mutant) | 23 |
| 17 | [Dates & times](exercises/17_dates_and_times) | zone-aware conversions, heterogeneous timestamp parsing, DST-safe recurrences, business days, month-end billing, slot overlap, humanize | 56 |
| 18 | [Deployment](exercises/18_deployment) | 12-factor settings, WSGI health/readiness/version, access-log middleware, argparse CLI, SIGTERM handling, `pyproject.toml` + `Dockerfile` | 91 |

Topic-specific notes:

- **16 Unit testing** — the grader copies your `main.py` next to original and mutated copies of
  `billing.py` and runs it in a subprocess (~30 s). Mutations are listed in the grader; don't
  read `test_unit_testing.py` if you want the exercise to stay honest.
- **18 Deployment** — you own `main.py`, `pyproject.toml`, `Dockerfile` and `.dockerignore`
  (shipped empty). Run `uv` from the repo root; inside that directory uv would pick up the
  placeholder `pyproject.toml`.
- **11 Type hints** — Part 7 runs `mypy --strict` over your `main.py` plus the provided
  `usage_ok.py` / `usage_bad.py`; loose (`Any`) types fail it.
