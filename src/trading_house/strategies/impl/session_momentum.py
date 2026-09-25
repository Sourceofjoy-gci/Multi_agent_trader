"""Session-conditioned directional momentum on EURUSD."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Final

from trading_house.core.exits import ExitPolicy, FixedTargetPolicy, NoExitPolicy
from trading_house.core.schemas import Side, TradeProposal
from trading_house.core.snapshot import FeatureSnapshot
from trading_house.core.values import BookId, PositiveQuantity
from trading_house.features.sessions import Session
from trading_house.strategies.spec import StrategySpec

SESSION_MOMENTUM_ID: Final[str] = "session_momentum_eurusd"
_INVALIDATION_DISTANCE: Final[Decimal] = Decimal("0.00100")

SESSION_MOMENTUM_SPEC: Final[StrategySpec] = StrategySpec(
    economic_rationale=(
        "Information arriving outside European hours is priced into EURUSD in thin conditions; "
        "the sign of that move can continue to be absorbed when London liquidity returns."
    ),
    universe=("fx.eurusd",),
    trading_horizon="M15 from the London open through 16:00 UTC.",
    entry_rule=(
        "On the first closed M15 bar whose session is LONDON and bars_since_session_open is "
        "zero, trade the sign of prior_session_return when it is neither None nor zero."
    ),
    exit_rule="Use the risk-engine stop, the 16:00 UTC time stop, and the selected exit arm.",
    cost_model_description=(
        "Declare spread, commission, slippage, and swap; no cost is left at an implicit default."
    ),
    capacity_model="Capacity is not modelled and is explicitly a non-promise.",
    invalidation=(
        "Rolling out-of-sample monitoring invalidates the thesis when its edge disappears."
    ),
    regime_constraints=(
        "The risk spread, spread-to-stop, and tick-staleness gates apply; UTC session windows are "
        "fixed and therefore carry the stated DST limitation."
    ),
    trail_decision="The A/B has not yet run; the trail decision is pending.",
    trial_count=3,
    versioning=(
        "Record the strategy id and version, constitution hash, contract digest, and result digest."
    ),
)


class SessionMomentum:
    """The first real strategy: carry the prior session's sign into London.

    The declared economics are priors awaiting the first run; measured values replace them.
    """

    def __init__(self, exit_policy: ExitPolicy | None = None) -> None:
        self._exit_policy = NoExitPolicy(kind="none") if exit_policy is None else exit_policy

    id: Final[str] = SESSION_MOMENTUM_ID
    version: Final[str] = "1"
    book: Final[BookId] = "fx_swing"
    horizon_seconds: Final[int] = 32_400
    max_holding_seconds: Final[int] = 31_500
    expected_return_bps: Final[float] = 5.0
    expected_return_stdev_bps: Final[float] = 2.0
    expected_cost_bps: Final[float] = 1.0
    win_probability: Final[float] = 0.55
    expected_swap_cost_bps: Final[float] = 0.0

    def evaluate(self, snapshot: FeatureSnapshot) -> TradeProposal | None:
        if (
            snapshot.instrument_id != "fx.eurusd"
            or snapshot.timeframe.value != "M15"
            or snapshot.session is not Session.LONDON
        ):
            return None
        is_session_open = snapshot.bars_since_session_open == 0
        if not is_session_open:
            return None

        prior_return = snapshot.prior_session_return
        if prior_return is None or prior_return == 0:
            return None

        side = Side.BUY if prior_return > 0 else Side.SELL
        entry_price = snapshot.bar.close
        invalidation_price = (
            entry_price - _INVALIDATION_DISTANCE
            if side is Side.BUY
            else entry_price + _INVALIDATION_DISTANCE
        )
        return TradeProposal(
            source=self.id,
            event_time=snapshot.as_of,
            availability_time=snapshot.as_of,
            processing_time=snapshot.as_of,
            proposal_id=self._proposal_id(snapshot),
            strategy_id=self.id,
            strategy_version=self.version,
            book=self.book,
            instrument_id=snapshot.instrument_id,
            side=side,
            horizon_seconds=self.horizon_seconds,
            entry_condition="london-open-prior-session-sign",
            entry_price_ref=entry_price,
            invalidation_price=invalidation_price,
            max_holding_seconds=self._max_holding_seconds(snapshot),
            expected_return_bps=self.expected_return_bps,
            expected_return_stdev_bps=self.expected_return_stdev_bps,
            expected_cost_bps=self.expected_cost_bps,
            expected_swap_cost_bps=self.expected_swap_cost_bps,
            win_probability=self.win_probability,
            calibration_id="session-momentum-prior-v1",
            required_liquidity=PositiveQuantity(amount=Decimal(1), unit="lots"),
            regime_ref="fixed-utc-session",
            features_snapshot_id=self._snapshot_id(snapshot),
            target_r_multiple=(
                self._exit_policy.r_multiple
                if isinstance(self._exit_policy, FixedTargetPolicy)
                else None
            ),
        )

    def exit_policy(self) -> ExitPolicy:
        return self._exit_policy

    @staticmethod
    def _max_holding_seconds(snapshot: FeatureSnapshot) -> int:
        fill_at: datetime = snapshot.bar.event_time + (snapshot.as_of - snapshot.bar.event_time)
        session_close = fill_at.replace(hour=16, minute=0, second=0, microsecond=0)
        return int((session_close - fill_at).total_seconds())

    def _proposal_id(self, snapshot: FeatureSnapshot) -> str:
        return (
            f"{self.id}:{snapshot.instrument_id}:{snapshot.timeframe.value}:"
            f"{snapshot.as_of.isoformat()}"
        )

    def _snapshot_id(self, snapshot: FeatureSnapshot) -> str:
        return f"snapshot:{snapshot.instrument_id}:{snapshot.as_of.isoformat()}"
