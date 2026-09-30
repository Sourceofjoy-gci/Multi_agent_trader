"""The replay loop: the one stateful piece of the backtester.

Tasks 1-4 built the snapshot, the cost model, the fill model and the result;
this composes them and owns nothing but the order in which they are called.
That order is the whole design, because a simulator flatters a strategy by
doing the right things in the wrong sequence. Per bar:

1. A queued entry fills at THIS bar's open. Never the close that generated the
   signal -- section 11.1's named violation.
2. Any open position is resolved against this bar: ``resolve_exit`` first (the
   stop before the target, D-2), then the engine's own time stop.
2a. The bar is marked to market: the equity observation for this bar, with an
   open position valued at the close and nothing carried unrealized once the
   slot is empty. Phase 8B1's series is a run's shape, and a point that missed a
   bar would describe a period the result never replayed.
3. ``as_of`` becomes the bar's ``availability_time``, and the shared
   ``ReplayClock`` is advanced to it.
4. A ``FeatureSnapshot`` is built at that ``as_of``, or the bar is skipped
   because the feature windows have no history yet.
4a. Under a chandelier policy, an open position's stop is trailed from the
   snapshot's ATR and this bar's extreme. Deliberately here and not at step 2:
   a stop set from a bar's own high must not be tested against that same bar's
   low, because the bar cannot say which came first. The level set here governs
   the NEXT bar, which is also what the live guard does -- it reacts to a
   closed bar. A bar with no snapshot trails nothing, the same rule step 4
   already applies to entries.
5. ``strategy.evaluate`` sees that snapshot and nothing else.
6. A proposal claiming availability later than the snapshot refuses the run.
7. With a position open or an entry queued, the proposal is dropped: one
   position at a time (D-7).
8. ``risk.evaluate_for_execution`` decides. A rejection is recorded with its
   reasons (D-8); an executable decision is read for its ``approved_quantity``
   and ``stop_loss_price`` (D-3) and queued to fill on the next bar.

Steps 2 and 7 are the pair that matters: the exit frees the slot before the
slot is read, so a strategy that proposes on every bar re-enters on the bar
after it exits rather than one bar later.

The time stop lives here rather than in ``fills.resolve_exit``, which takes no
time input and never returns ``ExitKind.TIME``: only the loop holds the
position's ``entry_at`` and the proposal's ``max_holding_seconds``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import assert_never

from trading_house.core.clock import ensure_utc
from trading_house.core.errors import (
    EquityEvidenceError,
    InsufficientHistoryError,
    TradingHouseError,
)
from trading_house.core.instruments import InstrumentContract
from trading_house.core.schemas import RejectedRiskDecision, Side
from trading_house.features.engine import BarReader, FeatureEngine, MaterializedBarReader
from trading_house.features.sessions import session_of
from trading_house.marketdata.models import Bar, BarQuality, Coverage, Timeframe, duration
from trading_house.research.backtest.costs import CostModel, commission_cost, swap_cost
from trading_house.research.backtest.costs_attribution import CostAttribution, TradeCostAttribution
from trading_house.research.backtest.fills import Exit, ExitKind, Fill, entry_fill, resolve_exit
from trading_house.research.backtest.mark import (
    MAX_EQUITY_OBSERVATIONS,
    BacktestOutcome,
    EquityObservation,
    EquitySeries,
)
from trading_house.research.backtest.result import BacktestResult, RefusalKind, SimulatedTrade
from trading_house.research.backtest.sizing import SizingMode
from trading_house.research.backtest.snapshot import FeatureSnapshot, horizon_is_simulatable
from trading_house.research.backtest.strategy import (
    ChandelierPolicy,
    ExitPolicy,
    FixedTargetPolicy,
    NoExitPolicy,
    Strategy,
)
from trading_house.risk.engine import MarginPort, RiskEngine
from trading_house.risk.sizing import quantise_down, quantise_up

_BEFORE_ANY_BAR: datetime = datetime.min.replace(tzinfo=UTC)
"""``ReplayClock``'s starting instant: a time no market ever traded at."""


class BacktestRefused(TradingHouseError):
    """A run this simulator will not perform, named by its ``RefusalKind``.

    Carries the kind and nothing else. No free text, so no credential, DSN,
    account number or broker message can reach a log or a result payload
    through a refusal.
    """

    public_message = "backtest refused"

    def __init__(self, kind: RefusalKind) -> None:
        super().__init__()
        self.kind = kind


@dataclass(slots=True)
class ReplayClock:
    """Simulated time: the replay cursor, not the wall clock.

    The risk engine's tick-freshness gate compares its own clock to the
    snapshot's ``tick_time`` against ``max_tick_age_seconds``, so a backtest
    must hand this same instance to the ``RiskEngine`` it injects here. Given a
    wall clock instead, every stored bar is stale by years and the gate rejects
    every proposal in the run.
    """

    # Overwritten by the first bar of every run, so a caller has nothing useful
    # to say here. The default makes a clock read before the run a visibly
    # wrong time rather than a plausible one.
    instant: datetime = _BEFORE_ANY_BAR

    def __post_init__(self) -> None:
        # The same guard ``FixedClock`` applies. A naive datetime reaching the
        # risk engine's tick-freshness gate raises deep inside a comparison
        # rather than here, where the value entered.
        self.instant = ensure_utc(self.instant)

    def now(self) -> datetime:
        return self.instant


@dataclass(frozen=True, slots=True)
class BacktestRequest:
    """One run. ``start`` and ``end`` are an inclusive range of bar open times.

    ``firm_equity`` is the initial capital. Under the default
    ``CONSTANT_NOTIONAL`` sizing (D-4) every decision is sized from it, so a
    strategy's measured edge cannot be an artefact of position sizes growing
    with its own luck; ``COMPOUNDING`` (Phase 8B3) sizes from it plus realized PnL.

    A dataclass rather than a ``CanonicalModel`` because ``strategy`` is a bare
    ``Protocol``: pydantic cannot build a schema for one, and widening the base
    config with ``arbitrary_types_allowed`` to admit it would weaken every
    canonical model in the package. So the three checks the model's own
    annotations would have made -- ``PositiveDecimal``, UTC-awareness on both
    stamps, and an ordered range -- are made here by hand. ``end < start`` is
    refused by ``run`` before any source access, beside the horizon refusal.
    """

    strategy: Strategy
    instrument_id: str
    timeframe: Timeframe
    start: datetime
    end: datetime
    firm_equity: Decimal
    cost_model: CostModel
    atr_period: int
    spread_window: int
    defective_bar_tolerance: Decimal = Decimal(0)
    sizing: SizingMode = SizingMode.CONSTANT_NOTIONAL

    def __post_init__(self) -> None:
        # The boundary carrying the money. A float here would survive every
        # later Decimal arithmetic as a silently inexact number, and a
        # non-positive equity sizes nothing while looking like a strategy that
        # proposed nothing.
        if not isinstance(self.firm_equity, Decimal):
            raise ValueError("firm_equity must be a Decimal")
        if self.firm_equity <= 0:
            raise ValueError("firm_equity must be positive")
        if not isinstance(self.defective_bar_tolerance, Decimal):
            raise ValueError("defective_bar_tolerance must be a Decimal")
        if (
            not self.defective_bar_tolerance.is_finite()
            or self.defective_bar_tolerance < 0
            or self.defective_bar_tolerance > 1
        ):
            raise ValueError("defective_bar_tolerance must be between 0 and 1")
        # The same guard ``ReplayClock`` applies twenty lines up, for the same
        # reason: a naive ``start`` reached ``_refuse_outside_coverage``'s
        # comparison against an aware ``Coverage`` stamp and raised
        # ``TypeError`` from inside the run -- past every handler, so an
        # operator got "unexpected failure" and a correlation id for a missing
        # timezone. ``ensure_utc`` raises the typed ``TimestampError`` instead,
        # here, where the value entered. Normalised rather than only checked,
        # as ``FixedClock`` does, so two requests naming the same instant in
        # different offsets produce the same ``run_id`` and the same digest.
        object.__setattr__(self, "start", ensure_utc(self.start))
        object.__setattr__(self, "end", ensure_utc(self.end))


@dataclass(frozen=True, slots=True)
class _Signal:
    """An approved decision waiting for a bar to open.

    Holds the quantity and stop read off the risk decision, never recomputed:
    sizing has exactly one home (D-3).
    """

    proposal_id: str
    side: Side
    lots: Decimal
    stop: Decimal
    target: Decimal | None
    # The proposal's own holding limit, not the strategy's. They are the same
    # for a strategy that states one number, and they are not for one that
    # sizes the horizon per proposal -- in which case the time stop must honour
    # what was proposed.
    max_holding_seconds: int


@dataclass(slots=True)
class _Position:
    """An open position, and the one mutable thing in this module besides the
    clock: ``stop`` starts at the risk engine's level and is the only field a
    trail is allowed to move. ``signal.stop`` keeps the original for the record,
    so a trailed run can still say where the validated stop was."""

    signal: _Signal
    entry: Fill
    deadline: datetime
    stop: Decimal


def _exit_arms(policy: ExitPolicy) -> tuple[ChandelierPolicy | None, FixedTargetPolicy | None]:
    """The declared arm, split into the two things the loop asks about.

    A ``match`` with ``assert_never`` rather than two ``isinstance`` checks at
    the call sites. Positive ``isinstance`` tests are silently inert for a
    variant nobody has taught them about, so a fourth ``ExitPolicy`` member
    would run as a baseline and report a number under its own name; this way
    mypy refuses to compile until the new arm is handled.
    """

    match policy:
        case NoExitPolicy():
            return None, None
        case FixedTargetPolicy():
            return None, policy
        case ChandelierPolicy():
            return policy, None
        case _:  # pragma: no cover - mypy proves this unreachable
            assert_never(policy)


def trail_candidate(
    *,
    side: Side,
    current_stop: Decimal,
    bar: Bar,
    atr: Decimal,
    contract: InstrumentContract,
    policy: ChandelierPolicy,
) -> Decimal | None:
    """Where a Chandelier trail would move this position's stop, or ``None``.

    The pure analogue of spec section 9.3's ``maybe_trail``, minus everything
    that needs a broker: no ticket, no retcodes, no rate limit. What survives
    is the arithmetic and, more importantly, its ORDER.

    1. **Clamp** the raw level to the broker's minimum distance from the
       current price -- ``max(min_stop_distance, freeze_distance)``, D-6's
       pair, because a stop inside the freeze band is legal to place and
       illegal to MODIFY, which is the one thing a trail does on every bar.
    2. **Hysteresis.** A candidate nearer the current stop than
       ``min_step_points`` is ignored.
    3. **Monotonic (I-8).** The stop never moves away from profit.

    Clamping FIRST is the part worth stating. The clamp pushes the candidate
    away from price; run after the monotonic check, it could take a level that
    had just been proved an improvement and push it back past the current stop,
    producing exactly the backwards move step 3 exists to forbid. Steps 2 and 3
    are both filters over the same clamped number and so commute with each
    other; step 1 commutes with neither.

    The reference price is the bar's close, which is this simulator's "current
    price": a bar carries no tick, and the close is the last price the bar can
    honestly be said to have traded at.

    Returns the trailed stop, already on the contract's tick grid -- rounded
    AWAY from price on both sides, the same direction ``stop_price`` rounds,
    so the rounding can only widen the distance the clamp just guaranteed and
    never narrow it back inside the floor.

    Refuses, rather than returning ``None``, when the candidate is not a
    positive price. ``None`` means "this bar moves nothing", and an arm whose
    every candidate is negative -- ``atr_multiple`` has no upper bound and
    cannot have a useful one, since absurdity is a function of the multiple AND
    the ATR -- would then report a number indistinguishable from the baseline
    under the chandelier's name. ``ChandelierPolicy`` already refuses a zero
    multiple loudly; this is the same refusal at the other end.
    """

    floor = max(contract.min_stop_distance, contract.freeze_distance)
    if side is Side.BUY:
        raw = bar.high - policy.atr_multiple * atr
        clamped = min(raw, bar.close - floor)
        candidate = quantise_down(max(clamped, Decimal(0)), contract.price_increment)
        if candidate <= 0:
            raise BacktestRefused(RefusalKind.EXIT_POLICY)
    else:
        raw = bar.low + policy.atr_multiple * atr
        candidate = quantise_up(max(raw, bar.close + floor), contract.price_increment)

    if abs(candidate - current_stop) < policy.min_step_points * contract.point_size:
        return None
    improves = candidate > current_stop if side is Side.BUY else candidate < current_stop
    return candidate if improves else None


class Backtester:
    """Replays a strategy over stored bars. One position at a time (D-7)."""

    def __init__(
        self,
        *,
        bars: BarReader,
        risk: RiskEngine,
        margin: MarginPort,
        contract: InstrumentContract,
        clock: ReplayClock,
        constitution_sha256: str,
    ) -> None:
        self._bars = bars
        self._risk = risk
        self._margin = margin
        self._contract = contract
        self._clock = clock
        self._constitution_sha256 = constitution_sha256
        self._contract_sha256 = contract.digest()

    def run(self, request: BacktestRequest) -> BacktestOutcome:
        strategy = request.strategy
        if not horizon_is_simulatable(
            horizon_seconds=strategy.horizon_seconds, timeframe=request.timeframe
        ):
            raise BacktestRefused(RefusalKind.HORIZON)
        if request.end < request.start:
            raise BacktestRefused(RefusalKind.COVERAGE)
        coverage = self._bars.coverage(request.instrument_id, request.timeframe)
        self._refuse_outside_coverage(request, coverage)
        reader = MaterializedBarReader(self._bars, coverage)
        features = FeatureEngine(reader)
        replay_bars = self._replay_bars(request, reader)
        tolerance_fraction = Fraction(request.defective_bar_tolerance)
        defective_bars = sum(1 for bar in replay_bars if bar.quality is not BarQuality.OK)
        if defective_bars:
            defective_fraction = Fraction(defective_bars, len(replay_bars))
            if defective_fraction > tolerance_fraction:
                raise BacktestRefused(RefusalKind.DEFECTIVE_BAR)

        # Read once. A strategy's exit policy is a declaration made before the
        # run (design section 9.2: no sweeps), not a per-bar decision, and
        # asking it every bar would let one drift mid-run and make the result
        # describe no single arm.
        policy = strategy.exit_policy()
        trail, target_arm = _exit_arms(policy)

        trades: list[SimulatedTrade] = []
        attributions: list[TradeCostAttribution] = []
        rejections: list[tuple[str, ...]] = []
        position: _Position | None = None
        queued: _Signal | None = None
        bars_seen = 0
        snapshots_skipped = 0
        realized = Decimal(0)
        observations: list[EquityObservation] = []

        for bar in replay_bars:
            if bar.quality is not BarQuality.OK:
                continue
            bars_seen += 1

            if queued is not None:
                position = self._open(queued, bar, request)
                queued = None
            if position is not None:
                closed = self._close_if_done(position, bar, request)
                if closed is not None:
                    trade, split = closed
                    # Appended in one place, so the two tuples cannot fall out
                    # of step; ``BacktestOutcome`` would catch it either way.
                    trades.append(trade)
                    attributions.append(split)
                    realized += trade.net_pnl
                    position = None

            # The mark is the state at this bar's close, so it is taken after
            # the open and the close have been applied and before the snapshot
            # that may skip the bar entirely. A bar that reached here was
            # processed -- it is already counted in ``bars_seen`` -- so it gets
            # a mark even when the strategy proposed nothing and the risk engine
            # rejected the idea. ``_gross_pnl`` is the same conversion the
            # eventual exit uses, with the bar's mid close standing in for an
            # exit price: a mark is a valuation, and pricing it through the
            # fill model would invent an exit that did not happen. That leaves
            # out what ``_trade`` also leaves to its own lines -- the commission
            # the exit books, and the swap -- so a mark taken immediately before
            # an exit is not that exit's ``net_pnl``, and equity steps down by
            # the charge at every close. A reader comparing a mark against a
            # trade's ``net_pnl`` is looking at a valuation path and an
            # accounting: related, not the same claim. Design section 9 carries
            # it as a documented limitation, and 8B2 owns the attribution.
            if position is None:
                unrealized = Decimal(0)
            else:
                unrealized = self._gross_pnl(
                    side=position.signal.side,
                    entry_price=position.entry.price,
                    exit_price=bar.close,
                    lots=position.signal.lots,
                )
            if len(observations) >= MAX_EQUITY_OBSERVATIONS:
                # The count that broke the ceiling and the ceiling itself ride
                # the private cause, never the public message, which stays as
                # uninformative as every other code in this repo's errors.
                raise EquityEvidenceError() from ValueError(
                    f"equity observations exceed the ceiling: {len(observations) + 1}"
                    f" > {MAX_EQUITY_OBSERVATIONS}"
                )
            observations.append(
                EquityObservation(
                    marked_at=bar.availability_time,
                    equity=request.firm_equity + realized + unrealized,
                    cumulative_realized_pnl=realized,
                    unrealized_pnl=unrealized,
                    open_positions=0 if position is None else 1,
                )
            )

            as_of = bar.availability_time
            self._clock.instant = as_of
            snapshot = self._snapshot(request, bar, as_of, features)
            if snapshot is None:
                snapshots_skipped += 1
                continue
            if trail is not None and position is not None:
                raised = trail_candidate(
                    side=position.signal.side,
                    current_stop=position.stop,
                    bar=bar,
                    atr=snapshot.atr,
                    contract=self._contract,
                    policy=trail,
                )
                if raised is not None:
                    position.stop = raised
            proposal = strategy.evaluate(snapshot)
            if proposal is None:
                continue
            if proposal.availability_time > snapshot.as_of:
                raise BacktestRefused(RefusalKind.LOOKAHEAD)
            if target_arm is not None and proposal.target_r_multiple != target_arm.r_multiple:
                # Checking agreement, not computing a price: the engine still
                # prices the only take-profit in the run, so D-3 holds. A
                # declared arm and a proposal configured from different sources
                # is a setup error, not a market condition -- and it would be
                # recorded as a result under the wrong policy's name, which is
                # worse than no result. Includes the case where the arm names a
                # multiple and the proposal carries none, which would otherwise
                # run the fixed-target arm as a silent baseline.
                raise BacktestRefused(RefusalKind.EXIT_POLICY)
            if position is not None or queued is not None:
                continue

            # Decisions are taken only when flat, so unrealized is zero here and
            # the compounding equity is initial capital plus realized (C-2).
            sizing_equity = (
                request.firm_equity + realized
                if request.sizing is SizingMode.COMPOUNDING
                else request.firm_equity
            )
            if sizing_equity <= 0:
                # A ruined account is a result, not a crash (C-3): the risk
                # engine raises on a non-positive equity, so it is not asked.
                rejections.append(("equity_exhausted",))
                continue
            decision = self._risk.evaluate_for_execution(
                proposal,
                margin=self._margin,
                contract=self._contract,
                firm_equity=sizing_equity,
                atr=snapshot.atr,
                median_spread_points=snapshot.median_spread_points,
                tick_spread_points=snapshot.tick_spread_points,
                tick_time=snapshot.tick_time,
            )
            if isinstance(decision, RejectedRiskDecision):
                rejections.append(decision.reasons)
                continue
            # D-3: read, never recompute. The risk engine already called
            # compute_stop_distance, stop_price and compute_volume; calling any
            # of them here would size the position a second time.
            queued = _Signal(
                proposal_id=proposal.proposal_id,
                side=proposal.side,
                lots=decision.approved_quantity.amount,
                stop=decision.stop_loss_price,
                # Gated on the declared arm rather than taken whenever the
                # risk engine produced one. The engine prices a target from
                # the proposal's own ``target_r_multiple``, so a strategy that
                # stamps one on every proposal -- the natural way to write one
                # strategy and run it three times -- would otherwise carry a
                # live target into the chandelier and baseline arms too, and
                # the A/B would compare three things that all have targets.
                # The policy is what names the arm; this reads it.
                target=decision.take_profit_price if target_arm is not None else None,
                max_holding_seconds=proposal.max_holding_seconds,
            )

        # A position still open when the bars run out is discarded, and a
        # queued signal never fills. Closing it at the last bar would report a
        # trade the requested period cannot justify -- the exit price would
        # come from the range's edge rather than from anything the position's
        # own rules asked for. An unfinished trade is not a result.
        result = self._result(
            request,
            trades=tuple(trades),
            rejections=tuple(rejections),
            bars_seen=bars_seen,
            defective_bars=defective_bars,
            tolerance_fraction=tolerance_fraction,
            exit_policy=policy,
            snapshots_skipped=snapshots_skipped,
        )
        return BacktestOutcome(
            result=result,
            equity=EquitySeries(firm_equity=request.firm_equity, observations=tuple(observations)),
            attribution=CostAttribution(trades=tuple(attributions)),
            sizing=request.sizing,
        )

    def _refuse_outside_coverage(self, request: BacktestRequest, coverage: Coverage) -> None:
        """Coverage is a boundary, not a list of holes.

        ``Coverage`` exposes ``earliest_event_time``, ``latest_event_time`` and
        bar counts -- never gaps -- so this refuses a requested range that
        reaches outside the window the store holds, and nothing more. Interior
        gaps are deliberately not refused: FX closes every weekend, so a gap
        rule would refuse every run spanning a Saturday and the feature would
        be useless. This is what section 8's "coverage gap" means in the only
        terms the store can actually answer.

        ``run`` refuses an unsimulatable horizon and a backwards range before
        any source access, then reads ``Coverage`` once and calls this before
        constructing the materialized reader. An out-of-range request therefore
        never triggers a bulk source bars read.

        ``end < start`` reaches no bar in any store, and ``start == end`` is a
        legal one-bar run -- both range endpoints are inclusive.
        """

        if coverage.earliest_event_time is None or coverage.latest_event_time is None:
            raise BacktestRefused(RefusalKind.COVERAGE)
        if request.start < coverage.earliest_event_time:
            raise BacktestRefused(RefusalKind.COVERAGE)
        if request.end > coverage.latest_event_time:
            raise BacktestRefused(RefusalKind.COVERAGE)

    def _replay_bars(self, request: BacktestRequest, bars: BarReader) -> tuple[Bar, ...]:
        """Every bar in the requested range, defective ones included.

        Included so a defective bar can be refused as it is read rather than
        silently dropped, which would leave the run quietly shorter than the
        period it claims.

        ``request.start``/``end`` are inclusive bar open times while the store's
        range is half-open, so the end moves one bar's duration forward here.
        The same instant serves as ``as_of``: holding the whole range is not a
        leak, because the only thing a strategy ever sees is one
        ``FeatureSnapshot`` carrying one closed bar.
        """

        horizon = request.end + duration(request.timeframe)
        return bars.bars(
            request.instrument_id,
            request.timeframe,
            start=request.start,
            end=horizon,
            as_of=horizon,
            include_defective=True,
        )

    def _snapshot(
        self,
        request: BacktestRequest,
        bar: Bar,
        as_of: datetime,
        features: FeatureEngine,
    ) -> FeatureSnapshot | None:
        """The point-in-time read, or ``None`` while the feature windows are
        still warming up.

        ``FeatureEngine`` refuses rather than guessing when it holds less
        history than an indicator's fixed window needs, and at the start of a
        range that is the normal case, not a fault. No features at an instant
        means no decision at that instant. ``session_open_price`` and
        ``bars_since_session_open`` extend the same rule: both raise
        ``InsufficientHistoryError`` -- never a bare ``None`` or a guessed
        answer -- for any of three reasons, none of which is "the very start
        of every session" (a session after the first in the store's history
        already has bars behind it): the current window has no closed bars
        yet (the store's whole history, or an interior gap, not a routine
        per-session event), the store's coverage only starts partway through
        it, or the reference bar falls in ``Session.OFF``, which has no
        window at all. This loop treats all of these the same way as the ATR
        and spread windows warming up: skip the bar, try again next bar. The
        alternative of refusing the whole run would make an otherwise
        healthy multi-year replay fail the first time it reaches OFF
        hours -- which is daily. The alternative of inventing a placeholder
        session-open price would be the guess the store's own author refused
        to make one call down. ``prior_session_return`` never raises -- its
        own ``None`` already distinguishes an outage from a flat night, so
        it is read unconditionally.
        """

        try:
            atr = features.atr(
                request.instrument_id,
                request.timeframe,
                period=request.atr_period,
                as_of=as_of,
            )
            median_spread_points = features.median_spread_points(
                request.instrument_id,
                request.timeframe,
                window=request.spread_window,
                as_of=as_of,
            )
            session_open_price = features.session_open_price(
                request.instrument_id, request.timeframe, as_of=as_of
            )
            bars_since_session_open = features.bars_since_session_open(
                request.instrument_id, request.timeframe, as_of=as_of
            )
        except InsufficientHistoryError:
            return None
        prior_session_return = features.prior_session_return(
            request.instrument_id, request.timeframe, as_of=as_of
        )
        return FeatureSnapshot(
            as_of=as_of,
            instrument_id=request.instrument_id,
            timeframe=request.timeframe,
            bar=bar,
            atr=atr,
            median_spread_points=median_spread_points,
            tick_spread_points=Decimal(bar.spread),
            tick_time=as_of,
            session=session_of(bar.event_time),
            prior_session_return=prior_session_return,
            session_open_price=session_open_price,
            bars_since_session_open=bars_since_session_open,
        )

    def _open(self, signal: _Signal, bar: Bar, request: BacktestRequest) -> _Position:
        fill = entry_fill(
            bar=bar, side=signal.side, contract=self._contract, model=request.cost_model
        )
        return _Position(
            signal=signal,
            entry=fill,
            deadline=fill.at + timedelta(seconds=signal.max_holding_seconds),
            stop=signal.stop,
        )

    def _close_if_done(
        self, position: _Position, bar: Bar, request: BacktestRequest
    ) -> tuple[SimulatedTrade, TradeCostAttribution] | None:
        """Whether this bar ended the position, and the trade and split it made.

        The stop and the target are asked first and the time stop second, so a
        bar that could have done either is charged the stop -- the same
        pessimism ``resolve_exit`` applies within a bar (D-2).

        ``position.stop``, not ``signal.stop``: under a chandelier policy this
        is the trailed level, which the loop moved at the END of an earlier
        bar. That ordering is the honest one. Trailing from THIS bar's high and
        then testing THIS bar's low against the result would let a bar that
        swept down and then rallied report an exit at a stop the market never
        traded through in that order -- and it flatters rather than punishes,
        because a raised long stop that the bar's low reaches exits at a BETTER
        price than the original. A bar's OHLC cannot order its own extremes,
        which is the same fact D-2 exists to respect.
        """

        signal = position.signal
        closed = resolve_exit(
            bar=bar,
            side=signal.side,
            stop=position.stop,
            target=signal.target,
            contract=self._contract,
            model=request.cost_model,
        )
        if closed is None and bar.event_time >= position.deadline:
            # The deadline is known at entry, so the first bar to open at or
            # after it is a price this simulator could genuinely have taken.
            closed = Exit(kind=ExitKind.TIME, fill=self._closing_fill(bar, signal.side, request))
        if closed is None:
            return None
        return self._trade(position, closed, request)

    def _closing_fill(self, bar: Bar, side: Side, request: BacktestRequest) -> Fill:
        """A time-stopped exit prices exactly like an entry on the opposite
        side: the same half-spread crossing, the same slippage sign. Reusing
        ``entry_fill`` keeps one fill rule rather than two that can drift."""

        closing_side = Side.SELL if side is Side.BUY else Side.BUY
        return entry_fill(
            bar=bar, side=closing_side, contract=self._contract, model=request.cost_model
        )

    def _trade(
        self, position: _Position, closed: Exit, request: BacktestRequest
    ) -> tuple[SimulatedTrade, TradeCostAttribution]:
        signal = position.signal
        gross_pnl = self._gross_pnl(
            side=signal.side,
            entry_price=position.entry.price,
            exit_price=closed.fill.price,
            lots=signal.lots,
        )
        # Priced from the two RAW prices, so ``market_pnl`` is the move the
        # market made. The fill prices already contain the spread and the
        # slippage, and pricing from those would make this a second copy of
        # ``gross_pnl`` -- which is the restatement this split exists to avoid.
        market_pnl = self._gross_pnl(
            side=signal.side,
            entry_price=position.entry.raw_price,
            exit_price=closed.fill.raw_price,
            lots=signal.lots,
        )
        spread_cost = self._price_to_money(
            position.entry.spread_charged + closed.fill.spread_charged, signal.lots
        )
        slippage_cost = self._price_to_money(
            position.entry.slippage_charged + closed.fill.slippage_charged, signal.lots
        )
        commission = commission_cost(model=request.cost_model, lots=signal.lots)
        swap = swap_cost(
            model=request.cost_model,
            side=signal.side,
            lots=signal.lots,
            contract=self._contract,
            opened_at=position.entry.at,
            closed_at=closed.fill.at,
        )
        return (
            SimulatedTrade(
                proposal_id=signal.proposal_id,
                side=signal.side,
                lots=signal.lots,
                entry_price=position.entry.price,
                entry_at=position.entry.at,
                exit_price=closed.fill.price,
                exit_at=closed.fill.at,
                exit_kind=closed.kind,
                gross_pnl=gross_pnl,
                commission=commission,
                swap=swap,
                net_pnl=gross_pnl - commission + swap,
            ),
            # Built from the components and NOT from ``gross_pnl``: the two are
            # computed by different routes on purpose, and the whole claim is
            # that they land on the same number. Copying would make the check on
            # ``BacktestOutcome`` a tautology.
            TradeCostAttribution(
                proposal_id=signal.proposal_id,
                market_pnl=market_pnl,
                spread_cost=spread_cost,
                slippage_cost=slippage_cost,
                post_fill_gross=market_pnl - spread_cost - slippage_cost,
            ),
        )

    def _price_to_money(self, price_delta: Decimal, lots: Decimal) -> Decimal:
        """A price distance in account currency, for one quantity.

        The single division by ``price_increment`` runs last, on an
        already-exact numerator -- the shape ``swap_cost`` uses and the reason
        two runs over identical inputs cannot differ only in trailing zeros and
        so cannot differ in digest.

        The identity the attribution rests on is exact only when that division
        terminates, i.e. when ``point_size / price_increment`` is a power of ten.
        ``price_increment`` is an unconstrained ``PositiveDecimal``, so it need
        not be: at ``0.00003`` the separately rounded components do not sum to
        the singly rounded total and a split can differ from ``gross_pnl`` by an
        ulp, at which point ``BacktestOutcome`` **refuses the run**. That
        refusal is intended and is not to be loosened -- a decomposition that
        cannot reconstruct its own total to the last digit is not a
        decomposition, and failing closed is the right direction to be wrong in.
        MT5's tick size is a power of ten, so no contract in practice reaches
        it.
        """

        return (
            price_delta
            * lots
            * self._contract.value_per_price_increment
            / self._contract.price_increment
        )

    def _gross_pnl(
        self, *, side: Side, entry_price: Decimal, exit_price: Decimal, lots: Decimal
    ) -> Decimal:
        """The move in the trade's favour, priced.

        Named "gross" and gross of only two of section 7.1's five terms.
        ``entry_price`` and ``exit_price`` come from the fill model, which has
        already charged the half-spread and the slippage offset into both, so
        this number already contains those two and excludes only
        ``commission`` and ``swap`` -- the two ``SimulatedTrade`` carries as
        named lines beside it. ``net_pnl`` is right either way.

        Neither of them is taken back out of it. Spread and slippage are still
        inside this number; what changed is that they are no longer left
        *only* inside it, because the full split now lives in
        ``costs_attribution.TradeCostAttribution`` beside the trade -- which
        prices the raw move separately and subtracts what each leg charged, so
        the two are stated as terms rather than folded into one number. That is
        what a decomposition is; a label would have been a second copy of this.
        """

        move = exit_price - entry_price if side is Side.BUY else entry_price - exit_price
        return self._price_to_money(move, lots)

    def _result(
        self,
        request: BacktestRequest,
        *,
        trades: tuple[SimulatedTrade, ...],
        rejections: tuple[tuple[str, ...], ...],
        bars_seen: int,
        defective_bars: int,
        tolerance_fraction: Fraction,
        exit_policy: ExitPolicy,
        snapshots_skipped: int,
    ) -> BacktestResult:
        return BacktestResult(
            run_id=self._run_id(request, exit_policy, tolerance_fraction),
            strategy_id=request.strategy.id,
            strategy_version=request.strategy.version,
            exit_policy=exit_policy,
            constitution_sha256=self._constitution_sha256,
            contract_sha256=self._contract_sha256,
            instrument_id=request.instrument_id,
            timeframe=request.timeframe,
            start=request.start,
            end=request.end,
            firm_equity=request.firm_equity,
            cost_model=request.cost_model,
            atr_period=request.atr_period,
            spread_window=request.spread_window,
            defective_bar_tolerance=tolerance_fraction,
            trades=trades,
            rejections=rejections,
            bars_seen=bars_seen,
            defective_bars=defective_bars,
            snapshots_skipped=snapshots_skipped,
            net_pnl=sum((trade.net_pnl for trade in trades), Decimal(0)),
        )

    def _run_id(
        self,
        request: BacktestRequest,
        exit_policy: ExitPolicy,
        tolerance_fraction: Fraction,
    ) -> str:
        """Derived from the request, never minted.

        Phase 8 hashes ``BacktestResult.digest()`` into a trial ledger beside a
        Sharpe ratio. A uuid4 or a wall-clock stamp would give two runs over
        identical inputs different digests, which is exactly the reproducibility
        the ledger exists to provide.
        """

        identity = {
            "strategy_id": request.strategy.id,
            "strategy_version": request.strategy.version,
            "exit_policy": exit_policy.model_dump(mode="json"),
            "constitution_sha256": self._constitution_sha256,
            "contract_sha256": self._contract_sha256,
            "instrument_id": request.instrument_id,
            "timeframe": request.timeframe.value,
            "start": request.start.isoformat(),
            "end": request.end.isoformat(),
            "atr_period": request.atr_period,
            "spread_window": request.spread_window,
            "defective_bar_tolerance": str(tolerance_fraction),
        }
        # Scenario identity enters the id only when non-default (C-4), so every
        # constant 1.0x run keeps the id it had before these keys existed.
        multiplier = request.cost_model.stress_multiplier
        if multiplier != 1:
            identity["stress_multiplier"] = format(multiplier.normalize(), "f")
        if request.sizing is SizingMode.COMPOUNDING:
            identity["sizing"] = SizingMode.COMPOUNDING.value
        canonical = json.dumps(identity, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()
