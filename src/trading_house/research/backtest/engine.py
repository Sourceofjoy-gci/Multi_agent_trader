"""The replay loop: the one stateful piece of the backtester.

Tasks 1-4 built the snapshot, the cost model, the fill model and the result;
this composes them and owns nothing but the order in which they are called.
That order is the whole design, because a simulator flatters a strategy by
doing the right things in the wrong sequence. Per bar:

1. A queued entry fills at THIS bar's open. Never the close that generated the
   signal -- section 11.1's named violation.
2. Any open position is resolved against this bar: ``resolve_exit`` first (the
   stop before the target, D-2), then the engine's own time stop.
3. ``as_of`` becomes the bar's ``availability_time``, and the shared
   ``ReplayClock`` is advanced to it.
4. A ``FeatureSnapshot`` is built at that ``as_of``, or the bar is skipped
   because the feature windows have no history yet.
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

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_house.core.clock import ensure_utc
from trading_house.core.errors import InsufficientHistoryError, TradingHouseError
from trading_house.core.instruments import InstrumentContract
from trading_house.core.schemas import RejectedRiskDecision, Side
from trading_house.features.engine import BarReader, FeatureEngine
from trading_house.features.sessions import session_of
from trading_house.marketdata.models import Bar, BarQuality, Timeframe, duration
from trading_house.research.backtest.costs import CostModel, commission_cost, swap_cost
from trading_house.research.backtest.fills import Exit, ExitKind, Fill, entry_fill, resolve_exit
from trading_house.research.backtest.result import BacktestResult, RefusalKind, SimulatedTrade
from trading_house.research.backtest.snapshot import FeatureSnapshot, horizon_is_simulatable
from trading_house.research.backtest.strategy import Strategy
from trading_house.risk.engine import MarginPort, RiskEngine

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

    ``firm_equity`` is constant for the whole run (D-4): this simulator does
    not compound, so a strategy's measured edge cannot be an artefact of
    position sizes growing with its own luck.

    A dataclass rather than a ``CanonicalModel`` because ``strategy`` is a bare
    ``Protocol``: pydantic cannot build a schema for one, and widening the base
    config with ``arbitrary_types_allowed`` to admit it would weaken every
    canonical model in the package. So the three checks the model's own
    annotations would have made -- ``PositiveDecimal``, UTC-awareness on both
    stamps, and an ordered range -- are made here by hand. ``end < start`` is
    refused in ``run`` rather than raised here, beside the other range
    refusals; see ``_refuse_outside_coverage``.
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

    def __post_init__(self) -> None:
        # The boundary carrying the money. A float here would survive every
        # later Decimal arithmetic as a silently inexact number, and a
        # non-positive equity sizes nothing while looking like a strategy that
        # proposed nothing.
        if not isinstance(self.firm_equity, Decimal):
            raise ValueError("firm_equity must be a Decimal")
        if self.firm_equity <= 0:
            raise ValueError("firm_equity must be positive")
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


@dataclass(frozen=True, slots=True)
class _Position:
    signal: _Signal
    entry: Fill
    deadline: datetime


class Backtester:
    """Replays a strategy over stored bars. One position at a time (D-7)."""

    def __init__(
        self,
        *,
        bars: BarReader,
        features: FeatureEngine,
        risk: RiskEngine,
        margin: MarginPort,
        contract: InstrumentContract,
        clock: ReplayClock,
    ) -> None:
        self._bars = bars
        self._features = features
        self._risk = risk
        self._margin = margin
        self._contract = contract
        self._clock = clock

    def run(self, request: BacktestRequest) -> BacktestResult:
        strategy = request.strategy
        if not horizon_is_simulatable(
            horizon_seconds=strategy.horizon_seconds, timeframe=request.timeframe
        ):
            raise BacktestRefused(RefusalKind.HORIZON)
        self._refuse_outside_coverage(request)

        trades: list[SimulatedTrade] = []
        rejections: list[tuple[str, ...]] = []
        position: _Position | None = None
        queued: _Signal | None = None
        bars_seen = 0
        snapshots_skipped = 0

        for bar in self._replay_bars(request):
            if bar.quality is not BarQuality.OK:
                raise BacktestRefused(RefusalKind.DEFECTIVE_BAR)
            bars_seen += 1

            if queued is not None:
                position = self._open(queued, bar, request)
                queued = None
            if position is not None:
                closed = self._close_if_done(position, bar, request)
                if closed is not None:
                    trades.append(closed)
                    position = None

            as_of = bar.availability_time
            self._clock.instant = as_of
            snapshot = self._snapshot(request, bar, as_of)
            if snapshot is None:
                snapshots_skipped += 1
                continue
            proposal = strategy.evaluate(snapshot)
            if proposal is None:
                continue
            if proposal.availability_time > snapshot.as_of:
                raise BacktestRefused(RefusalKind.LOOKAHEAD)
            if position is not None or queued is not None:
                continue

            decision = self._risk.evaluate_for_execution(
                proposal,
                margin=self._margin,
                contract=self._contract,
                firm_equity=request.firm_equity,
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
                # Dormant, and deliberately wired anyway. ``RiskEngine``
                # hard-codes ``take_profit_price`` to ``None`` on every
                # executable decision (risk/engine.py), so this is always
                # ``None`` and ``ExitKind.TARGET`` cannot occur in any run this
                # phase can perform -- D-2's stop-before-target pessimism is
                # proven in ``resolve_exit``'s unit tests and nowhere end to
                # end. The wiring stays because the day the risk engine starts
                # producing take-profits is the day the simulator must honour
                # them; ``test_engine.py`` pins the dormancy so that day
                # announces itself. Producing them is Phase 7's, not this
                # phase's.
                target=decision.take_profit_price,
                max_holding_seconds=proposal.max_holding_seconds,
            )

        # A position still open when the bars run out is discarded, and a
        # queued signal never fills. Closing it at the last bar would report a
        # trade the requested period cannot justify -- the exit price would
        # come from the range's edge rather than from anything the position's
        # own rules asked for. An unfinished trade is not a result.
        return self._result(
            request,
            trades=tuple(trades),
            rejections=tuple(rejections),
            bars_seen=bars_seen,
            snapshots_skipped=snapshots_skipped,
        )

    def _refuse_outside_coverage(self, request: BacktestRequest) -> None:
        """Coverage is a boundary, not a list of holes.

        ``Coverage`` exposes ``earliest_event_time``, ``latest_event_time`` and
        bar counts -- never gaps -- so this refuses a requested range that
        reaches outside the window the store holds, and nothing more. Interior
        gaps are deliberately not refused: FX closes every weekend, so a gap
        rule would refuse every run spanning a Saturday and the feature would
        be useless. This is what section 8's "coverage gap" means in the only
        terms the store can actually answer.

        Checked before any bar is read, so a run that cannot be simulated
        honestly never starts.

        A backwards range is refused here too, for the same reason and with the
        same kind. ``end < start`` reaches no bar in any store: the loop never
        runs and the result is ``bars_seen=0`` with no trades, which is
        indistinguishable from a strategy that proposed nothing. That is the
        silent lie this refusal exists to prevent, reached through a different
        door. ``start == end`` is a legal one-bar run -- both are inclusive.
        """

        if request.end < request.start:
            raise BacktestRefused(RefusalKind.COVERAGE)
        coverage = self._bars.coverage(request.instrument_id, request.timeframe)
        if coverage.earliest_event_time is None or coverage.latest_event_time is None:
            raise BacktestRefused(RefusalKind.COVERAGE)
        if request.start < coverage.earliest_event_time:
            raise BacktestRefused(RefusalKind.COVERAGE)
        if request.end > coverage.latest_event_time:
            raise BacktestRefused(RefusalKind.COVERAGE)

    def _replay_bars(self, request: BacktestRequest) -> tuple[Bar, ...]:
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
        return self._bars.bars(
            request.instrument_id,
            request.timeframe,
            start=request.start,
            end=horizon,
            as_of=horizon,
            include_defective=True,
        )

    def _snapshot(
        self, request: BacktestRequest, bar: Bar, as_of: datetime
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
            atr = self._features.atr(
                request.instrument_id,
                request.timeframe,
                period=request.atr_period,
                as_of=as_of,
            )
            median_spread_points = self._features.median_spread_points(
                request.instrument_id,
                request.timeframe,
                window=request.spread_window,
                as_of=as_of,
            )
            session_open_price = self._features.session_open_price(
                request.instrument_id, request.timeframe, as_of=as_of
            )
            bars_since_session_open = self._features.bars_since_session_open(
                request.instrument_id, request.timeframe, as_of=as_of
            )
        except InsufficientHistoryError:
            return None
        prior_session_return = self._features.prior_session_return(
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
        )

    def _close_if_done(
        self, position: _Position, bar: Bar, request: BacktestRequest
    ) -> SimulatedTrade | None:
        """Whether this bar ended the position, and the trade it produced.

        The stop and the target are asked first and the time stop second, so a
        bar that could have done either is charged the stop -- the same
        pessimism ``resolve_exit`` applies within a bar (D-2).
        """

        signal = position.signal
        closed = resolve_exit(
            bar=bar,
            side=signal.side,
            stop=signal.stop,
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

    def _trade(self, position: _Position, closed: Exit, request: BacktestRequest) -> SimulatedTrade:
        signal = position.signal
        gross_pnl = self._gross_pnl(
            side=signal.side,
            entry_price=position.entry.price,
            exit_price=closed.fill.price,
            lots=signal.lots,
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
        return SimulatedTrade(
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
        named lines beside it. ``net_pnl`` is right either way; the breakdown
        is partial, and splitting spread and slippage out is Phase 8's, with
        the cost attribution that needs them. See ``SimulatedTrade``.

        Every exact factor multiplies first and the single division by
        ``price_increment`` runs last, on an already-exact numerator -- the
        shape ``swap_cost`` uses, and the reason two runs over identical inputs
        cannot differ only in trailing zeros and so cannot differ in digest.
        """

        move = exit_price - entry_price if side is Side.BUY else entry_price - exit_price
        return (
            move * lots * self._contract.value_per_price_increment / self._contract.price_increment
        )

    def _result(
        self,
        request: BacktestRequest,
        *,
        trades: tuple[SimulatedTrade, ...],
        rejections: tuple[tuple[str, ...], ...],
        bars_seen: int,
        snapshots_skipped: int,
    ) -> BacktestResult:
        return BacktestResult(
            run_id=self._run_id(request),
            strategy_id=request.strategy.id,
            strategy_version=request.strategy.version,
            instrument_id=request.instrument_id,
            timeframe=request.timeframe,
            start=request.start,
            end=request.end,
            firm_equity=request.firm_equity,
            cost_model=request.cost_model,
            trades=trades,
            rejections=rejections,
            bars_seen=bars_seen,
            snapshots_skipped=snapshots_skipped,
            net_pnl=sum((trade.net_pnl for trade in trades), Decimal(0)),
        )

    @staticmethod
    def _run_id(request: BacktestRequest) -> str:
        """Derived from the request, never minted.

        Phase 8 hashes ``BacktestResult.digest()`` into a trial ledger beside a
        Sharpe ratio. A uuid4 or a wall-clock stamp would give two runs over
        identical inputs different digests, which is exactly the reproducibility
        the ledger exists to provide.
        """

        return ":".join(
            (
                request.strategy.id,
                request.strategy.version,
                request.instrument_id,
                request.timeframe.value,
                request.start.isoformat(),
                request.end.isoformat(),
            )
        )
