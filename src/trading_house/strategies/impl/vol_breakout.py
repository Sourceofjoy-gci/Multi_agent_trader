"""Bollinger-squeeze volatility breakout on EURUSD H1."""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from trading_house.core.exits import ExitPolicy, FixedTargetPolicy, NoExitPolicy
from trading_house.core.schemas import Side, TradeProposal
from trading_house.core.snapshot import FeatureBlock, FeatureSnapshot
from trading_house.core.values import BookId, PositiveQuantity
from trading_house.features.sessions import Session
from trading_house.strategies.spec import StrategySpec

VOL_BREAKOUT_ID: Final[str] = "vol_breakout_eurusd_h1"

VOL_BREAKOUT_SPEC: Final[StrategySpec] = StrategySpec(
    economic_rationale=(
        "Volatility clusters: when EURUSD's 20-bar dispersion contracts to a local minimum, "
        "the expansion that follows tends to resolve in one direction as positioning unwinds, "
        "and a break that agrees with the 200-bar trend has the larger pool of followers. "
        "A hypothesis to be falsified, not a claim of edge."
    ),
    universe=("fx.eurusd",),
    trading_horizon="H1 bars; holds of up to five calendar days, weekends permitted.",
    entry_rule=(
        "Bollinger 20 bars, 2 population sigma. A squeeze bar's bandwidth equals the minimum of "
        "the 125 bars ending at it (ties count). Long when a squeeze occurred within the last 10 "
        "bars, the close is above the upper band, the previous close was at or below its own "
        "upper band, and the close is above the 200-bar mean; short is the mirror. No signals on "
        "bars opening 21:00-24:00 UTC (a 20:00-bar signal still fills at the 21:00 open, at that "
        "bar's recorded spread)."
    ),
    exit_rule=(
        "Invalidation at the middle band; the risk-engine stop; a 432000-second time stop; "
        "and the selected exit arm."
    ),
    cost_model_description=(
        "Commission 0.0 per lot per side (official FBS publishes no commission); "
        "slippage 0.4 points per side (predeclared prior); "
        "swap long -7.7 points/day and short +2.0 points/day (terminal audit); "
        "triple swap Wednesday; stress multiplier 1; defective tolerance 0."
    ),
    capacity_model="Capacity is not modelled and is explicitly a non-promise.",
    invalidation=(
        "Not yet tested. Invalidated if the three-arm trial loses money after costs in every "
        "arm, or if the Phase 8 gates reject it; no tuning or promotion follows a rejection."
    ),
    regime_constraints=(
        "The risk spread, spread-to-stop and tick-staleness gates apply; no signals on bars "
        "opening 21:00-24:00 UTC (a 20:00-bar signal still fills at the 21:00 open, at that "
        "bar's recorded spread), which is fixed in UTC and carries the DST limitation."
    ),
    trail_decision="Pending the three-arm A/B: none, fixed_target 2.0R, chandelier 3.0 ATR.",
    trial_count=3,
    versioning=(
        "strategy=vol_breakout_eurusd_h1; version=1; "
        "constitution_sha256=a87e63fb8c46912b1bc21bae3e55535b88ae4abc613cb87988399c4c28e5d58b; "
        "contract_sha256=59121ba95a21afb81e48f4de9c9358705375c678954fa2249dbbd80b41b86f90"
    ),
)


class VolBreakout:
    """Trade the first close outside the Bollinger band after a squeeze, with
    the 200-bar trend.

    Every number is frozen by the Phase 9 spec, section 4. Changing one is a
    new version and a new trial.
    """

    def __init__(self, exit_policy: ExitPolicy | None = None) -> None:
        self._exit_policy = NoExitPolicy(kind="none") if exit_policy is None else exit_policy

    id: Final[str] = VOL_BREAKOUT_ID
    version: Final[str] = "1"
    book: Final[BookId] = "fx_swing"
    horizon_seconds: Final[int] = 432_000
    max_holding_seconds: Final[int] = 432_000
    required_features: Final[frozenset[FeatureBlock]] = frozenset({FeatureBlock.BOLLINGER})
    expected_return_bps: Final[float] = 20.0
    expected_return_stdev_bps: Final[float] = 10.0
    expected_cost_bps: Final[float] = 3.0
    expected_swap_cost_bps: Final[float] = 2.0
    win_probability: Final[float] = 0.45

    def evaluate(self, snapshot: FeatureSnapshot) -> TradeProposal | None:
        if snapshot.instrument_id != "fx.eurusd" or snapshot.timeframe.value != "H1":
            return None
        if snapshot.session is Session.OFF:
            return None
        block = snapshot.bollinger
        if block is None or block.bars_since_squeeze is None:
            return None

        close = snapshot.bar.close
        if (
            close > block.upper
            and block.previous_close <= block.previous_upper
            and close > block.sma_200
        ):
            side = Side.BUY
        elif (
            close < block.lower
            and block.previous_close >= block.previous_lower
            and close < block.sma_200
        ):
            side = Side.SELL
        else:
            return None

        return TradeProposal(
            source=self.id,
            event_time=snapshot.as_of,
            availability_time=snapshot.as_of,
            processing_time=snapshot.as_of,
            proposal_id=(
                f"{self.id}:{snapshot.instrument_id}:{snapshot.timeframe.value}:"
                f"{snapshot.as_of.isoformat()}"
            ),
            strategy_id=self.id,
            strategy_version=self.version,
            book=self.book,
            instrument_id=snapshot.instrument_id,
            side=side,
            horizon_seconds=self.horizon_seconds,
            entry_condition="bollinger-squeeze-breakout",
            entry_price_ref=close,
            invalidation_price=block.middle,
            max_holding_seconds=self.max_holding_seconds,
            expected_return_bps=self.expected_return_bps,
            expected_return_stdev_bps=self.expected_return_stdev_bps,
            expected_cost_bps=self.expected_cost_bps,
            expected_swap_cost_bps=self.expected_swap_cost_bps,
            win_probability=self.win_probability,
            calibration_id="vol-breakout-prior-v1",
            required_liquidity=PositiveQuantity(amount=Decimal(1), unit="lots"),
            regime_ref="squeeze-with-200-bar-trend",
            features_snapshot_id=f"snapshot:{snapshot.instrument_id}:{snapshot.as_of.isoformat()}",
            target_r_multiple=(
                self._exit_policy.r_multiple
                if isinstance(self._exit_policy, FixedTargetPolicy)
                else None
            ),
        )

    def exit_policy(self) -> ExitPolicy:
        return self._exit_policy
