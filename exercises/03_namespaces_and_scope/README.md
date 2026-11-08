# 03 · Namespaces and scope

You maintain the internals of a small automation toolkit: id generators for an order
service, a plugin registry that loads hooks at import time, a class hierarchy for a
ledger, a config loader for deploy-time settings, and a sandboxed evaluator for
spreadsheet-style formulas. Every piece depends on how Python resolves names, when
closures capture variables, and what `exec`/`eval` can reach. Implement the API in
`main.py`; the tests in `test_namespaces_and_scope.py` are the full spec.

## Part 1 · LEGB lookup, closures, and `nonlocal`

- `resolve_scope(name, *, local_scope, enclosing, global_scope, builtin_scope) -> str`
  returns `"local"`, `"enclosing"`, `"global"` or `"builtin"`: the first mapping, searched
  in LEGB order, that contains `name`. Raises `NameError` if none does.
- `make_counter(start: int = 0, step: int = 1) -> Callable[[], int]`: each call returns the
  current value then advances by `step`. `step == 0` raises `ValueError`. Counters are
  independent of each other.
- `make_id_generator(prefix: str, width: int = 4) -> Callable[[], str]`: returns ids
  `"<prefix>-<n>"` with `n` zero-padded to `width` and starting at `1`. Numbers wider than
  `width` are not truncated. An empty `prefix` or `width < 1` raises `ValueError`.

## Part 2 · Late binding

- `late_bound_callbacks(labels: Sequence[str]) -> list[Callable[[], str]]`: one callback
  per label, built in a loop, where each callback returns `"clicked <label>"`. This is the
  classic bug: write it so the callbacks *do* share the loop variable. Calling any of them
  returns the **last** label. Empty input yields `[]`.
- `make_handlers(labels: Iterable[str]) -> dict[str, Callable[[], str]]`: the fixed version.
  Each handler returns `"clicked <its own label>"`. Duplicate labels raise `ValueError`.
- `make_adders(offsets: Iterable[int]) -> list[Callable[[int], int]]`: `adder(value)` returns
  `value + offset` for the offset that created it.

## Part 3 · Module-level registry

Module globals are the process-wide singleton. The registry lives in a module-level dict.

- `register(name: str)`: decorator. `name` must be an identifier (`str.isidentifier`), else
  `ValueError`. Duplicate names raise `ValueError`. Returns the decorated function unchanged.
- `unregister(name)`: removes the plugin; unknown name raises `KeyError`.
- `get_plugin(name)`: returns the registered callable; unknown name raises `KeyError`.
- `plugin_names() -> list[str]`: names in registration order (not sorted).
- `run_plugins(*args, **kwargs) -> dict[str, Any]`: calls every plugin with the same
  arguments, returns `{name: result}` in registration order. Exceptions propagate.
- `clear_plugins()`: removes all plugins.
- `total_registrations() -> int`: number of successful `register` calls since import.
  Unaffected by `unregister` or `clear_plugins`. Failed (duplicate/invalid) registrations do
  not count. Requires a `global` statement to update.

## Part 4 · Class-body scope and name mangling

- `class Scaler` with class attributes `FACTOR = 3` and `TABLE_SIZE = 5`, and
  `TABLE: tuple[int, ...]` equal to `(0, 3, 6, 9, 12)`, computed **in the class body**.
  A naive `tuple(i * FACTOR for i in range(TABLE_SIZE))` raises `NameError` at class
  creation. Find the rule that explains why, and write a body that works. Leave no loop
  variables behind as class attributes.
  Instance method `scale(self, x: int) -> int` returns `x * self.FACTOR`.
- `mangle_name(class_name: str, attribute: str) -> str`: the name CPython stores
  `attribute` under inside `class class_name`. Names starting with `__` and not ending with
  `__` become `_<class name without leading underscores><attribute>`. Everything else is
  returned unchanged, including dunders, single-underscore names, and any name whose class
  name is only underscores.
- `class Ledger(opening: int = 0)` keeps its balance in `self.__balance`.
  - `balance` read-only property.
  - `deposit(amount) -> int` and `withdraw(amount) -> int` return the new balance.
  - Non-positive amounts or a negative opening balance raise `ValueError`.
  - Overdrawing raises `InsufficientFunds` (an `Exception` subclass you define) and leaves the balance unchanged.

## Part 5 · Module namespaces and `scoped_override`

- `scoped_override(module, name, value)`: context manager. Inside the block
  `getattr(module, name) == value`. On exit the previous value is restored. If `name` did
  not exist before, it is removed again. Restoration happens even when the block raises,
  and nested overrides unwind correctly. If the block itself deleted the name, exit still
  restores the previous value.
- `public_names(namespace: Mapping[str, object]) -> list[str]`: sorted keys that do not start
  with `_` and whose values are not modules.

## Part 6 · Executing config files into a fresh namespace

- `class ConfigError(Exception)`.
- `load_config(path: str | Path) -> dict[str, Any]`: executes the file in a brand-new
  namespace (nothing from your module is visible to it) and returns only the names that are
  `str.isupper()` and do not start with `_`, e.g. `MAX_RETRIES`, `DB2`.
  - Any exception while compiling or executing (including `SyntaxError`) raises
    `ConfigError` whose message contains the path, chained with `from exc`.
  - A missing file propagates `FileNotFoundError`.

## Part 7 · Restricted formula evaluation

- Exceptions: `FormulaError(Exception)` (base), `UnknownNameError(FormulaError)`,
  `ForbiddenSyntaxError(FormulaError)`.
- `DEFAULT_FUNCTIONS`: dict with `ABS`, `MIN`, `MAX`, `ROUND`.
- `evaluate(expression: str, cells: Mapping[str, float], functions: Mapping[str, Callable] | None = None) -> float`
  - Supported grammar: int/float literals, cell names, calls to whitelisted functions with
    positional arguments, unary `+`/`-`, binary `+ - * / // %`, parentheses.
  - Anything else (attribute access, subscripts, `**`, ternaries, strings, `True`/`False`,
    lambdas, comprehensions, keyword arguments, dunder names) raises `ForbiddenSyntaxError`.
  - A name used as a value must be in `cells`; a name used as a call target must be in
    `functions` (defaults merged with the argument). Otherwise `UnknownNameError`. Builtins
    are not reachable: `len(A1)` is an unknown name.
  - Cell and function names must not overlap; overlap raises `ValueError`.
  - Syntax errors, division by zero, and any `TypeError`/`ValueError` raised during evaluation
    are wrapped in `FormulaError` (chained). The result is always a `float`.

## Concepts exercised

- LEGB resolution order and `NameError` on unresolved names
- Closures, `nonlocal`, and factory functions that give each closure its own state
- Late binding of loop variables in closures, and the standard fixes
- `global` in a decorator-based module registry; module globals as singletons
- Class-body execution scope, comprehension scoping rules, and class attribute leakage
- Name mangling (`__x` → `_Class__x`) and its rules
- `vars(module)` / `__dict__` manipulation and context-manager restoration
- `compile`/`exec` into a fresh namespace; `eval` with an explicit `__builtins__` mapping
- AST-level validation before evaluation

## Run

```bash
uv run pytest exercises/03_namespaces_and_scope                 # your implementation
uv run pytest exercises/03_namespaces_and_scope -k part4        # one part
uv run pytest exercises/03_namespaces_and_scope --solutions     # reference implementation
```

## Stretch (untested)

- Make `evaluate` safe against resource exhaustion: cap AST node count and the magnitude of intermediate integers.
- Make `scoped_override` also work for classes via `setattr`/`delattr` on the target object.
- Replace the plugin registry's `global` with a small class and compare the two designs.
- Explain what `exec` does when the globals dict has no `__builtins__` key, and why the config loader still gets the full builtins.
