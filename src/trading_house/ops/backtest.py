"""The backtester's composition shim, and Phase 6's one registrable strategy.

``cli.py`` is the composition root for every other command and would have been
the obvious home, but it is ~1000 lines against a 558-line next-largest module;
Phase 5 put ``guard``'s wiring here for exactly that reason and this is the same
shape.

It also carries one thing the guard's wiring did not: a clock that *must* be
shared. ``RiskEngine``'s tick-freshness gate compares its own clock to the
snapshot's ``tick_time``, and during a replay the only clock that can satisfy it
is the ``ReplayClock`` the ``Backtester`` advances to each bar's
``availability_time``. Hand the engine a ``SystemClock`` instead and every
stored bar is stale by years: the run finishes with zero trades and one
``tick_stale`` rejection per proposal, which is a plausible-looking result
rather than an error -- the worst failure shape available. ``build_backtester``
constructs the clock and hands it to both, so there is no call site at which the
two can drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from trading_house.constitution.models import Constitution
from trading_house.core.errors import ConfigurationError
from trading_house.core.instruments import InstrumentContract
from trading_house.core.schemas import Side, TradeProposal
from trading_house.core.values import BookId, PositiveQuantity
from trading_house.features.engine import BarReader, FeatureEngine
from trading_house.marketdata.models import Timeframe, duration
from trading_house.research.backtest.engine import Backtester, ReplayClock
from trading_house.research.backtest.snapshot import MIN_HORIZON_BARS, FeatureSnapshot
from trading_house.research.backtest.strategy import ExitPolicy, NoExitPolicy, Strategy
from trading_house.risk.engine import MARGIN_HEADROOM_MULTIPLE, RiskEngine

TOY_STRATEGY_ID: Final[str] = "toy"
"""Phase 6's entire strategy registry. Phase 7 replaces this with a real one."""

_REQUIRED_MARGIN: Final[Decimal] = Decimal(1)
_TOY_STOP_DISTANCE: Final[Decimal] = Decimal("0.00100")


@dataclass(frozen=True, slots=True)
class NeverBindingMargin:
    """The ``MarginPort`` a backtest has: none.

    Nothing in this repo can supply a margin requirement --
    ``InstrumentContract`` carries no leverage or margin field, and a replay has
    no account to read free margin from -- so section 8.1's free-margin headroom
    gate is disabled here rather than fed an invented number. D-5 says a cost
    this simulator cannot know is declared rather than guessed, and there is
    nothing to declare it through: a ``--free-margin`` would be a number the
    operator made up, dressed as a broker fact.

    ``1`` and ``MARGIN_HEADROOM_MULTIPLE`` are deliberately not money. They are
    the smallest pair that clears the gate, sized off the engine's own constant
    so they still clear it if that constant moves, and small enough that nobody
    reads them as a modelled account. The README states plainly that a run
    assumes margin was always available.
    """

    def free_margin(self) -> Decimal:
        return _REQUIRED_MARGIN * MARGIN_HEADROOM_MULTIPLE

    def required_margin(
        self, *, instrument_id: str, side: Side, quantity: Decimal, price: Decimal
    ) -> Decimal:
        return _REQUIRED_MARGIN


@dataclass(slots=True)
class ToyStrategy:
    """Buys every ``every_n``-th snapshot it is handed. Not a strategy.

    It has no edge and is not meant to acquire one: it exists so ``backtest
    run`` can be exercised end to end, and so the wiring above can be proved by
    something that actually trades. Whatever P&L it reports is the shape of the
    data it was pointed at. Phase 7 brings the real registry and the first
    strategy worth running; until then, nothing here should be mistaken for one.

    It counts snapshots rather than bars because the feature windows are cold at
    the start of any range, so the first bars produce no snapshot and the
    strategy is never asked about them.

    **An instance is single-use.** ``_seen`` is never reset -- nothing in the
    ``Strategy`` port gives a run a place to reset it from -- so replaying the
    same instance twice offsets the second run's entries and changes its
    digest. ``backtest run`` builds a fresh one per invocation, which is the
    only path this phase ships; a caller reusing one in-process must build a
    new one per run.
    """

    every_n: int
    horizon_seconds: int
    max_holding_seconds: int
    id: str = TOY_STRATEGY_ID
    version: str = "1"
    # A book the signed constitution actually declares. An unknown one would
    # reject every proposal with UNKNOWN_BOOK and report an empty run rather
    # than an error -- the same silent shape the shared clock exists to avoid.
    book: BookId = "fx_swing"
    _seen: int = field(default=0, init=False)

    def evaluate(self, snapshot: FeatureSnapshot) -> TradeProposal | None:
        seen = self._seen
        self._seen += 1
        if seen % self.every_n != 0:
            return None
        return self._proposal(snapshot)

    def exit_policy(self) -> ExitPolicy:
        return NoExitPolicy(kind="none")

    def _proposal(self, snapshot: FeatureSnapshot) -> TradeProposal:
        as_of = snapshot.as_of
        entry = snapshot.bar.close
        return TradeProposal(
            source="toy-strategy",
            event_time=as_of,
            # Never later than the snapshot that produced it: a proposal
            # claiming availability past its own ``as_of`` refuses the run.
            availability_time=as_of,
            processing_time=as_of,
            proposal_id=f"{self.id}-{as_of.isoformat()}",
            strategy_id=self.id,
            strategy_version=self.version,
            book=self.book,
            instrument_id=snapshot.instrument_id,
            side=Side.BUY,
            horizon_seconds=self.horizon_seconds,
            entry_condition="toy",
            entry_price_ref=entry,
            invalidation_price=entry - _TOY_STOP_DISTANCE,
            max_holding_seconds=self.max_holding_seconds,
            expected_return_bps=5.0,
            expected_return_stdev_bps=2.0,
            expected_cost_bps=1.0,
            win_probability=0.55,
            calibration_id="toy",
            required_liquidity=PositiveQuantity(amount=Decimal(1), unit="lots"),
            regime_ref="toy",
            features_snapshot_id=f"snap-{as_of.isoformat()}",
        )


def build_strategy(strategy_id: str, *, every_n: int, timeframe: Timeframe) -> Strategy:
    """Phase 6's registry lookup, which knows exactly one id.

    The horizon and the holding limit are derived from the timeframe rather than
    stated: ``MIN_HORIZON_BARS`` bars is the shortest horizon D-1 will simulate
    at all, so the toy is runnable on every timeframe instead of silently
    refusing on the slow ones. A toy with a fixed number of seconds would be
    refused on D1 and hold for two hundred bars on M1.
    """

    if strategy_id != TOY_STRATEGY_ID:
        raise ConfigurationError()
    horizon_seconds = MIN_HORIZON_BARS * int(duration(timeframe).total_seconds())
    return ToyStrategy(
        every_n=every_n,
        horizon_seconds=horizon_seconds,
        max_holding_seconds=horizon_seconds,
    )


def build_backtester(
    *, bars: BarReader, contract: InstrumentContract, constitution: Constitution
) -> Backtester:
    """One ``ReplayClock``, handed to both the engine and the simulator.

    The whole reason this function exists rather than six lines at a call site:
    the risk engine and the backtester must read the same clock instance, and
    nothing in either type enforces it.
    """

    clock = ReplayClock()
    return Backtester(
        bars=bars,
        features=FeatureEngine(bars),
        risk=RiskEngine(constitution, clock),
        margin=NeverBindingMargin(),
        contract=contract,
        clock=clock,
    )
