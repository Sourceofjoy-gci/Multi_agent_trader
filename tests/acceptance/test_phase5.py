"""Executable guards for Phase 5's position guard.

Two structural properties that no behavioural test can hold down, because
both are about what the source is *shaped* like rather than what one run
does:

* ``execution/`` still reaches only ``core/``, ``database/`` and itself. This
  is the positive form of ``test_architecture.py``'s denylist -- it fails on
  a package nobody thought to forbid, which is how the guard's own
  ``ProtectionPort`` shim came to live outside this tree.
* ``amend_protection`` -- the one call in this system that moves a live
  position's stop -- is called from exactly one place, and no retry loop
  surrounds it.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src" / "trading_house"
EXECUTION_ROOT = SOURCE_ROOT / "execution"
# execution/ owns the ports it needs and lets the composition root satisfy
# them, so this is the whole of what it may reach inside this project.
EXECUTION_ALLOWED = ("trading_house.core", "trading_house.database", "trading_house.execution")
AMEND = "amend_protection"
LOOPS = (ast.For, ast.AsyncFor, ast.While)
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _parsed(root: Path) -> Iterator[tuple[Path, ast.Module]]:
    for path in sorted(root.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _project_imports(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return {module for module in modules if module.split(".")[0] == "trading_house"}


def _amend_calls(tree: ast.Module) -> list[ast.Call]:
    """Every ``<something>.amend_protection(...)`` call in one module."""

    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == AMEND
    ]


def _parents(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _loops_between_the_call_and_its_function(tree: ast.Module, call: ast.Call) -> list[str]:
    """The loops enclosing ``call`` *inside its own function*, walking up and
    stopping at the function that contains it.

    Stopping there is the whole point. ``cycle()`` iterates over positions and
    calls ``_process`` for each, and ``_process`` sends the amend -- that outer
    loop is the guard doing its job once per position. What must not exist is a
    loop between the call and the top of the function holding it: that is a
    retry, and D-7's "two failed restores escalate" is a counter across cycles,
    never a resend within one.
    """

    parents = _parents(tree)
    loops: list[str] = []
    node: ast.AST = call
    while (parent := parents.get(node)) is not None:
        if isinstance(parent, LOOPS):
            loops.append(type(parent).__name__)
        if isinstance(parent, FUNCTIONS):
            break
        node = parent
    return loops


def test_no_execution_module_reaches_outside_core_database_and_itself() -> None:
    """A denylist only catches the packages someone thought of. The guard's
    ``ProtectionPort`` needs ``brokers/`` and ``constitution/``, and nothing
    forbade the second -- so the shim lives in ``ops/`` and this test is what
    keeps it from drifting back in."""

    offenders: dict[str, list[str]] = {}
    for path, tree in _parsed(EXECUTION_ROOT):
        reached = sorted(
            module for module in _project_imports(tree) if not module.startswith(EXECUTION_ALLOWED)
        )
        if reached:
            offenders[path.relative_to(PROJECT_ROOT).as_posix()] = reached

    assert offenders == {}


def test_a_live_stop_is_moved_from_exactly_one_place_in_execution() -> None:
    """One call site is what makes "the guard attempts an amend at most once
    per position per cycle" a property of the code rather than of a reviewer's
    attention. A second caller elsewhere in the daemon could resend a stop the
    first one already sent, and no behavioural test of either caller would
    notice."""

    call_sites = {
        path.relative_to(PROJECT_ROOT).as_posix(): len(_amend_calls(tree))
        for path, tree in _parsed(EXECUTION_ROOT)
        if _amend_calls(tree)
    }

    assert call_sites == {"src/trading_house/execution/loop.py": 1}


def test_no_retry_loop_surrounds_the_amend() -> None:
    """See ``_loops_between_the_call_and_its_function``: the per-position loop
    in ``cycle()`` is legitimate, a loop inside ``_process`` would not be."""

    for path, tree in _parsed(EXECUTION_ROOT):
        for call in _amend_calls(tree):
            assert _loops_between_the_call_and_its_function(tree, call) == [], (
                f"{path.relative_to(PROJECT_ROOT).as_posix()}:{call.lineno} "
                "amends inside a loop within its own function"
            )


@pytest.mark.parametrize(
    ("source", "call_sites", "loops"),
    [
        pytest.param("def f(self):\n    self.venue.amend_protection(a, b, c)\n", 1, [], id="plain"),
        pytest.param(
            "def f(self):\n"
            "    for position in positions:\n"
            "        self.venue.amend_protection(a, b, c)\n",
            1,
            ["For"],
            id="for-in-the-same-function",
        ),
        pytest.param(
            "def f(self):\n    while not done:\n        self.venue.amend_protection(a, b, c)\n",
            1,
            ["While"],
            id="retry-while",
        ),
        pytest.param(
            "def outer(self):\n"
            "    for position in positions:\n"
            "        self.process(position)\n"
            "def process(self, position):\n"
            "    self.venue.amend_protection(a, b, c)\n",
            1,
            [],
            id="loop-in-the-caller-is-legitimate",
        ),
        pytest.param(
            "def f(self):\n"
            "    self.venue.amend_protection(a, b, c)\n"
            "def g(self):\n"
            "    self.venue.amend_protection(a, b, c)\n",
            2,
            [],
            id="two-call-sites",
        ),
    ],
)
def test_the_amend_detectors_can_still_fail(source: str, call_sites: int, loops: list[str]) -> None:
    """Guard the guard: prove both detectors see a real second call site and a
    real retry loop, so a passing check reflects the daemon's shape rather than
    an AST walk that stopped looking."""

    tree = ast.parse(source)
    calls = _amend_calls(tree)

    assert len(calls) == call_sites
    assert _loops_between_the_call_and_its_function(tree, calls[-1]) == loops
