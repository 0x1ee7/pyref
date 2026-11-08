"""Shared pytest plumbing for the pyref exercises.

Every exercise lives in ``exercises/<NN_topic>/``. Tests never import your
code directly; they request the ``main`` fixture, which imports a *fresh*
copy of ``main.py`` for every test (so module-level state never leaks
between tests). Missing names therefore fail one test at a time instead of
breaking collection of the whole file.

Run against the reference implementations with ``pytest --solutions``.
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent
EXERCISES = ROOT / "exercises"
SOLUTIONS = ROOT / "solutions"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--solutions",
        action="store_true",
        default=False,
        help="Grade the reference implementations in solutions/ instead of exercises/.",
    )


_USER_DIRS = tuple(os.path.normcase(str(d)) + os.sep for d in (EXERCISES, SOLUTIONS))


def _is_user_module(module: ModuleType) -> bool:
    """True for modules imported from exercises/ or solutions/ that are not test plumbing."""
    file = getattr(module, "__file__", None)
    if not file or not os.path.normcase(os.path.abspath(file)).startswith(_USER_DIRS):
        return False
    name = os.path.basename(file)
    return not (name.startswith("test_") or name == "conftest.py")


def _purge_user_modules() -> None:
    for name, module in list(sys.modules.items()):
        if module is not None and _is_user_module(module):
            del sys.modules[name]


@pytest.fixture
def exercise_dir(request: pytest.FixtureRequest) -> Path:
    """``exercises/<NN_topic>/`` for the requesting test: provided assets live here."""
    relative = request.path.resolve().relative_to(EXERCISES)
    return EXERCISES / relative.parts[0]


@pytest.fixture
def workdir(request: pytest.FixtureRequest, exercise_dir: Path) -> Path:
    """Directory holding the code being graded (yours, or the reference solution)."""
    if not request.config.getoption("--solutions"):
        return exercise_dir
    solution_dir = SOLUTIONS / exercise_dir.name
    if not solution_dir.is_dir():
        pytest.fail(f"no reference solution at {solution_dir}")
    return solution_dir


@pytest.fixture
def main(workdir: Path, exercise_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    """Freshly imported ``main`` module from :func:`workdir`.

    It is registered as ``sys.modules["main"]`` with ``workdir`` (then
    ``exercise_dir``) on ``sys.path``, so sibling modules import normally and
    objects defined in ``main`` pickle across process boundaries.
    """
    monkeypatch.syspath_prepend(str(exercise_dir))
    if workdir != exercise_dir:
        monkeypatch.syspath_prepend(str(workdir))
    _purge_user_modules()
    try:
        yield importlib.import_module("main")
    finally:
        _purge_user_modules()
