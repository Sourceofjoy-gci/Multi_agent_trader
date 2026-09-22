"""Executable guards for Phase 6's backtester.

Two structural properties that no behavioural test can hold down:

* **No cost defaults to zero.** ``CostModel`` requires every cost it charges,
  and ``backtest run`` requires an option for each. The classic flattering
  backtest is one that silently assumed zero commission (D-5), and a default
  added to either side of that boundary would produce exactly it -- with a
  green suite, because every existing test states all the costs anyway.
* **The simulator does not size.** ``research/backtest/`` calls neither
  ``compute_volume`` nor ``stop_price``. This is the structural form of D-3:
  ``RiskEngine.evaluate_for_execution`` already calls both internally and
  returns ``approved_quantity`` and ``stop_loss_price`` on the decision, so a
  simulator calling them again sizes the position a second time -- and would
  measure a strategy the live system will never trade.

An AST check rather than a grep, because ``engine.py``'s own docstring and one
of its comments name both functions in prose. A textual search would have to
be taught to ignore them, and would then be one edit away from ignoring a real
call.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from trading_house import cli
from trading_house.research.backtest.costs import CostModel

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src" / "trading_house"
# The simulator, plus the composition shim that builds it. The shim lives in
# ops/ for the same reason the position guard's does -- cli.py is already the
# largest module in the package -- and it holds Phase 6's toy strategy, so a
# sizing call added there would be as invisible as one inside the loop itself.
SIZING_SCANNED_ROOTS = (SOURCE_ROOT / "research", SOURCE_ROOT / "ops" / "backtest.py")
SIZING_PRIMITIVES = frozenset({"compute_volume", "stop_price"})
# stress_multiplier is the 1.5x-2x sensitivity knob section 12 asks for, not a
# cost. Every actual cost -- commission, slippage, both swap rates -- and the
# rollover weekday they are charged against must be stated by the caller.
DEFAULTED_COST_FIELDS = frozenset({"stress_multiplier"})


def _parsed(*roots: Path) -> Iterator[tuple[Path, ast.Module]]:
    for root in roots:
        paths = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in paths:
            yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _called_names(tree: ast.Module) -> set[str]:
    """Every callee name in one module, in both call forms.

    ``compute_volume(...)`` is an ``ast.Name`` and ``sizing.compute_volume(...)``
    an ``ast.Attribute``; a detector that saw only one of them would be blind
    to whichever import style the offending edit happened to use.
    """

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def _imported_names(tree: ast.Module) -> set[str]:
    """What the module imported, by its original name.

    Checked alongside the call names so ``from ... import compute_volume as
    _size`` cannot evade the call check by renaming the function at the door.
    """

    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }


def _required_cost_fields() -> list[str]:
    return sorted(name for name, field in CostModel.model_fields.items() if field.is_required())


def test_the_scanned_backtest_source_is_not_empty() -> None:
    """A silent glob failure must not make the sizing guard vacuous."""

    assert len(list(_parsed(*SIZING_SCANNED_ROOTS))) >= 5


def test_no_backtest_module_sizes_a_position() -> None:
    """D-3, structurally. The risk engine sizes; the simulator reads what it
    returned. Two sizings cannot disagree quietly for long, and the one that
    would win here is the one no live order ever goes through."""

    offenders: dict[str, list[str]] = {}
    for path, tree in _parsed(*SIZING_SCANNED_ROOTS):
        found = (_called_names(tree) | _imported_names(tree)) & SIZING_PRIMITIVES
        if found:
            offenders[path.relative_to(PROJECT_ROOT).as_posix()] = sorted(found)

    assert offenders == {}


@pytest.mark.parametrize(
    "statement",
    [
        "compute_volume(equity, stop, contract)",
        "stop_price(reference, distance, side)",
        "sizing.compute_volume(equity)",
        "trading_house.risk.sizing.stop_price(reference)",
        "from trading_house.risk.sizing import compute_volume",
        "from trading_house.risk.sizing import stop_price as _place_the_stop",
    ],
)
def test_the_sizing_guard_can_still_fail(statement: str) -> None:
    """Guard the guard: prove the detector flags a real sizing call -- in
    either call form, and through a rename -- so a passing check reflects a
    simulator that does not size rather than a detector that stopped looking."""

    tree = ast.parse(statement + "\n")

    assert (_called_names(tree) | _imported_names(tree)) & SIZING_PRIMITIVES


def test_the_sizing_guard_ignores_the_prose_that_names_both_functions() -> None:
    """Guard the guard, the other direction: ``engine.py`` names both
    functions in a comment and a docstring explaining why it does NOT call
    them. A grep would have to special-case that; the AST simply does not see
    it, and this pins that distinction rather than leaving it to luck."""

    source = (SOURCE_ROOT / "research" / "backtest" / "engine.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    assert "compute_volume" in source
    assert (_called_names(tree) | _imported_names(tree)) & SIZING_PRIMITIVES == set()


def _every_cost_stated() -> dict[str, object]:
    """Decimals, not strings. ``CanonicalModel`` is strict, so a string here
    would raise ``ValidationError`` whether or not a field was omitted -- and
    every case below would pass for the wrong reason."""

    return {
        "commission_per_lot_per_side": Decimal("3.50"),
        "slippage_points_per_side": Decimal("0.4"),
        "swap_long_points_per_day": Decimal("-0.80"),
        "swap_short_points_per_day": Decimal("0.30"),
        "triple_swap_weekday": 2,
    }


def test_the_complete_cost_set_constructs() -> None:
    """Guard the guard: the case below is only evidence about the omitted
    field while the set it omits from is otherwise valid."""

    assert CostModel(**_every_cost_stated()).stress_multiplier == Decimal(1)  # type: ignore[arg-type]


@pytest.mark.parametrize("omitted", _required_cost_fields())
def test_every_cost_is_required(omitted: str) -> None:
    """Construct the model with one cost left out and it must refuse.

    Parametrized off ``model_fields`` rather than a written list, so a cost
    added later is covered the day it appears instead of the day somebody
    remembers to extend this test. ``match`` pins the complaint to the field
    that was actually removed.
    """

    stated = _every_cost_stated()
    del stated[omitted]

    with pytest.raises(ValidationError, match=omitted):
        CostModel(**stated)  # type: ignore[arg-type]


def test_the_only_defaulted_field_is_the_stress_multiplier() -> None:
    """The positive form of the check above: it fails on a default nobody
    thought to test for, including one added to a cost invented later."""

    defaulted = {name for name, field in CostModel.model_fields.items() if not field.is_required()}

    assert defaulted == DEFAULTED_COST_FIELDS


@pytest.mark.parametrize("field_name", _required_cost_fields())
def test_the_command_requires_every_cost_the_model_requires(field_name: str) -> None:
    """The boundary the model cannot defend on its own.

    ``CostModel`` refusing a missing cost is worth nothing if ``backtest run``
    supplies one from a CLI default -- the model would be handed a complete
    set and a run would silently charge whatever the default said. Derived
    from ``model_fields`` so the two cannot drift.
    """

    parameter = inspect.signature(cli.backtest_run).parameters[field_name]

    assert parameter.default is inspect.Parameter.empty
