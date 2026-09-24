"""Strict, immutable contracts for market analysis and trade proposals."""

import math
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal, Self, cast

from pydantic import (
    Field,
    JsonValue,
    PositiveInt,
    TypeAdapter,
    field_validator,
    model_validator,
)

from trading_house.core.clock import ensure_utc
from trading_house.core.errors import TimestampError
from trading_house.core.freezing import freeze_json
from trading_house.core.values import (
    BasisPoints,
    BookId,
    InstrumentId,
    IntentState,
    PositiveQuantity,
    Price,
    Quantity,
    TimeInForce,
)
from trading_house.core.values import (
    CanonicalModel as CanonicalModel,
)
from trading_house.core.values import (
    FiniteFloat as FiniteFloat,
)
from trading_house.core.values import (
    NonEmptyStr as NonEmptyStr,
)
from trading_house.core.values import (
    NonNegativeFiniteFloat as NonNegativeFiniteFloat,
)
from trading_house.core.values import (
    PositiveFiniteFloat as PositiveFiniteFloat,
)
from trading_house.core.values import (
    Probability as Probability,
)
from trading_house.core.venue import ExecutionOutcome, VenueRef


class Side(str, Enum):  # noqa: UP042
    BUY = "BUY"
    SELL = "SELL"


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

    @field_validator("probabilities")
    @classmethod
    def freeze_probabilities(cls, value: dict[str, float]) -> dict[str, float]:
        return cast(dict[str, float], freeze_json(cast(JsonValue, value)))

    @model_validator(mode="after")
    def probabilities_sum_to_one(self) -> Self:
        if not math.isclose(sum(self.probabilities.values()), 1.0, abs_tol=1e-9):
            raise ValueError("regime probabilities must sum to 1")
        return self


class TradeProposal(Stamped):
    proposal_id: NonEmptyStr
    strategy_id: NonEmptyStr
    strategy_version: NonEmptyStr
    book: BookId
    instrument_id: InstrumentId
    side: Side
    horizon_seconds: PositiveInt
    entry_condition: NonEmptyStr
    entry_price_ref: Price
    invalidation_price: Price
    max_holding_seconds: PositiveInt
    expected_return_bps: FiniteFloat
    expected_return_stdev_bps: NonNegativeFiniteFloat
    expected_cost_bps: NonNegativeFiniteFloat
    # The strategy's declared swap component, already included in
    # expected_cost_bps above and carried separately here because
    # max_swap_cost_pct_of_expected_edge needs it alone. A default of zero is
    # not "no opinion" -- it is a claim an intraday strategy is entitled to
    # make, and one the swap gate then checks rather than skips.
    expected_swap_cost_bps: NonNegativeFiniteFloat = 0.0
    win_probability: Probability
    calibration_id: NonEmptyStr
    required_liquidity: PositiveQuantity
    regime_ref: NonEmptyStr
    features_snapshot_id: NonEmptyStr
    # An R multiple, not a price. The engine prices it off the stop distance
    # it computed -- never off invalidation_price above -- so the strategy
    # cannot declare a target inconsistent with the stop actually applied.
    # gt=0: zero or negative would price the target at or behind the stop --
    # a real loss the engine would hand back labelled ExitKind.TARGET. There
    # is no upper bound: how far a target can sit is a question about a
    # price being positive, which only the engine's own stop distance can
    # answer, not a multiple ceiling picked in advance.
    target_r_multiple: Annotated[Decimal, Field(gt=0)] | None = None
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
    approved_quantity: PositiveQuantity
    stop_loss_price: Price
    take_profit_price: Price | None
    risk_money: Annotated[Decimal, Field(gt=0)]
    risk_pct_of_book: Annotated[Decimal, Field(gt=0)]


class ApprovedRiskDecision(ExecutableRiskDecision):
    verdict: Literal["APPROVED"]


class ResizedRiskDecision(ExecutableRiskDecision):
    verdict: Literal["RESIZED"]


class RejectedRiskDecision(BaseRiskDecision):
    verdict: Literal["REJECTED"]
    approved_quantity: Quantity
    stop_loss_price: None = None
    take_profit_price: None = None
    # ge=0, le=0 is not a typo: it pins the value to exactly zero. A rejected
    # decision must carry no risk, and Decimal has no Literal[0]-style way to
    # say that, so the range is collapsed to a single permitted value instead.
    # Widening either bound would silently permit non-zero risk on a REJECTED
    # decision.
    risk_money: Annotated[Decimal, Field(ge=0, le=0)]
    risk_pct_of_book: Annotated[Decimal, Field(ge=0, le=0)]

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
    """One idempotent request to change a position. Neutral by construction."""

    intent_id: NonEmptyStr
    proposal_id: NonEmptyStr
    book: BookId
    instrument_id: InstrumentId
    side: Side
    quantity: PositiveQuantity
    stop_loss: Price
    take_profit: Price | None
    time_in_force: TimeInForce
    max_slippage_bps: BasisPoints
    state: IntentState
    t_submit_utc: datetime
    venue_ref: VenueRef | None = None
    outcome: ExecutionOutcome | None = None

    @field_validator("t_submit_utc")
    @classmethod
    def normalize_submit_time(cls, value: datetime) -> datetime:
        try:
            return ensure_utc(value)
        except TimestampError as error:
            raise ValueError(str(error)) from error


class PositionState(CanonicalModel):
    intent_id: NonEmptyStr | None
    strategy_id: NonEmptyStr
    book: BookId
    instrument_id: InstrumentId
    side: Side
    quantity: PositiveQuantity
    open_price: Price
    current_sl: Price
    current_tp: Price | None
    opened_at_utc: datetime
    lifecycle: Literal["OPEN_PROTECTED", "BREAKEVEN_ELIGIBLE", "TRAILING", "EXIT_PENDING", "CLOSED"]
    r_multiple_open: FiniteFloat
    mae_r: FiniteFloat
    mfe_r: FiniteFloat
    initial_risk_distance: Price
    venue_ref: VenueRef

    @field_validator("opened_at_utc")
    @classmethod
    def normalize_opened_time(cls, value: datetime) -> datetime:
        try:
            return ensure_utc(value)
        except TimestampError as error:
            raise ValueError(str(error)) from error
