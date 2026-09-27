"""Shared fakes and builders for the backtest engine tests.

The ``_run`` helper assembles a real ``FeatureEngine`` and a real ``RiskEngine``
on the real signed constitution -- only the bar store, the margin port and the
strategy are fakes. D-3 says the risk engine sizes and the simulator never
does, so a fake risk engine here would test the simulator against a sizing
rule nothing in production uses.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import cache
from pathlib import Path

from trading_house.constitution.loader import LoadedConstitution, load_constitution
from trading_house.constitution.models import Constitution
from trading_house.core.errors import CoverageError
from trading_house.core.instruments import FillPolicy, FinancingModel, InstrumentContract
from trading_house.core.schemas import Side, TradeProposal
from trading_house.core.values import AssetClass
from trading_house.features.engine import WARMUP_MULTIPLE
from trading_house.marketdata.models import Bar, BarQuality, Coverage, Timeframe, duration
from trading_house.research.backtest.costs import CostModel
from trading_house.research.backtest.engine import (
    Backtester,
    BacktestRequest,
    ReplayClock,
)
from trading_house.research.backtest.mark import BacktestOutcome
from trading_house.research.backtest.result import BacktestResult
from trading_house.research.backtest.snapshot import FeatureSnapshot
from trading_house.research.backtest.strategy import ExitPolicy, NoExitPolicy
from trading_house.risk.engine import RiskEngine

CONFIG_DIR = Path(__file__).resolve().parents[4] / "config"

ORIGIN = datetime(2026, 9, 21, 7, 0, tzinfo=UTC)
"""Where ``_ramp``'s series starts: bar 0 opens here.

Pinned to 07:00 -- the London session's own start -- rather than some hour
inside it. ``session_open_price``/``bars_since_session_open`` raise
``InsufficientHistoryError`` when the store's coverage begins after the
current session window started (engine.py's replay loop then skips the bar,
the same as an ATR window still warming up). ``FakeBarReader``'s coverage is
exactly this fixture's earliest bar, so anchoring the ramp inside the London
hour rather than at its boundary would make every snapshot in every test
below hit that guard and the strategy would never be asked anything. All
`_ramp` calls below stay under 90 bars, so the series never runs past 16:00
and never crosses into a second session."""

POINT = Decimal("0.00001")
RAMP_SPREAD_POINTS = 10
HALF_SPREAD = Decimal("0.00005")
"""10 points at ``point_size`` 0.00001. Every fill in ``_ramp``'s series is a
bar open plus or minus exactly this, because ``_run``'s cost model states
``slippage_points_per_side`` as zero -- so the hand-computed answer stays the
bar open and the spread, with no third term."""

ATR_PERIOD = 2
"""``FeatureEngine`` hands ATR ``period * WARMUP_MULTIPLE + 1`` bars, so period
2 needs 21 bars of history before it answers at all. A 60-bar ramp cannot
afford the usual 14 (141 bars), and the warm-up length is why the first
decision in every test below lands on bar 20 rather than bar 0."""

FIRST_SNAPSHOT_BAR = ATR_PERIOD * WARMUP_MULTIPLE
"""The index of the first bar a strategy is ever asked about: the first one
with ``period * WARMUP_MULTIPLE`` bars behind it. Derived, not written down, so
a change to ``ATR_PERIOD`` moves the tests with it."""

SPREAD_WINDOW = 10

HOLDING_SECONDS = 660
"""Eleven minutes from an entry's stamped ``entry_at``, which is the entry
bar's ``event_time`` -- the instant its price is drawn from. The deadline
therefore lands on the open of the bar exactly eleven bars after the one the
entry filled on: entry on bar 21's open, exit on bar 32's open."""


def ramp_price(index: int) -> Decimal:
    """The open of bar ``index`` in ``_ramp``'s series, by its definition."""

    return Decimal("1.10000") + index * POINT


@cache
def _loaded_constitution() -> LoadedConstitution:
    return load_constitution(
        CONFIG_DIR / "risk_constitution.yaml",
        CONFIG_DIR / "risk_constitution.yaml.sig",
        CONFIG_DIR / "risk_constitution.public.pem",
    )


def _constitution() -> Constitution:
    return _loaded_constitution().constitution


def _contract(**overrides: object) -> InstrumentContract:
    """A 5-digit FX contract: 1 lot moves $1 per 0.00001 of price."""

    defaults: dict[str, object] = {
        "instrument_id": "fx.eurusd",
        "asset_class": AssetClass.FX,
        "base_currency": "EUR",
        "quote_currency": "USD",
        "price_increment": POINT,
        "point_size": POINT,
        "quantity_increment": Decimal("0.01"),
        "quantity_min": Decimal("0.01"),
        "quantity_max": Decimal("100"),
        "value_per_price_increment": Decimal("1"),
        "min_stop_distance": Decimal("0.0002"),
        "freeze_distance": Decimal("0.0001"),
        "session_calendar_id": "fx.24x5",
        "financing": FinancingModel.SWAP,
        "can_open_long": True,
        "can_open_short": True,
        "supported_fills": frozenset({FillPolicy.IOC, FillPolicy.FOK}),
    }
    return InstrumentContract(**{**defaults, **overrides})  # type: ignore[arg-type]


def _cost_model(**overrides: object) -> CostModel:
    """Every field stated: a cost this model defaulted to zero is the classic
    flattering backtest (D-5). Slippage is deliberately zero so the
    known-answer prices stay derivable from the ramp and the spread alone."""

    defaults: dict[str, object] = {
        "commission_per_lot_per_side": Decimal("3.50"),
        "slippage_points_per_side": Decimal("0"),
        "swap_long_points_per_day": Decimal("-0.80"),
        "swap_short_points_per_day": Decimal("0.30"),
        "triple_swap_weekday": 2,
        "stress_multiplier": Decimal(1),
    }
    return CostModel(**{**defaults, **overrides})  # type: ignore[arg-type]


def _bar(
    *,
    high: Decimal,
    low: Decimal | None = None,
    close: Decimal | None = None,
    event_time: datetime | None = None,
    spread: int = RAMP_SPREAD_POINTS,
) -> Bar:
    """One standalone bar, for the tests that ask a pure function about a price
    rather than replaying a series.

    ``close`` defaults to ``high`` rather than to some lower price on purpose.
    ``trail_candidate`` clamps against the close, so a default below the high
    would make the CLAMP -- not the monotonic check -- the reason a falling bar
    returns ``None``, and the monotonicity test would pass without ever
    reaching the rule it names. ``low`` then defaults below both, so the bar
    stays coherent whatever the caller passes for ``close``.
    """

    settled = close if close is not None else high
    return Bar(
        instrument_id="fx.eurusd",
        timeframe=Timeframe.M1,
        event_time=event_time if event_time is not None else ORIGIN,
        availability_time=(event_time if event_time is not None else ORIGIN)
        + duration(Timeframe.M1),
        open=settled,
        high=high,
        low=low if low is not None else min(high, settled) - POINT,
        close=settled,
        tick_volume=0,
        spread=spread,
        real_volume=0,
        quality=BarQuality.OK,
    )


def _ramp(n: int, *, start: datetime | None = None) -> tuple[Bar, ...]:
    """``n`` consecutive M1 bars rising by exactly one point per bar.

    Bar *i* opens and closes at ``ramp_price(i)`` with its high one point
    above and its low one point below, so the true range of every bar is
    exactly two points and Wilder's ATR over the series is exactly 0.00002.

    ``start`` defaults to ``ORIGIN`` (the London session's own boundary). A
    caller that wants a store whose coverage begins partway through its first
    session -- the case ``session_open_price``/``bars_since_session_open``
    raise ``InsufficientHistoryError`` for -- passes a later ``start`` still
    inside the same session window.
    """

    origin = start if start is not None else ORIGIN
    return tuple(
        Bar(
            instrument_id="fx.eurusd",
            timeframe=Timeframe.M1,
            event_time=origin + timedelta(minutes=index),
            availability_time=origin + timedelta(minutes=index) + duration(Timeframe.M1),
            open=ramp_price(index),
            high=ramp_price(index) + POINT,
            low=ramp_price(index) - POINT,
            close=ramp_price(index),
            tick_volume=0,
            spread=RAMP_SPREAD_POINTS,
            real_volume=0,
            quality=BarQuality.OK,
        )
        for index in range(n)
    )


def _session_ramp(n: int = 65) -> tuple[Bar, ...]:
    """M15 bars spanning the Asian session, London open, and 16:00 close."""

    origin = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
    return tuple(
        Bar(
            instrument_id="fx.eurusd",
            timeframe=Timeframe.M15,
            event_time=origin + timedelta(minutes=15 * index),
            availability_time=origin + timedelta(minutes=15 * (index + 1)),
            open=Decimal("1.10000") + index * POINT * 2,
            high=Decimal("1.10000") + index * POINT * 2 + POINT,
            low=Decimal("1.10000") + index * POINT * 2 - POINT,
            close=Decimal("1.10000") + index * POINT * 2,
            tick_volume=100,
            spread=RAMP_SPREAD_POINTS,
            real_volume=0,
            quality=BarQuality.OK,
        )
        for index in range(n)
    )


@dataclass(slots=True)
class FakeBarReader:
    """A ``BarReader`` over a list held in memory.

    ``held`` is public on purpose. A test staging a store that changes its mind
    between cycles assigns to it, and a private name would accept the
    assignment silently while this fake went on serving the old bars -- which
    is how a test passes for a reason unrelated to what it checks.
    """

    held: tuple[Bar, ...]
    coverage_calls: int = 0
    bars_calls: int = 0

    def bars(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        *,
        start: datetime,
        end: datetime,
        as_of: datetime,
        include_defective: bool = False,
    ) -> tuple[Bar, ...]:
        """``[start, end)`` by event time, knowable at ``as_of`` -- the real
        store's contract, including its ``CoverageError`` on a start that
        reaches further back than anything held."""

        self.bars_calls += 1
        coverage = self._coverage(instrument_id, timeframe)
        if coverage.earliest_event_time is None or start < coverage.earliest_event_time:
            raise CoverageError
        return tuple(
            bar
            for bar in sorted(self.held, key=lambda bar: bar.event_time)
            if bar.instrument_id == instrument_id
            and bar.timeframe is timeframe
            and start <= bar.event_time < end
            and bar.availability_time <= as_of
            and (include_defective or bar.quality is BarQuality.OK)
        )

    def coverage(self, instrument_id: str, timeframe: Timeframe) -> Coverage:
        self.coverage_calls += 1
        return self._coverage(instrument_id, timeframe)

    def _coverage(self, instrument_id: str, timeframe: Timeframe) -> Coverage:
        matching = [
            bar
            for bar in self.held
            if bar.instrument_id == instrument_id and bar.timeframe is timeframe
        ]
        return Coverage(
            instrument_id=instrument_id,
            timeframe=timeframe,
            earliest_event_time=min((bar.event_time for bar in matching), default=None),
            latest_event_time=max((bar.event_time for bar in matching), default=None),
            latest_availability_time=max((bar.availability_time for bar in matching), default=None),
            clean_bars=sum(1 for bar in matching if bar.quality is BarQuality.OK),
            defective_bars=sum(1 for bar in matching if bar.quality is not BarQuality.OK),
        )


@dataclass(frozen=True, slots=True)
class AlwaysAffordableMargin:
    """A ``MarginPort`` that never binds. ``required_margin`` must stay strictly
    positive: the engine rejects a non-positive requirement outright."""

    def free_margin(self) -> Decimal:
        return Decimal("1000000")

    def required_margin(
        self, *, instrument_id: str, side: Side, quantity: Decimal, price: Decimal
    ) -> Decimal:
        return Decimal("1")


@dataclass(slots=True)
class ToyStrategy:
    """Buys every ``every_n``-th snapshot it is handed, and records them all.

    Counting snapshots rather than bars is what makes the entries land on bars
    20 and 40 for ``every_n=20``: bars 0-19 have too little history for the ATR
    window, so no snapshot is built for them and the strategy is never asked.
    """

    every_n: int = 1
    horizon_seconds: int = 7200
    max_holding_seconds: int = HOLDING_SECONDS
    proposal_holding_seconds: int | None = None
    """What the PROPOSAL carries, when it differs from the strategy's own
    number. Only a strategy sizing its horizon per proposal has the two
    diverge, and the engine must honour the proposal's -- which is exactly
    what a strategy stating one number cannot show."""

    policy: ExitPolicy = field(default_factory=lambda: NoExitPolicy(kind="none"))
    """The declared arm. ``none`` is the baseline the other two are measured
    against, so it is the default here for the same reason it is the default
    in a strategy spec: nothing is trailed and no target is honoured unless a
    run says so out loud."""

    target_r_multiple: Decimal | None = None
    """Stamped on the proposal for the risk engine to price a target from.
    Deliberately independent of ``policy``: a run that sets this WITHOUT
    naming ``fixed_target`` is what proves the engine gates the target on the
    arm rather than on whatever the risk decision happened to carry."""

    id: str = "toy"
    version: str = "1"
    book: str = "fx_swing"
    seen: list[FeatureSnapshot] = field(default_factory=list)

    def evaluate(self, snapshot: FeatureSnapshot) -> TradeProposal | None:
        self.seen.append(snapshot)
        if (len(self.seen) - 1) % self.every_n != 0:
            return None
        return self._proposal(snapshot)

    def exit_policy(self) -> ExitPolicy:
        return self.policy

    def _stamps(self, snapshot: FeatureSnapshot) -> dict[str, object]:
        return {
            "event_time": snapshot.as_of,
            "availability_time": snapshot.as_of,
            "processing_time": snapshot.as_of,
        }

    def _proposal(self, snapshot: FeatureSnapshot) -> TradeProposal:
        entry = snapshot.bar.close
        return TradeProposal(
            source="toy-strategy",
            proposal_id=f"toy-{snapshot.as_of.isoformat()}",
            strategy_id=self.id,
            strategy_version=self.version,
            book=self.book,
            instrument_id=snapshot.instrument_id,
            side=Side.BUY,
            horizon_seconds=self.horizon_seconds,
            entry_condition="ramp",
            entry_price_ref=entry,
            # 100 points below the reference, so the structural term is the
            # widest of section 8.2's four and the stop distance is exactly
            # 0.00100 -- which is what makes the lot size hand-computable.
            invalidation_price=entry - Decimal("0.00100"),
            target_r_multiple=self.target_r_multiple,
            max_holding_seconds=self.proposal_holding_seconds or self.max_holding_seconds,
            expected_return_bps=5.0,
            expected_return_stdev_bps=2.0,
            expected_cost_bps=1.0,
            expected_swap_cost_bps=0.0,
            win_probability=0.55,
            calibration_id="c-1",
            required_liquidity={"amount": Decimal("1"), "unit": "lots"},  # type: ignore[arg-type]
            regime_ref="r-1",
            features_snapshot_id=f"snap-{snapshot.as_of.isoformat()}",
            **self._stamps(snapshot),  # type: ignore[arg-type]
        )


class PeekingStrategy(ToyStrategy):
    """``ToyStrategy`` with one difference: its proposal claims to have been
    available one second after the snapshot that produced it."""

    def _stamps(self, snapshot: FeatureSnapshot) -> dict[str, object]:
        later = snapshot.as_of + timedelta(seconds=1)
        return {
            "event_time": snapshot.as_of,
            "availability_time": later,
            "processing_time": later,
        }


def _outcome(
    *,
    bars: tuple[Bar, ...],
    strategy: ToyStrategy,
    firm_equity: Decimal = Decimal("100000"),
    start: datetime | None = None,
    end: datetime | None = None,
    defective_bar_tolerance: Decimal | None = None,
    contract: InstrumentContract | None = None,
    constitution_sha256: str | None = None,
    atr_period: int = ATR_PERIOD,
    spread_window: int = SPREAD_WINDOW,
    reader: FakeBarReader | None = None,
) -> BacktestOutcome:
    source = reader if reader is not None else FakeBarReader(bars)
    # One clock, shared: the risk engine's tick-freshness gate compares its own
    # clock to the snapshot's tick_time, so the backtester must advance the
    # very clock the risk engine reads. A wall clock would make every stored
    # bar stale and reject every proposal in the run.
    clock = ReplayClock(instant=bars[0].availability_time)
    selected_contract = contract if contract is not None else _contract()
    tester = Backtester(
        bars=source,
        risk=RiskEngine(_constitution(), clock),
        margin=AlwaysAffordableMargin(),
        contract=selected_contract,
        clock=clock,
        constitution_sha256=(
            constitution_sha256
            if constitution_sha256 is not None
            else _loaded_constitution().constitution_sha256
        ),
    )
    tolerance_kwargs = (
        {}
        if defective_bar_tolerance is None
        else {"defective_bar_tolerance": defective_bar_tolerance}
    )
    return tester.run(
        BacktestRequest(
            strategy=strategy,
            instrument_id="fx.eurusd",
            timeframe=Timeframe.M1,
            start=start if start is not None else bars[0].event_time,
            end=end if end is not None else bars[-1].event_time,
            firm_equity=firm_equity,
            cost_model=_cost_model(),
            atr_period=atr_period,
            spread_window=spread_window,
            **tolerance_kwargs,
        )
    )


def _run(
    *,
    bars: tuple[Bar, ...],
    strategy: ToyStrategy,
    firm_equity: Decimal = Decimal("100000"),
    start: datetime | None = None,
    end: datetime | None = None,
    defective_bar_tolerance: Decimal | None = None,
    contract: InstrumentContract | None = None,
    constitution_sha256: str | None = None,
    atr_period: int = ATR_PERIOD,
    spread_window: int = SPREAD_WINDOW,
    reader: FakeBarReader | None = None,
) -> BacktestResult:
    """The result, for the many tests that only care about trades and their totals.

    Every one of the 41 call sites, all of them in ``test_engine.py``, wants the
    result and not the series, so this keeps them untouched. The series is
    reached through ``_outcome``, and the two cannot drift because one calls the
    other. The keywords stay explicit here rather than collapsing to
    ``**kwargs: Any``, because a mistyped keyword at those call sites is a type
    error the moment it is written and a runtime surprise long after.
    """

    return _outcome(
        bars=bars,
        strategy=strategy,
        firm_equity=firm_equity,
        start=start,
        end=end,
        defective_bar_tolerance=defective_bar_tolerance,
        contract=contract,
        constitution_sha256=constitution_sha256,
        atr_period=atr_period,
        spread_window=spread_window,
        reader=reader,
    ).result
