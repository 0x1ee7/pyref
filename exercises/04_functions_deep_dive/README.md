# 04 · Functions deep dive

You are building the function-level toolkit that a small internal platform team
ships with its Python services: retry and timing wrappers, a runtime
annotation guard, a rate limiter for API clients, a TTL cache, version
comparison, settings merging, a JSON-ish serialiser for the reporting
pipeline, and a CLI command registry. Everything lives in `main.py`.

Every public name below must exist at module level in `main.py`. Tests import
a fresh `main` per test, so module-level state never leaks between tests.

## Part 1: closures and first-class functions

- `make_multiplier(factor: int) -> Callable[[int], int]`: returns a closure that
  multiplies its argument by `factor`.
- `make_counter(start: int = 0) -> Callable[[], int]`: each call returns the
  current value and then increments it. First call returns `start`. Use
  `nonlocal`.
- `compose(*funcs) -> Callable[[Any], Any]`: applies `funcs` left to right
  (`compose(f, g)(x) == g(f(x))`). With no arguments it is the identity.
  A non-callable argument raises `TypeError` at `compose` time.
- `make_adders(n: int) -> list[Callable[[int], int]]`: returns `n` adders where
  the adder at index `i` adds `i`. A naive comprehension closing over the loop
  variable late-binds and is wrong; avoid it. Negative `n` raises `ValueError`.

## Part 2: decorators, optional arguments, `functools.wraps`

- `timed(func=None, *, logger=None, level=logging.DEBUG, clock=time.perf_counter)`:
  works bare (`@timed`) and called (`@timed(...)`). Each call logs
  `"<qualname> took <seconds with 3 decimals>s"` at `level` on `logger`
  (default: `logging.getLogger(func.__module__)`). The elapsed time is logged in
  a `finally`, so failing calls are logged too. Exceptions propagate.
  Measure with `clock()` before and after the call.
- The wrapper must preserve metadata: `__name__`, `__qualname__`, `__doc__`,
  `__module__`, and `__wrapped__` pointing at the original.
- A non-callable target (`timed(42)`) raises `TypeError`.

## Part 3: parameterised decorators and stacking

- `retry(func=None, *, attempts=3, exceptions=(Exception,), backoff=0.0, sleep=time.sleep)`:
  bare and called forms both work. `attempts` is the total number of calls,
  including the first, and must be an `int >= 1` (`ValueError` otherwise).
  `exceptions` is a tuple or a single exception class.
  Only listed exceptions are retried; others propagate on the first attempt.
  After the final failed attempt the last exception propagates unchanged.
  Between attempts, call `sleep(backoff * 2 ** (attempt - 1))`. This call happens
  even when the delay is `0.0`, and never after a success or the final failure.
  Positional options are not allowed (`retry(3)` raises `TypeError`).
- Stacking order matters. The outermost decorator sees everything beneath it.
  The Part 3 tests cover `timed` outside `retry` (timing includes sleeps) and
  `retry` outside `timed` (each attempt is timed).

## Part 4: introspection and runtime annotation checks

- `validate_args(func)`: enforces annotations at call time using
  `inspect.signature` and `typing.get_type_hints`. Each argument the caller
  actually passed is checked against its annotation. Unsupplied defaults are not
  checked. Parameters with no annotation are not checked. `*args` and `**kwargs`
  annotations apply per item. The return value is checked when there is a
  return annotation. On failure raise `TypeError` naming the parameter (or
  `return`).
  - Supported hints: plain classes (`isinstance`), `X | None` and `Union[...]`,
    `Any` (always accepted), and generic aliases such as `list[str]`, which
    check only the origin (`list`).
  - Other hint forms (`TypeVar`, `Literal`, ...) are accepted without checks.
  - Argument binding errors (missing or extra arguments) come from
    `Signature.bind` and are `TypeError`.
- `signature_summary(func, *, follow_wrapped=True) -> dict[str, Any]` returns:
  - `"name"`: `func.__name__`
  - `"parameters"`: list of `(name, kind_name, has_default)` tuples from
    `inspect.signature(func, follow_wrapped=follow_wrapped)`, where `kind_name` is
    `Parameter.kind.name`
  - `"defaults"`: the raw `func.__defaults__` attribute, or `None`
  - `"wrapped_depth"`: number of `__wrapped__` hops before reaching a callable
    with no `__wrapped__`

## Part 5: class-based and method decorators

- `CallCounter(func)`: a class-based decorator.
  - `.count` is the number of calls, and `.reset()` sets it to zero.
  - It copies metadata with `functools.update_wrapper`, so `__wrapped__` is set.
  - `__get__` must bind the instance so that it works on methods. Accessed from
    the class (`Cls.method`), it returns the counter object itself.
  - The count is shared by all instances, because there is one decorator object
    per method.
- `RateLimitExceeded(RuntimeError)`: has attribute `retry_after: float`
  (seconds until the oldest call in the window expires). The message includes
  it.
- `rate_limited(max_calls, period, *, clock=time.monotonic)`: a decorator
  factory for methods. Implements a sliding window of `max_calls` per `period`
  seconds, kept per instance (store it on `self.__dict__`). A call at time
  `now` is allowed if fewer than `max_calls` calls occurred within the last
  `period` seconds. A call that is rejected is not recorded. Raises `ValueError`
  for `max_calls < 1` or `period <= 0`. Metadata is preserved.

## Part 6: functools toolkit

- `ttl_cache(seconds, *, clock=time.monotonic, maxsize=None)`: decorator factory
  (always called, with the parentheses).
  - Memoises by `(args, tuple(sorted(kwargs.items())))`. Positional and keyword
    spellings of a call are different keys.
  - An entry is valid while `clock() < stored_at + seconds`.
  - With `maxsize`, evict the least recently used entry (a hit counts as a use).
    `maxsize=None` is unbounded.
  - Attach `cache_info()`, which returns `CacheInfo(hits, misses, maxsize, currsize)`
    (a `NamedTuple` named `CacheInfo`), and `cache_clear()`, which resets entries
    and counters.
  - Unhashable arguments raise `TypeError`. `seconds <= 0` and `maxsize < 1`
    raise `ValueError`. Metadata is preserved.
- `Version`: `Version(major, minor, patch)` with non-negative ints (`ValueError`
  otherwise).
  - `Version.parse(text)` accepts `MAJOR.MINOR.PATCH` with an optional leading
    `v`. Malformed input raises `ValueError`. Only ASCII digits are accepted.
  - `str()` gives `"1.2.3"`, and `repr()` gives `Version(1, 2, 3)`.
  - Ordering is numeric and total. Use `functools.total_ordering`.
  - Equal versions hash equally. Comparing with a non-`Version` value gives `False` for
    `==`, and `TypeError` for `<`.
- `Release(version, artifacts)`: `.checksum` is the sha256 hex digest of
  `"\n".join(sorted(artifacts))`. It is a `functools.cached_property`, computed
  once.
- `merge_settings(*layers) -> dict`: folds the mappings left to right with
  `functools.reduce`. Later layers win. Nested mappings merge recursively. A
  scalar overridden by a mapping (or the reverse) is replaced. Inputs are never
  mutated. No layers gives `{}`.
- `make_url_builder(base: str) -> Callable[..., str]`: returns a
  `functools.partial` of the internal `_build_url`, with `base` pre-bound.
  Calling it with string path `segments` and keyword `query` values gives
  `base` (trailing slash removed) + `/` + segments joined with `/` (leading and
  trailing slashes stripped, empty segments dropped). Query parameters are
  sorted by key and URL-encoded with `urllib.parse.urlencode`. With no segments
  and no query, the result is the base without a trailing slash.

## Part 7: singledispatch serialisation and a command registry

- `to_jsonish` is a `functools.singledispatch` function. Its base implementation
  serialises dataclass instances field by field (recursively), and raises
  `TypeError` for everything else. It must be extensible with
  `to_jsonish.register(cls, func)`. Registered implementations:
  - `str`, `int`, `float`, `bool`, `None`: returned unchanged.
  - `Decimal`: `str(value)`, which keeps precision.
  - `datetime.datetime`: `isoformat()`. `datetime` must win over its base class
    `date`.
  - `datetime.date`: `isoformat()`.
  - `Enum`: the serialised `.value`.
  - `list` and `tuple`: a list of serialised items.
  - `set` and `frozenset`: a sorted list of serialised items. If the items are
    not mutually orderable, sort by `repr` instead.
  - `dict`: keys must be `str` (else `TypeError`); values are serialised.
- `UnknownCommand(KeyError)`: raised by `dispatch` and `help` for an unregistered
  name.
- `CommandRegistry()`: each instance is independent.
  - `command(...)` is a decorator. Forms: `@reg.command`, `@reg.command()` and
    `@reg.command("name")`. The default name is the function name with `_`
    replaced by `-`. It returns the function unchanged. A duplicate name raises
    `ValueError`. A non-`str` name raises `TypeError`.
  - `dispatch(name, *args, **kwargs)`: calls the registered function and returns
    its result.
  - `names() -> list[str]`: sorted.
  - `help(name) -> str`: the first line of the docstring (via
    `inspect.getdoc`), or `""` if there is none.

## Concepts exercised

- Closures, `nonlocal`, late binding, and why factory functions fix it
- Decorator anatomy: optional-argument decorators, parameterised decorators,
  `functools.wraps`, `__wrapped__`, stacking order
- Injected clocks and sleeps for deterministic time-based behaviour
- `inspect.signature`, `follow_wrapped`, `__defaults__`, `get_type_hints`
- Class-based decorators with `__get__` for method binding
- Per-instance state for decorated methods
- `functools`: `partial`, `reduce`, `total_ordering`, `cached_property`,
  `singledispatch`, and the `lru_cache` semantics behind `ttl_cache`

## Run

```
uv run pytest exercises/04_functions_deep_dive            # all tests (fail until implemented)
uv run pytest exercises/04_functions_deep_dive -k part2   # one part
uv run pytest exercises/04_functions_deep_dive --solutions  # grade the reference solution
```

## Stretch

- Make `validate_args` handle `Annotated[...]` and `typing.Protocol` targets.
- Support `async def` functions in `retry` and `timed` (detect with
  `inspect.iscoroutinefunction`), and make sure the wrapper stays a coroutine
  function.
- Make `CallCounter` per-instance by implementing `__set_name__` and storing
  counters in `instance.__dict__`.
- Extend `to_jsonish` to handle `IntEnum`. Its MRO hits the `int` registration
  before `Enum`. Decide what the right output is and test it.
