import pytest

from trading_house.core.freezing import FrozenDict, FrozenList, freeze_json


def test_frozen_dict_rejects_mutation() -> None:
    frozen = freeze_json({"a": 1})
    assert isinstance(frozen, FrozenDict)
    with pytest.raises(TypeError):
        frozen["a"] = 2


def test_frozen_list_rejects_mutation() -> None:
    frozen = freeze_json([1, 2])
    assert isinstance(frozen, FrozenList)
    with pytest.raises(TypeError):
        frozen.append(3)


def test_freezing_is_recursive() -> None:
    frozen = freeze_json({"outer": {"inner": [1]}})
    with pytest.raises(TypeError):
        frozen["outer"]["inner"].append(2)


def test_regime_probabilities_cannot_be_mutated() -> None:
    """The review finding: a frozen model still allowed dict mutation."""

    from datetime import UTC, datetime

    from trading_house.core.schemas import RegimeAssessment

    when = datetime(2026, 8, 23, 9, 0, tzinfo=UTC)
    model = RegimeAssessment(
        event_time=when,
        availability_time=when,
        processing_time=when,
        source="test",
        symbol="EURUSD",
        volatility_state="normal",
        trend_state="up",
        liquidity_state="normal",
        probabilities={"up": 1.0},
        uncertainty=0.5,
    )

    with pytest.raises(TypeError):
        model.probabilities["injected"] = 99.0
    assert model.probabilities == {"up": 1.0}
