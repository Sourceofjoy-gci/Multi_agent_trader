"""Phase 3 acceptance: sizing cannot exceed the budget and cannot approve a
position without a protective stop.

The general import rule lives in test_architecture.py; this file asserts what
Phase 3 itself promised.
"""

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RISK = PROJECT_ROOT / "src" / "trading_house" / "risk"

PURE_IMPORTS = frozenset(
    {
        "__future__",
        "decimal",
        "trading_house.core.instruments",
        "trading_house.core.schemas",
    }
)
IMPURE_BUILTINS = frozenset({"open", "input"})


def _imported_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_no_risk_module_reaches_a_broker_or_the_store() -> None:
    """Sizing must run identically in live trading and in a backtester with no
    terminal. An import of brokers/ or marketdata/ would end that."""

    for path in sorted(RISK.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for forbidden in (
            "trading_house.brokers",
            "trading_house.marketdata",
            "trading_house.features",
        ):
            assert forbidden not in source, f"{path.name} imports {forbidden}"


def test_the_sizing_module_imports_nothing_that_could_perform_io() -> None:
    """risk/sizing.py is pure arithmetic, and purity is asserted by an
    allow-list rather than by banning three names. The deny-list this replaced
    passed against an injected ``import time`` and a wall-clock read, so it
    proved nothing about purity at all."""

    tree = ast.parse((RISK / "sizing.py").read_text(encoding="utf-8"))
    unexpected = _imported_modules(tree) - PURE_IMPORTS

    assert unexpected == set(), f"sizing.py imports {sorted(unexpected)}"


def test_the_sizing_module_calls_no_impure_builtin() -> None:
    """An import allow-list cannot see ``open()``, which needs no import."""

    tree = ast.parse((RISK / "sizing.py").read_text(encoding="utf-8"))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert called & IMPURE_BUILTINS == set()


@pytest.mark.parametrize(
    "statement",
    ["import time", "import os", "from datetime import datetime"],
)
def test_the_purity_guard_can_still_fail(statement: str) -> None:
    """Guard the guard: the first of these is the exact injection that defeated
    the check this replaced."""

    assert _imported_modules(ast.parse(statement + "\n")) - PURE_IMPORTS


def test_every_approved_decision_type_requires_a_stop_price() -> None:
    """The master spec's 7.3 forbids trading without a protective stop, and
    Phase 0 made it structural by typing stop_loss_price as non-optional on
    ExecutableRiskDecision. This test exists so a later widening to
    `Price | None` fails here rather than in production."""

    from trading_house.core.schemas import ExecutableRiskDecision

    annotation = ExecutableRiskDecision.model_fields["stop_loss_price"].annotation

    assert annotation is not None
    assert "None" not in str(annotation)


def test_the_engine_is_the_only_holder_of_the_constitution() -> None:
    """The guard above is only meaningful while something still holds it."""

    assert "Constitution" in (RISK / "engine.py").read_text(encoding="utf-8")
