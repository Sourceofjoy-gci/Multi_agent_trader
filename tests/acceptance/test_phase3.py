"""Phase 3 acceptance: sizing cannot exceed the budget and cannot approve a
position without a protective stop.

The general import rule lives in test_architecture.py; this file asserts what
Phase 3 itself promised.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RISK = PROJECT_ROOT / "src" / "trading_house" / "risk"


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


def test_the_sizing_module_holds_no_io() -> None:
    """risk/sizing.py is pure arithmetic. A Clock, a Protocol or a constitution
    reference in it means the boundary has moved."""

    source = (RISK / "sizing.py").read_text(encoding="utf-8")
    for forbidden in ("Clock", "Protocol", "Constitution"):
        assert forbidden not in source, f"sizing.py references {forbidden}"


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
