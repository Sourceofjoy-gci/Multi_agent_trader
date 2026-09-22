"""The strategy port a backtest replays against.

A ``Strategy`` reads only a ``FeatureSnapshot`` -- never the bar store, never
the clock -- so structurally nothing it does can see past ``as_of`` (I-17).
It is a ``Protocol``, not an ABC, matching the rest of this codebase's ports
(``BarReader``, ``MarginPort``, ``TerminalPort``): a strategy does not need to
inherit from anything to be one.
"""

from __future__ import annotations

from typing import Literal, Protocol

from trading_house.core.schemas import TradeProposal
from trading_house.core.values import BookId, CanonicalModel
from trading_house.research.backtest.snapshot import FeatureSnapshot


class TrailPolicy(CanonicalModel):
    """How a filled position's stop is allowed to move after entry.

    Deliberately near-empty: a single ``"none"`` member. Phase 7 widens this
    once a strategy can produce the A/B evidence spec section 9.2 demands
    before any trailing policy may be chosen -- a policy that cannot express
    trailing is the honest shape while that evidence does not exist.
    """

    kind: Literal["none"]


class Strategy(Protocol):
    id: str
    version: str
    book: BookId
    horizon_seconds: int
    max_holding_seconds: int

    def evaluate(self, snapshot: FeatureSnapshot) -> TradeProposal | None: ...

    def trail_policy(self) -> TrailPolicy | None: ...
