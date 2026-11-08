# 09 · Exceptions and context managers

Scenario: a small service-support library. Config loading translates low-level
OS and JSON errors into a domain hierarchy. A settings reader and a step runner
show chaining and control flow. A dict-backed `Transaction` with savepoints
covers class-based context managers. A set of `contextlib` helpers handles env
vars, file handles, timing, and cwd. A batch runner collects failures into an
`ExceptionGroup`, and a retry layer separates transient errors from permanent
ones.

Everything lives in `main.py`. Start from the empty module and implement each
part until its tests pass. Tests are in `test_exceptions_context.py`.

## Part 1 · Domain hierarchy and error translation

```
DomainError(Exception)              # base for every error in this module
ConfigError(DomainError)
ConfigNotFound(ConfigError)
ConfigUnreadable(ConfigError)
ConfigInvalid(ConfigError)
SettingMissing(ConfigError)         # __init__(self, key: str); attribute .key
                                    # str(): "setting 'db.url' is not defined"
```

`SettingMissing` must survive `pickle.dumps`/`loads`. Pass the key as the only
positional argument to `super().__init__`.

`load_config(path: str | os.PathLike[str]) -> dict[str, Any]` reads the file as
UTF-8 and parses it as JSON. The root must be an object.

- Missing file: `ConfigNotFound`, chained `from` the `FileNotFoundError`. The
  message contains the path.
- Other OS error (for example, the path is a directory): `ConfigUnreadable`,
  chained from the `OSError`.
- Bytes that are not UTF-8, or malformed JSON: `ConfigInvalid`, chained from
  the `UnicodeDecodeError` or `json.JSONDecodeError`. The JSON message includes
  the line number.
- Root that is not an object: `ConfigInvalid` with no cause.

## Part 2 · Chaining, notes, and walking the chain

`get_setting(config: Mapping[str, Any], dotted_key: str, *, cast=None) -> Any`

- Walks nested mappings by splitting `dotted_key` on `.`.
- A missing key, or a non-mapping in the middle of the path, raises
  `SettingMissing(dotted_key)` with `from None`. This clears `__cause__` and
  sets `__suppress_context__`. The original `KeyError` must still be reachable
  as `__context__`.
- If `cast` is given, it is applied to the found value. A `ValueError` or
  `TypeError` from `cast` becomes `ConfigInvalid` chained from that error. Add
  a note that contains the dotted key and the raw value. Any `DomainError`
  raised by `cast` passes through unchanged.

`describe_chain(exc: BaseException) -> list[str]`

- Returns `"Type: message"` for `exc` and each exception it chains to,
  outermost first. An empty message renders as the type name alone.
- Follow `__cause__` when it is set. Otherwise follow `__context__`, unless
  `__suppress_context__` is true.
- Terminate on cycles.

## Part 3 · try / else / finally semantics

`StepFailed(DomainError)` has attribute `step_name` and message
`"step 'x' failed"`.

`run_checked(name: str, step: Callable[[], T], log: list[str]) -> T`

- Appends `start <name>` to `log` before calling `step`.
- If `step` returns, appends `ok <name>` from the `else` branch and returns the
  value.
- If `step` raises an `Exception`, appends `failed <name>` and raises
  `StepFailed(name)` chained from the original error. `ok` is never logged.
- A `BaseException` such as `KeyboardInterrupt` propagates unchanged, with no
  `failed` entry.
- Always appends `end <name>` from `finally`.

## Part 4 · Class-based context manager with savepoints

`SavepointRollback(DomainError)`: raise it inside a savepoint to discard that
block's writes and continue.

`Transaction(store: MutableMapping[str, Any])`

- `with Transaction(store) as tx:` buffers `tx.set(key, value)` and
  `tx.delete(key)` as pending writes. `tx.get(key)` sees pending writes first.
- Clean exit applies the pending writes to `store`. Any exception discards them
  and propagates the same exception object. `__exit__` never suppresses.
- `get` and `delete` raise `KeyError` for a missing or deleted key.
- `tx.savepoint()` returns a context manager. It snapshots the pending writes on
  entry. On a clean exit it keeps them. On exception it restores the snapshot. It
  suppresses only `SavepointRollback`. Other exceptions propagate, and the
  transaction stays active. Savepoints nest.
- Entering an already-active `Transaction` raises `RuntimeError`, and the outer
  block stays usable. After completion, the same object can be entered again.
- `savepoint()` or any operation on an inactive transaction raises
  `RuntimeError`.

## Part 5 · contextmanager generators, ExitStack, suppress, timer

`temporary_env(**variables: str | None)` (a `@contextmanager`)

- Sets each variable for the duration of the block. A value of `None` unsets it.
- On exit, restores originals. Variables that did not exist before are removed.
  Restoration happens on exception too.

`open_all(paths, *, encoding="utf-8")` (a `@contextmanager` yielding `list[TextIO]`)

- Opens each path for reading using the builtin `open()`, in order, with an
  `ExitStack`.
- If an open fails partway, the files already opened are closed before the
  error propagates. On normal exit, all files are closed.

`unlink_all(paths) -> int`

- Deletes each path. Missing files are ignored, using `contextlib.suppress`.
  Returns how many files were actually deleted.
- Other `OSError`s propagate.

`timer(label, *, clock=time.perf_counter, sink=None)` (a `@contextmanager` yielding `Timer`)

- `Timer.label` is the label. `Timer.elapsed` is live while the block runs and
  frozen when it exits.
- `clock` is injectable. `sink(label, elapsed)`, if given, is called on exit,
  including when the block raised. The exception still propagates.

## Part 6 · contextlib helpers

- `with_resource(factory: Callable[[], R], consume: Callable[[R], T]) -> T`:
  builds the resource, passes it to `consume`, and closes it with
  `contextlib.closing`, even if `consume` raises. `R` implements `close()`.
- `lock_or_null(lock: AbstractContextManager | None) -> AbstractContextManager`:
  returns `lock` itself when given, otherwise a `contextlib.nullcontext()`.
- `capture_stdout(fn, *args, **kwargs) -> tuple[T, str]`: runs `fn` under
  `contextlib.redirect_stdout` and returns `(result, captured_text)`. If `fn`
  raises an `Exception`, add the captured text as a note
  (`captured stdout: '...'`), re-raise, and restore stdout.
- `list_names_in(directory) -> list[str]`: sorted entry names, using
  `contextlib.chdir`. The cwd is restored even if the directory is missing.

## Part 7 · ExceptionGroup batches, retries, and triage

`run_batch(tasks: Sequence[tuple[str, Callable[[], T]]]) -> dict[str, T]`

- Task names must be unique. Otherwise raise `ValueError` before running
  anything.
- Runs every task in order, even after failures. A task's `Exception` is
  recorded, and the note `task '<name>' failed` is added to it.
- If there were no failures, returns `{name: result}` in task order.
- If any failed, raises `ExceptionGroup(f"{n_failed} of {n_total} tasks failed", failures)`.
  Failures are in task order.
- A `BaseException` such as `KeyboardInterrupt` is not collected. It stops the
  batch immediately and propagates.

`is_transient(exc: BaseException) -> bool`

- True for `TimeoutError`, `ConnectionError`, and `InterruptedError`. Also true
  for an `OSError` whose `errno` is `EAGAIN` or `EBUSY`. Everything else is
  false.

`RetryExhausted(DomainError)`: attributes `attempts`. Message `"gave up after N attempts"`.
Its `__init__` takes `attempts`, so it also pickles correctly.

`retry_call(fn, *, attempts, base_delay, sleep=time.sleep, classify=is_transient) -> T`

- Calls `fn` up to `attempts` times. `attempts < 1` raises `ValueError`.
- Between attempts after a classified-transient `Exception`, calls
  `sleep(base_delay * 2 ** (n - 1))`, where `n` is the attempt that just failed.
- A permanent error is re-raised immediately, with no sleep.
- After the final transient failure, raises `RetryExhausted(attempts)` chained
  from the last error. There is no sleep after the final attempt.

`triage(group: ExceptionGroup) -> tuple[ExceptionGroup | None, ExceptionGroup | None]`

- Returns `(transient, permanent)` as produced by `group.split(is_transient)`.
  An empty side is `None`.

## Concepts exercised

- Exception hierarchies, and translating low-level errors into domain errors
- `raise ... from err`, `from None`, `__cause__`, `__context__`,
  `__suppress_context__`
- `add_note`, and `ExceptionGroup` with `except*` and `split`
- `try` / `except` / `else` / `finally` ordering, and `BaseException` versus
  `Exception`
- Class-based context managers, `__exit__` return values and suppression,
  reentrancy
- `contextlib`: `contextmanager`, `ExitStack`, `suppress`, `closing`,
  `nullcontext`, `redirect_stdout`, `chdir`
- Injectable clocks and sleeps, and exception pickling for process pools

## Run

```
uv run pytest exercises/09_exceptions_and_context_managers
uv run pytest exercises/09_exceptions_and_context_managers -k part2
uv run pytest exercises/09_exceptions_and_context_managers --solutions
```

## Stretch (untested)

- Add `__reduce__` to `RetryExhausted` so it also carries `__cause__` across a
  pickle round trip. Think about what pickling actually preserves.
- Give `Transaction` a two-phase commit that validates every pending write
  before applying any of them. Decide what a failure in the middle should leave
  behind.
- Make `timer` a class-based context manager. Compare the two forms for
  exception suppression and reentrancy.
