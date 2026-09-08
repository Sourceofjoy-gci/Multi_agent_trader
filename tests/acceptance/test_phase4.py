"""Phase 4 acceptance: the venue is called from exactly one, loop-free place,
and execution/ never imports a broker directly.

The general import-boundary rule lives in test_architecture.py; this file
asserts what Phase 4 itself promised.
"""

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXECUTION = PROJECT_ROOT / "src" / "trading_house" / "execution"


def test_no_execution_module_imports_a_broker() -> None:
    """execution/ declares the venue port it needs; the adapter satisfies it
    structurally. An import here would make the order manager untestable
    without MetaTrader5 installed."""

    for path in sorted(EXECUTION.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for forbidden in (
            "trading_house.brokers",
            "trading_house.risk",
            "trading_house.marketdata",
        ):
            assert forbidden not in source, f"{path.name} imports {forbidden}"


def test_the_manager_cannot_resend() -> None:
    """Grep the manager for a retry loop. I-6 is that a lost response never
    doubles a position, and the simplest way to break it is a `for attempt in
    range(...)` around the send."""

    source = (EXECUTION / "manager.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    sends = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "submit"
    ]
    assert len(sends) == 1, "the venue must be called from exactly one place"
    for node in ast.walk(tree):
        if isinstance(node, ast.For | ast.While):
            assert not any(
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "submit"
                for inner in ast.walk(node)
            ), "the venue is called inside a loop"
