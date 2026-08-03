"""Strict, immutable contracts for market analysis and trade proposals."""

import math
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from trading_house.core.clock import ensure_utc
from trading_house.core.errors import TimestampError

NonEmptyStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)
]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
PositiveFiniteFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]
NonNegativeFiniteFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class Book(str, Enum):  # noqa: UP042
    CORE = "core"
    SLEEVE = "sleeve"


class Side(str, Enum):  # noqa: UP042
    BUY = "BUY"
    SELL = "SELL"


class CanonicalModel(BaseModel):
    """Base model for canonical data exchanged between trading services."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class Stamped(CanonicalModel):
    event_time: datetime
    availability_time: datetime
    processing_time: datetime
    source: NonEmptyStr
    revision_id: NonEmptyStr | None = None
    quality_flags: tuple[NonEmptyStr, ...] = ()

    @field_validator("event_time", "availability_time", "processing_time")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        try:
            return ensure_utc(value)
        except TimestampError as error:
            raise ValueError(str(error)) from error

    @model_validator(mode="after")
    def timestamps_are_ordered(self) -> Self:
        if self.availability_time < self.event_time:
            raise ValueError("availability_time must not precede event_time")
        if self.processing_time < self.availability_time:
            raise ValueError("processing_time must not precede availability_time")
        return self


class RegimeAssessment(Stamped):
    symbol: NonEmptyStr
    volatility_state: Literal["low", "normal", "high", "extreme"]
    trend_state: Literal["down", "range", "up"]
    liquidity_state: Literal["thin", "normal", "deep"]
    probabilities: dict[NonEmptyStr, Probability]
    uncertainty: Probability

    @model_validator(mode="after")
    def probabilities_sum_to_one(self) -> Self:
        if not math.isclose(sum(self.probabilities.values()), 1.0, abs_tol=1e-9):
            raise ValueError("regime probabilities must sum to 1")
        return self


class TradeProposal(Stamped):
    proposal_id: NonEmptyStr
    strategy_id: NonEmptyStr
    strategy_version: NonEmptyStr
    book: Book
    symbol: NonEmptyStr
    side: Side
    horizon_seconds: PositiveInt
    entry_condition: NonEmptyStr
    entry_price_ref: PositiveFiniteFloat
    invalidation_price: PositiveFiniteFloat
    max_holding_seconds: PositiveInt
    expected_return_bps: FiniteFloat
    expected_return_stdev_bps: NonNegativeFiniteFloat
    expected_cost_bps: NonNegativeFiniteFloat
    win_probability: Probability
    calibration_id: NonEmptyStr
    required_liquidity_lots: PositiveFiniteFloat
    regime_ref: NonEmptyStr
    features_snapshot_id: NonEmptyStr
    rationale: NonEmptyStr | None = None

    @field_validator("win_probability")
    @classmethod
    def win_probability_is_not_degenerate(cls, value: float) -> float:
        if value in (0.0, 1.0):
            raise ValueError("degenerate probability rejected")
        return value

    @model_validator(mode="after")
    def invalidation_is_on_loss_side(self) -> Self:
        if self.side is Side.BUY and self.invalidation_price >= self.entry_price_ref:
            raise ValueError("BUY invalidation must be below entry_price_ref")
        if self.side is Side.SELL and self.invalidation_price <= self.entry_price_ref:
            raise ValueError("SELL invalidation must be above entry_price_ref")
        return self


class AgentOpinion(Stamped):
    agent_role: NonEmptyStr
    subject_id: NonEmptyStr
    stance: Literal["FOR", "AGAINST", "ABSTAIN"]
    evidence_for: tuple[NonEmptyStr, ...] = ()
    evidence_against: tuple[NonEmptyStr, ...] = ()
    missing_information: tuple[NonEmptyStr, ...] = ()
    confidence: Probability


class BaseRiskDecision(CanonicalModel):
    proposal_id: NonEmptyStr
    reasons: tuple[NonEmptyStr, ...]
    checks_passed: tuple[NonEmptyStr, ...]
    constitution_version: PositiveInt


class ExecutableRiskDecision(BaseRiskDecision):
    approved_volume_lots: PositiveFiniteFloat
    stop_loss_price: PositiveFiniteFloat
    take_profit_price: PositiveFiniteFloat | None
    risk_money: PositiveFiniteFloat
    risk_pct_of_book: PositiveFiniteFloat


class ApprovedRiskDecision(ExecutableRiskDecision):
    verdict: Literal["APPROVED"]


class ResizedRiskDecision(ExecutableRiskDecision):
    verdict: Literal["RESIZED"]


class RejectedRiskDecision(BaseRiskDecision):
    verdict: Literal["REJECTED"]
    approved_volume_lots: Literal[0.0]  # type: ignore[valid-type]
    stop_loss_price: None = None
    take_profit_price: None = None
    risk_money: Literal[0.0]  # type: ignore[valid-type]
    risk_pct_of_book: Literal[0.0]  # type: ignore[valid-type]

    @field_validator("approved_volume_lots", "risk_money", "risk_pct_of_book", mode="before")
    @classmethod
    def zero_fields_are_not_booleans(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("boolean values are not valid zero fields")
        return value

    @model_validator(mode="after")
    def has_rejection_reason(self) -> Self:
        if not self.reasons:
            raise ValueError("rejected decisions require at least one reason")
        return self


RiskDecision = Annotated[
    ApprovedRiskDecision | ResizedRiskDecision | RejectedRiskDecision,
    Field(discriminator="verdict"),
]
RISK_DECISION_ADAPTER: TypeAdapter[RiskDecision] = TypeAdapter(RiskDecision)


class OrderIntent(CanonicalModel):
    intent_id: NonEmptyStr
    proposal_id: NonEmptyStr
    magic: PositiveInt
    symbol: NonEmptyStr
    side: Side
    volume: PositiveFiniteFloat
    sl: PositiveFiniteFloat
    tp: PositiveFiniteFloat | None
    deviation_points: NonNegativeInt
    filling: NonNegativeInt
    state: Literal["SUBMITTING", "CONFIRMED", "UNKNOWN", "RECONCILING", "FAILED", "REJECTED"]
    t_submit_utc: datetime
    broker_order_ticket: PositiveInt | None = None
    broker_position_ticket: PositiveInt | None = None
    fill_price: PositiveFiniteFloat | None = None
    retcode: int | None = None

    @field_validator("t_submit_utc")
    @classmethod
    def normalize_submit_time(cls, value: datetime) -> datetime:
        try:
            return ensure_utc(value)
        except TimestampError as error:
            raise ValueError(str(error)) from error


class PositionState(CanonicalModel):
    position_ticket: PositiveInt
    intent_id: NonEmptyStr | None
    strategy_id: NonEmptyStr
    book: Book
    symbol: NonEmptyStr
    side: Side
    volume: PositiveFiniteFloat
    open_price: PositiveFiniteFloat
    current_sl: PositiveFiniteFloat
    current_tp: PositiveFiniteFloat | None
    opened_at_utc: datetime
    lifecycle: Literal[
        "OPEN_PROTECTED", "BREAKEVEN_ELIGIBLE", "TRAILING", "EXIT_PENDING", "CLOSED"
    ]
    r_multiple_open: FiniteFloat
    mae_r: FiniteFloat
    mfe_r: FiniteFloat
    initial_risk_distance: PositiveFiniteFloat

    @field_validator("opened_at_utc")
    @classmethod
    def normalize_opened_time(cls, value: datetime) -> datetime:
        try:
            return ensure_utc(value)
        except TimestampError as error:
            raise ValueError(str(error)) from error
