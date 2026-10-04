"""Shared exit policies and the strategy port used by the backtester."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Protocol

from pydantic import Field, field_validator

from trading_house.core.schemas import TradeProposal
from trading_house.core.snapshot import FeatureBlock, FeatureSnapshot
from trading_house.core.values import BookId, CanonicalModel, NonNegativeDecimal, PositiveDecimal


def _canonical_decimal(value: Decimal) -> Decimal:
    if value == 0:
        return Decimal(0)
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return Decimal(text)


class NoExitPolicy(CanonicalModel):
    """The baseline arm: the engine's own stop and the time stop, nothing else.

    Not an absence of policy. Spec section 9.2's honest note is that trailing
    does not universally improve expectancy -- it tends to help on swing
    horizons and to degrade scalping ones by converting winners into
    scratches -- so every strategy must A/B trail against fixed target and
    record the result. This is the arm the other two are measured against, and
    a strategy that names it has declared a position rather than skipped one.
    """

    kind: Literal["none"]


class FixedTargetPolicy(CanonicalModel):
    """Exit at a take-profit priced at ``r_multiple`` times the risk.

    The strategy declares the multiple; ``RiskEngine`` prices it, off the stop
    distance it actually emitted rather than off the strategy's own
    invalidation price. One place computes every price level, which is D-3's
    rule for sizing applied to the level that pays.
    """

    kind: Literal["fixed_target"]
    r_multiple: PositiveDecimal

    @field_validator("r_multiple")
    @classmethod
    def canonicalize_r_multiple(cls, value: Decimal) -> Decimal:
        return _canonical_decimal(value)


class ChandelierPolicy(CanonicalModel):
    """Trail the stop ``atr_multiple`` ATRs behind the bar's extreme.

    ``min_step_points`` is the hysteresis band, in MT5 points rather than
    price: a candidate nearer the current stop than this is ignored. Zero is
    legal and means "move on every improvement", which is the right setting
    for a simulator that pays no per-modification cost and the wrong one for
    the live guard, where it is a request to the broker on every tick.
    """

    kind: Literal["chandelier"]
    atr_multiple: PositiveDecimal
    min_step_points: NonNegativeDecimal

    @field_validator("atr_multiple", "min_step_points")
    @classmethod
    def canonicalize_decimals(cls, value: Decimal) -> Decimal:
        return _canonical_decimal(value)


ExitPolicy = Annotated[
    NoExitPolicy | FixedTargetPolicy | ChandelierPolicy, Field(discriminator="kind")
]
"""How a filled position's stop and target are allowed to move after entry.

Named for exits rather than for trailing because a fixed target is not a
trail: Phase 6 shipped a ``TrailPolicy`` with a single ``"none"`` member and
its docstring reserved this widening for the phase that could produce spec
section 9.2's A/B evidence. Keeping the target under the old name would have
misnamed one of the two things the A/B compares.
"""


class Strategy(Protocol):
    id: str
    version: str
    book: BookId
    horizon_seconds: int
    max_holding_seconds: int
    required_features: frozenset[FeatureBlock]

    def evaluate(self, snapshot: FeatureSnapshot) -> TradeProposal | None: ...

    def exit_policy(self) -> ExitPolicy: ...
