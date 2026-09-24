"""The strategy port a backtest replays against.

A ``Strategy`` reads only a ``FeatureSnapshot`` -- never the bar store, never
the clock -- so structurally nothing it does can see past ``as_of`` (I-17).
It is a ``Protocol``, not an ABC, matching the rest of this codebase's ports
(``BarReader``, ``MarginPort``, ``TerminalPort``): a strategy does not need to
inherit from anything to be one.
"""

from __future__ import annotations

from typing import Annotated, Literal, Protocol

from pydantic import Field

from trading_house.core.schemas import TradeProposal
from trading_house.core.values import BookId, CanonicalModel, NonNegativeDecimal, PositiveDecimal
from trading_house.research.backtest.snapshot import FeatureSnapshot


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

    def evaluate(self, snapshot: FeatureSnapshot) -> TradeProposal | None: ...

    def exit_policy(self) -> ExitPolicy: ...
