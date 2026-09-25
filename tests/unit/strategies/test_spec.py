import pytest
from pydantic import ValidationError

from trading_house.strategies.spec import StrategySpec


def _spec_fields() -> dict[str, object]:
    return {
        "economic_rationale": "Overnight repricing can continue when London liquidity returns.",
        "universe": ("fx.eurusd",),
        "trading_horizon": "The London session, from 07:00 through 16:00 UTC.",
        "entry_rule": "At the first closed London bar, trade the sign of the prior session return.",
        "exit_rule": (
            "Use the risk-engine stop, the 16:00 UTC time stop, and the selected exit arm."
        ),
        "cost_model_description": (
            "Declare spread, commission, slippage, and swap; do not default costs."
        ),
        "capacity_model": "Capacity is not modelled and is explicitly a non-promise.",
        "invalidation": (
            "Rolling out-of-sample monitoring invalidates the thesis when its edge disappears."
        ),
        "regime_constraints": (
            "The risk gates and the fixed UTC session windows constrain eligibility."
        ),
        "trail_decision": "The A/B has not yet run; no trailing decision is recorded.",
        "trial_count": 3,
        "versioning": (
            "Record strategy id and version, constitution hash, contract digest, and result digest."
        ),
    }


def test_a_complete_strategy_spec_validates() -> None:
    assert tuple(StrategySpec.model_fields) == tuple(_spec_fields())
    assert StrategySpec(**_spec_fields()).trial_count == 3


def test_a_spec_missing_any_mandatory_item_is_refused() -> None:
    complete = _spec_fields()

    for omitted in complete:
        with pytest.raises(ValidationError):
            StrategySpec(**{key: value for key, value in complete.items() if key != omitted})


def test_a_blank_trail_decision_is_refused() -> None:
    with pytest.raises(ValidationError):
        StrategySpec(**{**_spec_fields(), "trail_decision": ""})


@pytest.mark.parametrize(
    ("field", "value"),
    [("universe", ()), ("trial_count", 0)],
)
def test_a_spec_refuses_an_empty_universe_or_trial_count(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        StrategySpec(**{**_spec_fields(), field: value})
