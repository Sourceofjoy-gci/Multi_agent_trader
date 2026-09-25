"""The validated artifact every registered strategy must ship."""

from pydantic import Field, PositiveInt

from trading_house.core.values import CanonicalModel, InstrumentId, NonEmptyStr


class StrategySpec(CanonicalModel):
    economic_rationale: NonEmptyStr
    universe: tuple[InstrumentId, ...] = Field(min_length=1)
    trading_horizon: NonEmptyStr
    entry_rule: NonEmptyStr
    exit_rule: NonEmptyStr
    cost_model_description: NonEmptyStr
    capacity_model: NonEmptyStr
    invalidation: NonEmptyStr
    regime_constraints: NonEmptyStr
    trail_decision: NonEmptyStr
    trial_count: PositiveInt
    versioning: NonEmptyStr
