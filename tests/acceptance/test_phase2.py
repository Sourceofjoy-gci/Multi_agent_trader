"""Phase 2 acceptance: features are reproducible and cannot bypass the store.

The general import rule lives in ``test_architecture.py``; this file asserts
what Phase 2 itself promised.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FEATURES = PROJECT_ROOT / "src" / "trading_house" / "features"
INDICATORS = FEATURES / "indicators"


def _module_functions(path: Path) -> list[ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node for node in tree.body if isinstance(node, ast.FunctionDef)]


def test_every_indicator_returns_decimal() -> None:
    """Features flow into stop distance and then into lot size. A float here
    would put float back on the money path Phase 0 spent effort removing."""

    checked = 0
    for path in sorted(INDICATORS.glob("*.py")):
        for function in _module_functions(path):
            if function.name.startswith("_"):
                continue
            assert function.returns is not None, f"{path.name}:{function.name} unannotated"
            assert ast.unparse(function.returns) == "Decimal", (
                f"{path.name}:{function.name} returns {ast.unparse(function.returns)}"
            )
            checked += 1
    assert checked >= 3, "expected true_range, wilder_atr and median_spread_points"


def test_no_feature_module_can_reach_the_store() -> None:
    """Only the engine holds the store. If a feature module could fetch, a caller
    could obtain a feature without a point-in-time read and without the fixed
    window, which is exactly what I-18 forbids.

    The engine depends on ``BarReader``, not ``BarStore`` (see
    ``test_the_engine_holds_the_store`` below) -- so every feature module must carry
    neither name, not just the one the engine happens to use. ``engine.py`` is
    exempt: it is where the store lives and where ``BarReader`` is declared.
    """

    for path in sorted(FEATURES.rglob("*.py")):
        if path.name == "engine.py":
            continue
        source = path.read_text(encoding="utf-8")
        assert "BarReader" not in source, f"{path.name} reaches the store's read protocol"
        assert "BarStore" not in source, f"{path.name} reaches the store"
        assert "marketdata.store" not in source, f"{path.name} imports the store module"


def test_the_engine_holds_the_store() -> None:
    """The guard above is only meaningful while something still does.

    The engine depends on ``BarReader``, a narrow two-method protocol
    (``bars()``, ``coverage()``) -- not ``BarStore`` itself, which also
    carries ``record_run``, ``append_bars`` and ``finalize_run``: write
    methods a feature engine must never reach. Asserting the narrower name
    is what keeps this guard meaningful; asserting the wider one would pass
    vacuously even if the engine depended on nothing at all.
    """

    assert "BarReader" in (FEATURES / "engine.py").read_text(encoding="utf-8")


def test_features_reaches_marketdata_and_nothing_else() -> None:
    """Phase-level restatement of the rule test_architecture.py owns."""

    forbidden = ("trading_house.brokers", "trading_house.risk", "trading_house.execution")
    for path in sorted(FEATURES.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for name in forbidden:
            assert name not in source, f"{path.name} imports {name}"
