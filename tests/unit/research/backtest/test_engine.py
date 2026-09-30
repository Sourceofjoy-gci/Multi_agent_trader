import hashlib
import json
from datetime import timedelta, timezone
from decimal import Decimal
from fractions import Fraction

import pytest

from tests.unit.research.backtest.conftest import (
    FIRST_SNAPSHOT_BAR,
    HALF_SPREAD,
    ORIGIN,
    POINT,
    FakeBarReader,
    PeekingStrategy,
    ToyStrategy,
    _bar,
    _constitution,
    _contract,
    _cost_model,
    _loaded_constitution,
    _outcome,
    _ramp,
    _run,
    ramp_price,
)
from trading_house.core.errors import EquityEvidenceError, TimestampError
from trading_house.core.schemas import Side
from trading_house.marketdata.models import Bar, BarQuality, Timeframe, duration
from trading_house.research.backtest import engine
from trading_house.research.backtest.costs import slippage_price_offset
from trading_house.research.backtest.engine import (
    BacktestRefused,
    ReplayClock,
    trail_candidate,
)
from trading_house.research.backtest.fills import ExitKind
from trading_house.research.backtest.result import BacktestResult, RefusalKind
from trading_house.research.backtest.sizing import SizingMode
from trading_house.research.backtest.strategy import (
    ChandelierPolicy,
    FixedTargetPolicy,
    NoExitPolicy,
)
from trading_house.risk.engine import RiskEngine


def test_a_known_answer_run_produces_exactly_the_hand_computed_trades() -> None:
    """The test this phase exists for. A synthetic series and a toy whose every
    entry and exit can be worked out on paper -- which is what validates the
    SIMULATOR rather than a strategy, and is why Phase 6 is split from Phase 7.

    60 M1 bars rising by exactly one point each, spread fixed at 10 points.
    The ATR window needs 21 bars, so the first snapshot is bar 20; ToyStrategy
    proposes on its 1st and 21st snapshot, i.e. bars 20 and 40. Each entry
    fills at the NEXT bar's open (bars 21 and 41), never the close that
    generated it -- bar 20 closes at 1.10020 while bar 21 opens at 1.10021, so
    the entry-price assertion below tells the two apart. Each is held to its
    660-second (eleven-minute) deadline from its entry bar's ``event_time``,
    which lands on the open of bar 32 and bar 52 -- eleven bars after the
    entry bar, exactly what ``max_holding_seconds`` states.

    Every expectation is derived from the ramp's closed form and the fill rule,
    never from the simulator:

        entry = ramp_price(21) + half spread,  exit = ramp_price(32) - half
        stop distance = 0.00100 (the structural term is the widest of 8.2's
            four), so 100 ticks at $1 a tick a lot
        book equity = 100000 x 0.45, risked at 0.75% = $337.50
        lots = 337.50 / 100 = 3.375, floored to the 0.01 grid = 3.37
        gross = 1 tick x 3.37 = $3.37 a trade (ramp_price(32) - ramp_price(21)
            is 11 points, less the two half-spreads crossed on entry and exit
            -- 11 - 5 - 5 = 1 point)
        commission = 2 sides x 3.37 lots x $3.50 = $23.59 a trade
        swap = 0 (opened and closed the same calendar day)
        net = 3.37 - 23.59 = -$20.22 a trade, -$40.44 over the run
    """

    outcome = _outcome(bars=_ramp(60), strategy=ToyStrategy(every_n=20))
    result = outcome.result

    assert [trade.exit_kind for trade in result.trades] == [ExitKind.TIME, ExitKind.TIME]
    assert result.trades[0].entry_price == ramp_price(21) + HALF_SPREAD
    assert result.trades[0].exit_price == ramp_price(32) - HALF_SPREAD
    assert result.trades[1].entry_price == ramp_price(41) + HALF_SPREAD
    assert result.trades[1].exit_price == ramp_price(52) - HALF_SPREAD
    # D-3: the lot size comes off the risk engine's decision. Sized against
    # firm equity instead of the book's 0.45 slice it would be 7.50 lots.
    assert [trade.lots for trade in result.trades] == [Decimal("3.37"), Decimal("3.37")]
    assert result.trades[0].gross_pnl == Decimal("3.37")
    assert result.trades[0].commission == Decimal("23.59")
    assert result.trades[0].swap == Decimal(0)
    # The one line that carries the run's arithmetic. Restating it per trade
    # would add nothing: SimulatedTrade already refuses a net its own terms do
    # not produce and BacktestResult already refuses a net that is not the sum
    # of the trades', so every per-trade restatement is true by construction.
    # Only the hand-computed absolute can fail.
    assert result.net_pnl == Decimal("-40.44")
    assert result.bars_seen == 60
    # Phase 8B1: one mark per processed bar, and the run closed flat, so the
    # final observation reconciles to net PnL. Both trades were TIME exits, so
    # cumulative realized at the close is exactly the -40.44 above.
    assert len(outcome.equity.observations) == outcome.result.bars_seen
    assert outcome.equity.is_flat is True
    assert outcome.equity.observations[-1].cumulative_realized_pnl == Decimal("-40.44")
    assert outcome.equity.observations[-1].unrealized_pnl == Decimal(0)
    assert outcome.equity.observations[-1].equity == Decimal("100000") + Decimal("-40.44")
    # Phase 8B2a: the decomposition reconstructs the number above exactly. A
    # fill-model regression fails a hand-computed value, not a shape. The raw
    # move is 11 points and the two crossed half-spreads are 5 and 5, so
    # market = 11 x 3.37 = 37.07, spread = 10 x 3.37 = 33.70, slippage = 0, and
    # 37.07 - 33.70 = 3.37. Only the absolutes are asserted: the identity and
    # the per-trade pairing are already guaranteed by ``BacktestOutcome``'s
    # construction, and the identity holds for any pair of numbers a symmetric
    # bug produced. A falsified split is refused in
    # ``test_costs_attribution.py``, which is where that check belongs.
    attribution = outcome.attribution
    # Both trades in this fixture are TIME exits, so each crosses a second
    # half-spread from its own exit bar.
    assert all(entry.spread_cost > 0 for entry in attribution.trades)
    assert all(entry.slippage_cost == 0 for entry in attribution.trades)
    assert attribution.trades[0].market_pnl == Decimal("37.07")
    assert attribution.trades[0].spread_cost == Decimal("33.70")
    assert attribution.trades[0].slippage_cost == Decimal(0)


def test_a_processed_bar_with_no_proposal_still_gets_a_mark() -> None:
    """One mark per bar the engine looked at, not per bar it traded.

    ``_ramp(60)`` gives 60 bars and the strategy asks for a proposal on every
    twentieth, so most bars are processed and untraded. The count is the whole
    claim: a mark per *trade* would understate the path. Tying the count to the
    processed bars rather than the raw ones is ``BacktestOutcome``'s invariant --
    one observation per ``bars_seen`` -- so a mark for a skipped bar could not
    land at all. ``_ramp(60)`` holds no defective bar, so this test cannot draw
    that distinction itself;
    ``test_a_defective_bar_within_tolerance_is_counted_and_skipped`` does, at
    39 observations against 40 bars.
    """

    outcome = _outcome(bars=_ramp(60), strategy=ToyStrategy(every_n=20))

    assert len(outcome.equity.observations) == 60
    assert outcome.result.bars_seen == 60
    assert len(outcome.result.trades) < 60


def test_a_run_past_the_observation_ceiling_refuses_rather_than_subsampling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one branch in the emission that fails closed, and the guard with the
    most room to rot: a ceiling nothing reaches is a ceiling nothing tests.

    Lowered rather than reached. The constant is 2,000,000 and the comparison is
    read from this module's global, so the honest way to cover it is to move the
    ceiling, not to build two million bars.

    Refused, not truncated. Sixty bars against a ceiling of three: an engine
    that stopped at the ceiling would have returned a three-observation series,
    and one that refuses returns no series at all. A subsample is evidence of
    something other than the run, which is the one thing this framework exists
    to prevent.
    """

    monkeypatch.setattr(engine, "MAX_EQUITY_OBSERVATIONS", 3)

    with pytest.raises(EquityEvidenceError) as error:
        _outcome(bars=_ramp(60), strategy=ToyStrategy(every_n=20))

    # Design section 7 promises the count and the limit, so both ride the
    # private cause. The count is 4, not 3: three observations are already
    # stored and it is the fourth mark that breaks the ceiling.
    assert error.value.__cause__ is not None
    assert str(error.value.__cause__) == "equity observations exceed the ceiling: 4 > 3"
    # And the public message stays as bare as every other code in this repo's
    # errors, so an edit that pastes the count into it fails here.
    assert str(error.value) == "mark-to-market equity evidence is not trustworthy"


def test_an_assembled_run_materializes_the_source_once_and_keeps_the_known_answer() -> None:
    bars = _ramp(60)
    source = FakeBarReader(bars)

    result = _run(
        bars=bars,
        strategy=ToyStrategy(every_n=20),
        reader=source,
    )

    assert (source.coverage_calls, source.bars_calls) == (1, 1)
    assert result.trades[0].entry_price == ramp_price(21) + HALF_SPREAD
    assert result.trades[0].exit_price == ramp_price(32) - HALF_SPREAD
    assert result.net_pnl == Decimal("-40.44")


def test_one_position_at_a_time_and_an_exit_frees_the_slot_on_its_own_bar() -> None:
    """D-7, and the loop order that makes it honest.

    Two properties, one predicate each, because they are two halves of the same
    decision. First: no trade opens before the previous one closed --
    ``max_concurrent_positions`` is enforced nowhere at decision time, so a
    simulator opening many would report risk the live engine permits while
    nothing in the live path limits it. Second: with a strategy proposing on
    every bar, the gap between an exit and the next entry is exactly one bar.
    Resolve the exit after the new entry is considered and the strategy is
    still holding a closed position when it is asked, so every re-entry slips a
    bar -- the run would show fewer, later trades than the live system took.
    """

    result = _run(bars=_ramp(60), strategy=ToyStrategy(every_n=1))

    assert len(result.trades) >= 2
    for earlier, later in zip(result.trades, result.trades[1:], strict=False):
        assert earlier.exit_at <= later.entry_at
        assert later.entry_at - earlier.exit_at == duration(Timeframe.M1)


def test_the_strategy_is_asked_once_per_bar_at_the_loops_own_position() -> None:
    """Which bar the engine fed in -- the part no validator can speak for.

    ``FeatureSnapshot`` already refuses unless ``as_of`` equals the bar's
    ``availability_time``, and ``Bar`` already refuses unless that is after
    ``event_time``, so asserting either here would assert Task 1's validators
    rather than this loop: no object violating them can exist to be caught.
    What they cannot see is WHICH bar the loop handed over, and how many. One
    snapshot per bar, in the store's order, from the first bar the ATR window
    can answer for through the last bar of the requested range -- including
    every bar where a position is open, because the D-7 guard drops the
    proposal after the strategy is asked rather than skipping the ask.
    """

    strategy = ToyStrategy(every_n=5)
    bars = _ramp(30)
    _run(bars=bars, strategy=strategy)

    assert [snapshot.bar.event_time for snapshot in strategy.seen] == [
        bar.event_time for bar in bars[FIRST_SNAPSHOT_BAR:]
    ]


def test_a_session_the_store_only_partly_covers_is_skipped_not_refused() -> None:
    """The replay loop's decision for the ``InsufficientHistoryError`` that
    ``session_open_price``/``bars_since_session_open`` raise at a session's
    opening instant: skip the bar and try the next one, exactly like an ATR
    or spread window still warming up. Refusing the whole run would fail an
    otherwise healthy multi-year replay at its very first session open.

    The ramp starts two minutes into the London session rather than at 07:00,
    its own boundary, so every bar's current-session window has a true start
    the store never covers -- ``_current_session_bars`` raises for all of
    them, every time. The run must still complete without that exception
    escaping, and the strategy -- which reads only what ``FeatureSnapshot``
    hands it -- must never be asked anything, because no snapshot exists to
    ask it about.
    """

    bars = _ramp(30, start=ORIGIN + timedelta(minutes=2))
    strategy = ToyStrategy(every_n=1)

    result = _run(bars=bars, strategy=strategy)

    assert strategy.seen == []
    assert result.trades == ()
    assert result.bars_seen == 30


def test_a_run_crossing_the_off_window_completes_and_counts_the_skips() -> None:
    """C1 + C2. Between 21:00 and midnight UTC a bar's reference session is
    ``Session.OFF``, which has no window -- before the fix,
    ``_current_session_bars`` passed ``as_of`` straight to ``session_bounds``
    and its bare ``ValueError`` propagated out of ``Backtester.run()``
    uncaught, since the loop only ever catches ``InsufficientHistoryError``.
    Every realistic multi-day backtest crosses 21:00 UTC daily.

    The ramp starts at ``ORIGIN`` (07:00, London's own boundary -- the
    default start, so London and New York are both fully covered, including
    the 12:00-16:00 overlap that resolves to London) and runs to 21:10, ten
    minutes into OFF. The run must complete rather than crash. The twenty
    warm-up bars (the ATR window filling) and the eleven OFF bars (21:00
    through 21:10) are exactly what ``snapshots_skipped`` (C2) exists to
    make visible: without it, a run skipping real hours of every day by
    design looks identical to one where the feature engine silently failed
    over the same range. Verified directly (not just asserted here): every
    bar strictly between the warm-up and 21:00 produces a real snapshot,
    confirming the only skips are the two expected, contiguous ranges.
    """

    bars = _ramp(851)

    result = _run(bars=bars, strategy=ToyStrategy(every_n=10_000))

    assert result.bars_seen == 851
    assert result.snapshots_skipped == 31  # 20 warm-up + 11 OFF (21:00-21:10)


def test_snapshots_skipped_counts_only_the_warmup_when_no_session_is_involved() -> None:
    """The plainest case, with no OFF hours anywhere in the fixture: every one
    of the first ``FIRST_SNAPSHOT_BAR`` bars produces no snapshot because the
    ATR window has not filled yet, and ``snapshots_skipped`` must count
    exactly those and nothing else -- not bars where a proposal was simply
    not made (the loop's other three ``continue`` paths all run after a
    snapshot was already built)."""

    bars = _ramp(30)

    result = _run(bars=bars, strategy=ToyStrategy(every_n=1))

    assert result.snapshots_skipped == FIRST_SNAPSHOT_BAR
    assert result.bars_seen == 30


def test_a_bar_that_hits_the_stop_and_the_deadline_together_is_charged_the_stop() -> None:
    """The engine's stop path, and the order ``_close_if_done`` claims.

    Every other test here rides a monotonically rising ramp with a BUY stop
    100 points below the reference, so the stop is unreachable by construction
    and every exit is ``ExitKind.TIME`` -- ``resolve_exit`` could return
    ``None`` unconditionally and the suite would not notice. Task 3 tests
    ``resolve_exit`` in isolation; nothing tested that the engine calls it,
    with the stop that came off the risk decision, before the time stop.

    One bar dips below that stop, and it is the deadline bar: the bar that
    could have exited either way. D-2 says it is charged the stop, so both the
    kind and the price tell the two apart -- the stop exits at 1.09920 while a
    time stop on the same bar would exit at its open less half the spread.
    """

    bars = _ramp(40)
    # decision.stop_loss_price: the proposal references bar 20's close and puts
    # its invalidation 100 points under it, the structural term is the widest
    # of section 8.2's four, and stop_price quantises onto the tick grid.
    stop = ramp_price(FIRST_SNAPSHOT_BAR) - Decimal("0.00100")
    # Entry fills on bar 21's open and HOLDING_SECONDS lands the deadline on
    # bar 32's open -- the same two bars the known-answer run above uses.
    deadline_bar = 32
    dipped = (
        *bars[:deadline_bar],
        bars[deadline_bar].model_copy(update={"low": stop - POINT}),
        *bars[deadline_bar + 1 :],
    )

    outcome = _outcome(bars=dipped, strategy=ToyStrategy(every_n=20))
    result = outcome.result

    assert [trade.exit_kind for trade in result.trades] == [ExitKind.STOP]
    assert result.trades[0].exit_price == stop
    assert result.trades[0].exit_at == bars[deadline_bar].event_time
    # Phase 8B2a: a stop crosses no spread, so the attribution carries the
    # entry's half-spread alone -- 5 points at $1 a point on 3.37 lots. The two
    # TIME exits of the known-answer run above pay twice this, and the asymmetry
    # is the model rather than a residual to be smoothed away.
    assert outcome.attribution.trades[0].spread_cost == Decimal("16.85")


SLIPPAGE_POINTS = Decimal(4)
SLIPPAGE_OFFSET = SLIPPAGE_POINTS * POINT
"""Four points a side, worth four ticks of price at this contract's
``point_size``. Chosen over the default zero because zero is the reason this
needed its own tests: with ``slippage_cost`` identically ``0`` in every other
engine test, dropping the exit leg from ``engine.py``'s sum is a mutant the
whole suite survives."""


def _slippage_legs_in_money(side: Side, lots: Decimal) -> tuple[Decimal, Decimal]:
    """What one trade's entry leg and exit leg each paid in slippage, in money.

    Both legs are priced by ``entry_fill`` -- the closing one too, on the
    opposite side, through ``Backtester._closing_fill`` -- so the two offsets
    are equal and opposite and each of them is a charge. The conversion is
    ``_price_to_money`` read off the contract rather than off a tester
    instance; at ``point_size == price_increment`` it terminates either way.
    """

    contract = _contract()
    model = _cost_model(slippage_points_per_side=SLIPPAGE_POINTS)
    closing = Side.SELL if side is Side.BUY else Side.BUY
    return (
        _money(
            abs(slippage_price_offset(model=model, side=side, contract=contract, opening=True)),
            lots,
        ),
        _money(
            abs(slippage_price_offset(model=model, side=closing, contract=contract, opening=True)),
            lots,
        ),
    )


def _money(price_delta: Decimal, lots: Decimal) -> Decimal:
    contract = _contract()
    return price_delta * lots * contract.value_per_price_increment / contract.price_increment


def test_a_run_that_charges_slippage_attributes_both_legs_of_it() -> None:
    """``slippage_cost`` is the two legs summed, on the one axis where the legs
    are not symmetric and cannot be.

    Every other engine test runs with ``slippage_points_per_side=0`` — the
    conftest default, and the only value ``_ramp``'s hand-computed prices are
    built around — so there the exit leg is a term nothing can see and a sum of
    one leg is indistinguishable
    from a sum of two. Here it is 4 points a side: 4 points x 3.37 lots at $1 a
    point is 13.48 a leg, 26.96 a trade, and the trade's own reported gross
    moves from the known-answer run's +3.37 to -23.59 by exactly that 26.96.
    The legs are asserted separately as well as summed, so a half-decomposition
    is a named failure rather than a wrong total nobody can place.
    """

    outcome = _outcome(
        bars=_ramp(60),
        strategy=ToyStrategy(every_n=20),
        cost_model=_cost_model(slippage_points_per_side=SLIPPAGE_POINTS),
    )

    # A long buys against its own slippage: up on entry, and the closing sell
    # fills lower. Both crossings are visible in the fill prices themselves.
    assert outcome.result.trades[0].entry_price == ramp_price(21) + HALF_SPREAD + SLIPPAGE_OFFSET
    assert outcome.result.trades[0].exit_price == ramp_price(32) - HALF_SPREAD - SLIPPAGE_OFFSET
    for split, trade in zip(outcome.attribution.trades, outcome.result.trades, strict=True):
        entry_leg, exit_leg = _slippage_legs_in_money(trade.side, trade.lots)
        assert (entry_leg, exit_leg) == (Decimal("13.48"), Decimal("13.48"))
        assert split.slippage_cost == entry_leg + exit_leg
    assert outcome.attribution.trades[0].slippage_cost == Decimal("26.96")
    assert outcome.result.trades[0].gross_pnl == Decimal("-23.59")


def test_a_short_is_charged_the_same_two_legs_with_the_signs_inverted() -> None:
    """§3.3's sell-side claim, which no other test in this file reaches.

    ``ToyStrategy`` only ever bought, so nothing said end to end what the design
    says of the other side: a short slips DOWN on entry and its closing buy
    slips UP, and the two signs invert while both are still charged. The ramp
    rises, so the short's ``market_pnl`` is negative and its gross is worse
    than the raw move by the same three terms a long's is --
    -37.07 - 33.70 - 26.96 = -97.73.
    """

    outcome = _outcome(
        bars=_ramp(60),
        strategy=ToyStrategy(every_n=20, side=Side.SELL),
        cost_model=_cost_model(slippage_points_per_side=SLIPPAGE_POINTS),
    )

    assert [trade.side for trade in outcome.result.trades] == [Side.SELL, Side.SELL]
    assert outcome.result.trades[0].entry_price == ramp_price(21) - HALF_SPREAD - SLIPPAGE_OFFSET
    assert outcome.result.trades[0].exit_price == ramp_price(32) + HALF_SPREAD + SLIPPAGE_OFFSET
    for split, trade in zip(outcome.attribution.trades, outcome.result.trades, strict=True):
        entry_leg, exit_leg = _slippage_legs_in_money(trade.side, trade.lots)
        assert (entry_leg, exit_leg) == (Decimal("13.48"), Decimal("13.48"))
        assert split.slippage_cost == entry_leg + exit_leg
    assert outcome.attribution.trades[0].market_pnl == Decimal("-37.07")
    assert outcome.attribution.trades[0].slippage_cost == Decimal("26.96")
    assert outcome.result.trades[0].gross_pnl == Decimal("-97.73")


def test_a_position_still_open_when_the_bars_run_out_produces_no_trade() -> None:
    """Two properties, because one strategy shows both.

    The proposal carries 7200 seconds while the strategy's own field still
    says 660. The deadline must come from the proposal -- two hours past an
    entry on bar 21, well outside a 40-bar ramp -- and not from the strategy,
    which would close on bar 33 and report a trade. They agree for a strategy
    stating one number, which is why nothing else here can tell them apart.

    And the position left open when the range ends is discarded rather than
    flushed at the last bar: an exit priced at the edge of the requested range
    is an exit the position's own rules never asked for, and crediting the
    strategy with it reports a trade the period cannot justify.
    """

    outcome = _outcome(
        bars=_ramp(40), strategy=ToyStrategy(every_n=20, proposal_holding_seconds=7200)
    )
    result = outcome.result

    assert result.trades == ()
    assert result.net_pnl == Decimal(0)
    assert result.bars_seen == 40
    # Phase 8B1: the discarded position is why the series ends not flat -- one
    # mark per processed bar, the last of them still holding. What this run
    # cannot show is that the ``is_flat`` guard is load-bearing: with no trades
    # and no realized PnL, a run that reconciled unconditionally would agree
    # with its own result all the same.
    # ``test_the_final_realized_total_is_reconciled_against_the_result_only_when_flat``
    # in ``test_mark.py`` is the test that carries that, because its closing
    # realized total disagrees with the result's net.
    assert outcome.equity.is_flat is False
    assert outcome.equity.observations[-1].open_positions == 1
    assert len(outcome.equity.observations) == outcome.result.bars_seen


def test_a_rejected_decision_is_recorded_with_its_reasons() -> None:
    """D-8. A strategy whose edge lives in trades the constitution refuses has
    no edge in this system, and a count alone would not say which trades."""

    result = _run(
        bars=_ramp(30),
        strategy=ToyStrategy(every_n=5),
        firm_equity=Decimal("1"),  # too small to size anything
    )

    assert result.trades == ()
    assert result.rejections != ()
    assert all("below_min_lot" in reasons for reasons in result.rejections)


def test_a_horizon_under_ten_bars_refuses_the_run() -> None:
    bars = _ramp(30)
    source = FakeBarReader(bars)

    with pytest.raises(BacktestRefused) as caught:
        _run(
            bars=bars,
            strategy=ToyStrategy(every_n=5, horizon_seconds=60),
            reader=source,
        )

    assert caught.value.kind is RefusalKind.HORIZON
    assert (source.coverage_calls, source.bars_calls) == (0, 0)


def test_a_defective_bar_in_the_range_refuses_the_run() -> None:
    """The ingester already flagged this bar as wrong. Trading on it anyway is
    the simulator overruling a judgement made with more information."""

    bars = _ramp(30)
    poisoned = (
        *bars[:15],
        bars[15].model_copy(update={"quality": BarQuality.OHLC_INCOHERENT}),
        *bars[16:],
    )

    with pytest.raises(BacktestRefused) as caught:
        _run(bars=poisoned, strategy=ToyStrategy(every_n=5))

    assert caught.value.kind is RefusalKind.DEFECTIVE_BAR


def test_a_defective_bar_within_tolerance_is_counted_and_skipped() -> None:
    bars = _ramp(40)
    bars = (
        *bars[:20],
        bars[20].model_copy(update={"quality": BarQuality.OHLC_INCOHERENT}),
        *bars[21:],
    )

    result = _run(bars=bars, strategy=ToyStrategy(), defective_bar_tolerance=Decimal("0.1"))

    assert result.defective_bars == 1
    assert result.bars_seen == 39


def test_a_defective_fraction_above_tolerance_still_refuses() -> None:
    """The tolerance is a declared allowance, not a way to ignore bad data."""

    bars = tuple(
        bar.model_copy(update={"quality": BarQuality.OHLC_INCOHERENT}) for bar in _ramp(40)
    )

    with pytest.raises(BacktestRefused) as caught:
        _run(bars=bars, strategy=ToyStrategy(), defective_bar_tolerance=Decimal("0.1"))

    assert caught.value.kind is RefusalKind.DEFECTIVE_BAR


def test_the_default_tolerance_is_zero_so_phase_sixs_behaviour_is_unchanged() -> None:
    """A caller who says nothing gets the strict refusal, so the tolerance
    cannot be acquired by accident."""

    bars = _ramp(40)
    bars = (
        *bars[:20],
        bars[20].model_copy(update={"quality": BarQuality.OHLC_INCOHERENT}),
        *bars[21:],
    )

    with pytest.raises(BacktestRefused):
        _run(bars=bars, strategy=ToyStrategy())


def test_a_defective_fraction_equal_to_the_tolerance_is_allowed() -> None:
    bars = _ramp(10)
    bars = (
        bars[0].model_copy(update={"quality": BarQuality.OHLC_INCOHERENT}),
        *bars[1:],
    )

    result = _run(bars=bars, strategy=ToyStrategy(), defective_bar_tolerance=Decimal("0.1"))

    assert result.defective_bars == 1
    assert result.bars_seen == 9


def test_a_fraction_just_below_exact_one_third_is_refused() -> None:
    bars = _ramp(3)
    bars = (
        bars[0].model_copy(update={"quality": BarQuality.OHLC_INCOHERENT}),
        *bars[1:],
    )

    with pytest.raises(BacktestRefused) as caught:
        _run(
            bars=bars,
            strategy=ToyStrategy(),
            defective_bar_tolerance=Decimal("0.3333333333333333333333333333"),
        )

    assert caught.value.kind is RefusalKind.DEFECTIVE_BAR


def test_run_identity_includes_the_defective_bar_tolerance() -> None:
    bars = _ramp(40)
    bars = (
        *bars[:20],
        bars[20].model_copy(update={"quality": BarQuality.OHLC_INCOHERENT}),
        *bars[21:],
    )

    low = _run(bars=bars, strategy=ToyStrategy(), defective_bar_tolerance=Decimal("0.1"))
    high = _run(bars=bars, strategy=ToyStrategy(), defective_bar_tolerance=Decimal("0.2"))

    assert low.defective_bars == high.defective_bars == 1
    assert low.run_id != high.run_id
    assert low.digest() != high.digest()


def test_result_carries_complete_run_and_constitution_provenance() -> None:
    contract = _contract()
    payload = contract.model_dump(mode="json")
    payload["supported_fills"] = sorted(payload["supported_fills"])
    expected_contract_sha256 = hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()

    result = _run(bars=_ramp(60), strategy=ToyStrategy(every_n=20), contract=contract)

    assert result.exit_policy == NoExitPolicy(kind="none")
    assert result.constitution_sha256 == _loaded_constitution().constitution_sha256
    assert result.contract_sha256 == expected_contract_sha256
    assert result.atr_period == 2
    assert result.spread_window == 10
    assert result.defective_bar_tolerance == Fraction(0)


def test_each_exit_policy_has_a_distinct_identity_even_when_trades_coincide() -> None:
    policies = (
        NoExitPolicy(kind="none"),
        FixedTargetPolicy(kind="fixed_target", r_multiple=Decimal("1000")),
        ChandelierPolicy(
            kind="chandelier", atr_multiple=Decimal("3"), min_step_points=Decimal("1000")
        ),
    )

    results = [
        _run(
            bars=_ramp(60),
            strategy=ToyStrategy(every_n=1000, target_r_multiple=Decimal("1000"), policy=policy),
        )
        for policy in policies
    ]

    assert results[0].trades
    assert all(result.trades == results[0].trades for result in results[1:])
    assert len({result.run_id for result in results}) == 3
    assert len({result.digest() for result in results}) == 3


def test_every_run_assumption_changes_run_and_result_identity() -> None:
    bars = _ramp(60)
    base = _run(bars=bars, strategy=ToyStrategy(every_n=20))
    changed = (
        _run(
            bars=bars,
            strategy=ToyStrategy(every_n=20),
            contract=_contract(freeze_distance=Decimal("0.0002")),
        ),
        _run(
            bars=bars,
            strategy=ToyStrategy(every_n=20),
            constitution_sha256="f" * 64,
        ),
        _run(bars=bars, strategy=ToyStrategy(every_n=20), atr_period=3),
        _run(bars=bars, strategy=ToyStrategy(every_n=20), spread_window=11),
        _run(
            bars=bars,
            strategy=ToyStrategy(every_n=20),
            defective_bar_tolerance=Decimal("0.01"),
        ),
    )

    for result in changed:
        assert result.run_id != base.run_id
        assert result.digest() != base.digest()


def test_equivalent_policy_and_tolerance_spellings_have_one_identity() -> None:
    bars = _ramp(60)
    first = _run(
        bars=bars,
        strategy=ToyStrategy(
            every_n=1000,
            target_r_multiple=Decimal("1.0"),
            policy=FixedTargetPolicy(kind="fixed_target", r_multiple=Decimal("1.0")),
        ),
        defective_bar_tolerance=Decimal("0.10"),
    )
    second = _run(
        bars=bars,
        strategy=ToyStrategy(
            every_n=1000,
            target_r_multiple=Decimal("1.00"),
            policy=FixedTargetPolicy(kind="fixed_target", r_multiple=Decimal("1.00")),
        ),
        defective_bar_tolerance=Decimal("0.1"),
    )

    assert first.exit_policy.model_dump_json() == second.exit_policy.model_dump_json()
    assert first.defective_bar_tolerance == second.defective_bar_tolerance == Fraction(1, 10)
    assert first.run_id == second.run_id
    assert first.digest() == second.digest()


@pytest.mark.parametrize(
    "tolerance",
    [
        Decimal("-0.1"),
        Decimal("1.1"),
        Decimal("NaN"),
        Decimal("Infinity"),
        0.1,
    ],
)
def test_a_request_refuses_a_non_finite_or_out_of_range_defective_bar_tolerance(
    tolerance: object,
) -> None:
    with pytest.raises(ValueError, match="defective_bar_tolerance"):
        _run(
            bars=_ramp(30),
            strategy=ToyStrategy(every_n=5),
            defective_bar_tolerance=tolerance,
        )


def test_a_range_outside_the_stores_coverage_refuses_the_run() -> None:
    """Simulating across data we do not have is the worst kind of silent lie:
    the equity curve simply has fewer bars than the period claims."""

    bars = _ramp(30)
    source = FakeBarReader(bars)

    with pytest.raises(BacktestRefused) as caught:
        _run(
            bars=bars,
            strategy=ToyStrategy(every_n=5),
            start=bars[0].event_time - timedelta(days=30),
            reader=source,
        )

    assert caught.value.kind is RefusalKind.COVERAGE
    assert (source.coverage_calls, source.bars_calls) == (1, 0)


def test_a_range_ending_past_the_stores_last_bar_refuses_the_run() -> None:
    """The other half of the coverage boundary, and the half that does work.

    A range reaching outside held coverage is refused before any bar is read.
    Without that boundary a too-far end would answer with fewer bars than the
    period claims, which is the short equity curve with no one to blame.
    """

    bars = _ramp(30)
    source = FakeBarReader(bars)

    with pytest.raises(BacktestRefused) as caught:
        _run(
            bars=bars,
            strategy=ToyStrategy(every_n=5),
            end=bars[-1].event_time + timedelta(days=1),
            reader=source,
        )

    assert caught.value.kind is RefusalKind.COVERAGE
    assert (source.coverage_calls, source.bars_calls) == (1, 0)


def test_an_end_before_the_start_refuses_the_run() -> None:
    """A backwards range reaches no bar in any store. The loop never runs, and
    the result would be bars_seen=0 with no trades and no refusal --
    indistinguishable from a strategy that proposed nothing. The same silent
    lie, through a different door."""

    bars = _ramp(30)
    source = FakeBarReader(bars)

    with pytest.raises(BacktestRefused) as caught:
        _run(
            bars=bars,
            strategy=ToyStrategy(every_n=5),
            start=bars[-1].event_time,
            end=bars[0].event_time,
            reader=source,
        )

    assert caught.value.kind is RefusalKind.COVERAGE
    assert (source.coverage_calls, source.bars_calls) == (0, 0)


@pytest.mark.parametrize("equity", [Decimal(0), Decimal("-1"), 100000.0])
def test_a_request_refuses_an_equity_that_is_not_positive_decimal_money(equity: object) -> None:
    """The backtester's one input boundary, and the field carrying the money.

    Zero or negative equity sizes nothing, so every proposal is rejected and
    the run reports no trades -- a strategy that cannot be sized reads exactly
    like a strategy with nothing to say. A float survives every later Decimal
    multiply as a silently inexact number. ``BacktestRequest`` is a dataclass
    because ``strategy`` is a bare Protocol, so these are the two checks a
    ``CanonicalModel``'s own annotations would have made.
    """

    with pytest.raises(ValueError, match="firm_equity"):
        _run(bars=_ramp(30), strategy=ToyStrategy(every_n=5), firm_equity=equity)


def test_a_strategy_that_peeks_is_refused() -> None:
    """The classic leak. TradeProposal is Stamped, so a proposal claiming
    availability later than the snapshot that produced it is detectable for
    free -- and a strategy that peeked must be refused, not scored."""

    with pytest.raises(BacktestRefused) as caught:
        _run(bars=_ramp(30), strategy=PeekingStrategy())

    assert caught.value.kind is RefusalKind.LOOKAHEAD


def test_the_fixed_target_arm_exits_on_its_target_end_to_end() -> None:
    """The arm Phase 6 could only describe. ``resolve_exit`` has implemented
    target fills on both sides since Phase 6 and never once executed, because
    ``RiskEngine`` returned ``None`` for every take-profit; Phase 7 gave it
    ``target_r_multiple`` to price one from, and this is the first run in which
    ``ExitKind.TARGET`` actually occurs.

    Every number is derived, not read off the simulator. The risk engine prices
    the target off the stop distance IT emitted: the structural term is the
    widest of section 8.2's four, so that distance is 0.00100 from the
    reference price ``ramp_price(20)``, and 0.05R of it is 5 points. Target
    = 1.10020 + 0.00005 = 1.10025, rounded away from entry onto the tick grid,
    which it already sits on. The ramp's bar *i* highs reach one point above
    ``ramp_price(i)``, so the first bar whose high touches 1.10025 is bar 24 --
    eight bars inside the toy's eleven-bar hold, which is what keeps the time
    stop from taking this trade instead.
    """

    bars = _ramp(60)
    result = _run(
        bars=bars,
        strategy=ToyStrategy(
            every_n=1000,
            target_r_multiple=Decimal("0.05"),
            policy=FixedTargetPolicy(kind="fixed_target", r_multiple=Decimal("0.05")),
        ),
    )

    assert [trade.exit_kind for trade in result.trades] == [ExitKind.TARGET]
    assert result.trades[0].exit_price == Decimal("1.10025")
    assert result.trades[0].exit_at == bars[24].event_time


def test_a_fixed_target_arm_refuses_a_proposal_without_the_declared_target() -> None:
    with pytest.raises(BacktestRefused) as caught:
        _run(
            bars=_ramp(40),
            strategy=ToyStrategy(
                every_n=1000,
                target_r_multiple=None,
                policy=FixedTargetPolicy(kind="fixed_target", r_multiple=Decimal("1")),
            ),
        )

    assert caught.value.kind is RefusalKind.EXIT_POLICY


def test_a_target_the_risk_engine_priced_is_ignored_unless_the_arm_names_it() -> None:
    """The A/B's integrity. One strategy is meant to run three times with three
    policies, so the natural shape is a proposal that always carries its
    ``target_r_multiple`` and a policy that says which arm this run is. If the
    loop took whatever take-profit the risk decision happened to hold, the
    baseline and chandelier arms would both quietly acquire a target and the
    comparison would be between three things that all have one.

    Same bars and the same proposals as the test above -- the risk engine
    prices the identical 1.10025 target on both runs -- and only the declared
    arm differs. This one must reach its time stop instead.
    """

    result = _run(
        bars=_ramp(60),
        strategy=ToyStrategy(
            every_n=1000,
            target_r_multiple=Decimal("0.05"),
            policy=NoExitPolicy(kind="none"),
        ),
    )

    assert [trade.exit_kind for trade in result.trades] == [ExitKind.TIME]


def test_a_chandelier_run_stops_out_at_the_level_the_previous_bar_set() -> None:
    """The trail, wired, and the bar-ordering rule that keeps it honest.

    Over the ramp the clamp binds every bar -- 3 ATRs is 6 points while the
    broker floor is 20 -- so the stop tracks exactly 20 points under each
    close. Entry is bar 21; by bar 30 the stop stands at
    ``ramp_price(30) - 0.00020`` = 1.10010, against the risk engine's original
    1.09920.

    Bar 31 is then replaced by a spike: it dips to 1.10005 and rallies to close
    at 1.10150. That single bar separates three implementations.

    * No trail: 1.10005 never reaches 1.09920, so the position survives to the
      time stop on bar 32 -- which the companion assertion below runs, on the
      same bars, to prove the difference belongs to the policy.
    * Trailing AFTER the bar's exits are resolved, which is what this engine
      does: the stop is the one bar 30 set, 1.10010, and the exit fills there.
    * Trailing BEFORE them, which the brief asked for: bar 31's own close would
      first drag the stop up to 1.10130, and the exit would fill at the bar's
      open, 1.10031 -- 21 points better, on a bar whose low the market may well
      have reached first. A bar cannot order its own extremes; that is the same
      fact D-2 exists to respect, and reading it the flattering way is the
      defect this assertion pins.
    """

    bars = _ramp(40)
    spike = bars[31].model_copy(
        update={"high": Decimal("1.10200"), "low": Decimal("1.10005"), "close": Decimal("1.10150")}
    )
    bars = (*bars[:31], spike, *bars[32:])

    trailed = _run(
        bars=bars,
        strategy=ToyStrategy(
            every_n=1000,
            policy=ChandelierPolicy(
                kind="chandelier", atr_multiple=Decimal(3), min_step_points=Decimal(1)
            ),
        ),
    )
    baseline = _run(bars=bars, strategy=ToyStrategy(every_n=1000))

    assert [trade.exit_kind for trade in trailed.trades] == [ExitKind.STOP]
    assert trailed.trades[0].exit_at == spike.event_time
    assert trailed.trades[0].exit_price == Decimal("1.10010")
    # The same bars, the same proposals, the baseline arm: no stop is reached
    # and the position runs to its deadline on bar 32.
    assert [trade.exit_kind for trade in baseline.trades] == [ExitKind.TIME]
    assert baseline.trades[0].exit_at == bars[32].event_time


@pytest.mark.parametrize("field_name", ["start", "end"])
def test_a_request_refuses_a_naive_start_or_end(field_name: str) -> None:
    """The boundary's third exception shape, closed.

    ``BacktestRequest`` stands in for the annotations a ``CanonicalModel``
    would have carried, and a canonical model would have enforced
    UTC-awareness -- which is why ``ReplayClock.__post_init__`` calls
    ``ensure_utc`` twenty lines up. Without it a naive stamp survived
    construction and raised ``TypeError`` from inside
    ``_refuse_outside_coverage``'s comparison against an aware ``Coverage``
    stamp: past every handler, so an operator got "unexpected failure" and a
    correlation id for a missing timezone. ``cli.py``'s ``_as_utc`` saved the
    CLI path only; an in-process caller had nothing.
    """

    bars = _ramp(60)
    naive = {field_name: bars[0].event_time.replace(tzinfo=None)}

    with pytest.raises(TimestampError):
        _run(bars=bars, strategy=ToyStrategy(every_n=20), **naive)  # type: ignore[arg-type]


def test_a_request_normalises_an_offset_aware_range_to_utc() -> None:
    """Checked *and* normalised, as ``FixedClock`` does. ``run_id`` is built
    from ``start.isoformat()`` and the digest is built from the result, so two
    requests naming the same instant in different offsets must not produce two
    different digests."""

    bars = _ramp(60)
    plus_two = timezone(timedelta(hours=2))

    utc = _run(bars=bars, strategy=ToyStrategy(every_n=20))
    offset = _run(
        bars=bars,
        strategy=ToyStrategy(every_n=20),
        start=bars[0].event_time.astimezone(plus_two),
        end=bars[-1].event_time.astimezone(plus_two),
    )

    assert offset.run_id == utc.run_id
    assert offset.digest() == utc.digest()


def test_a_non_positive_chandelier_level_refuses_the_run() -> None:
    with pytest.raises(BacktestRefused) as caught:
        trail_candidate(
            side=Side.BUY,
            current_stop=Decimal("1.09900"),
            bar=_bar(high=Decimal("1.10500"), close=Decimal("1.10000")),
            atr=Decimal("0.00010"),
            contract=_contract(min_stop_distance=Decimal("2")),
            policy=ChandelierPolicy(
                kind="chandelier", atr_multiple=Decimal(3), min_step_points=Decimal(1)
            ),
        )

    assert caught.value.kind is RefusalKind.EXIT_POLICY


def test_a_positive_sub_tick_chandelier_level_that_quantizes_to_zero_refuses_the_run() -> None:
    with pytest.raises(BacktestRefused) as caught:
        trail_candidate(
            side=Side.BUY,
            current_stop=Decimal("0.00001"),
            bar=_bar(
                high=Decimal("0.00002"),
                low=Decimal("0.000004"),
                close=Decimal("0.000005"),
            ),
            atr=Decimal("0.000001"),
            contract=_contract(
                min_stop_distance=Decimal("0.000004"),
                freeze_distance=Decimal("0.000004"),
            ),
            policy=ChandelierPolicy(
                kind="chandelier", atr_multiple=Decimal(1), min_step_points=Decimal(1)
            ),
        )

    assert caught.value.kind is RefusalKind.EXIT_POLICY


def test_a_chandelier_stop_only_ever_moves_toward_profit() -> None:
    """I-8. A falling high must not drag a long's stop back down."""

    policy = ChandelierPolicy(
        kind="chandelier", atr_multiple=Decimal(3), min_step_points=Decimal(1)
    )
    high = _bar(high=Decimal("1.10500"))
    low = _bar(high=Decimal("1.10100"))

    raised = trail_candidate(
        side=Side.BUY,
        current_stop=Decimal("1.09900"),
        bar=high,
        atr=Decimal("0.00010"),
        contract=_contract(),
        policy=policy,
    )
    assert raised is not None
    assert raised > Decimal("1.09900")

    assert (
        trail_candidate(
            side=Side.BUY,
            current_stop=raised,
            bar=low,
            atr=Decimal("0.00010"),
            contract=_contract(),
            policy=policy,
        )
        is None
    )


def test_a_candidate_inside_the_minimum_step_is_ignored() -> None:
    """Without hysteresis the stop is re-modified on every bar, which in live
    trading is a request per tick to the broker.

    ``current_stop`` is placed so that the candidate genuinely IMPROVES on it,
    by 2 points against a 100-point step. Only hysteresis can refuse this one:
    the monotonic check would have let it through, which the second half
    demonstrates by asking the same question with a one-point step and getting
    a candidate back. Without that half the ``None`` would prove nothing --
    every rule in the function returns ``None``.
    """

    def ask(step: Decimal) -> Decimal | None:
        return trail_candidate(
            side=Side.BUY,
            current_stop=Decimal("1.10450"),
            bar=_bar(high=Decimal("1.10500")),
            atr=Decimal("0.00010"),
            contract=_contract(),
            policy=ChandelierPolicy(
                kind="chandelier", atr_multiple=Decimal(3), min_step_points=step
            ),
        )

    assert ask(Decimal(100)) is None
    assert ask(Decimal(1)) == Decimal("1.10470")


def test_a_candidate_closer_than_the_brokers_minimum_distance_is_clamped() -> None:
    """A stop inside the freeze distance is rejected by MT5 outright, so the
    simulator must not produce one or it reports fills the venue would refuse.

    The ATR multiple is small enough that the raw Chandelier level sits one
    tenth of a point under the close -- far inside the floor -- so the clamp
    is the only thing that can put the returned stop outside it.
    """

    policy = ChandelierPolicy(
        kind="chandelier", atr_multiple=Decimal("0.01"), min_step_points=Decimal(1)
    )
    bar = _bar(high=Decimal("1.10500"), close=Decimal("1.10500"))
    contract = _contract()

    candidate = trail_candidate(
        side=Side.BUY,
        current_stop=Decimal("1.09900"),
        bar=bar,
        atr=Decimal("0.00010"),
        contract=contract,
        policy=policy,
    )

    assert candidate is not None
    assert bar.close - candidate >= max(contract.min_stop_distance, contract.freeze_distance)


# --- Phase 8B3: scenario identity in the run id, and the compounding arm -----

_PRE_CHANGE_RUN_ID = "81300ddd2be308e006ab418e38565fb81b0b78313edc6ed408baa635d759cd0e"
_PRE_CHANGE_DIGEST = "91a237e8fd17fb3761a8c87cb45021f87a2eea65066466ffc59d8bdb97e8bcac"
_PRE_CHANGE_OUTCOME_SHA256 = "0393c5ab2b317348b3b03dbee8177e04378f1e0845cf77f7e5104586f5703676"

_EQUITY = Decimal("1000000")
"""Large enough that the lot grid (0.01) is finer than one trade's effect on
equity: 33.75 lots unrounded, and a 40-point winner moves equity by ~0.11%."""


def _long_hold(*, side: Side = Side.BUY) -> ToyStrategy:
    """Re-enters the bar after every exit, holding 30 bars. On the rising ramp a
    long wins (~40 points against a 10-point spread) and a short loses."""

    return ToyStrategy(every_n=1, max_holding_seconds=1800, side=side)


def test_a_constant_notional_run_keeps_its_pre_change_identity() -> None:
    outcome = _outcome(bars=_ramp(60), strategy=ToyStrategy(every_n=20))

    assert outcome.result.run_id == _PRE_CHANGE_RUN_ID
    assert outcome.result.digest() == _PRE_CHANGE_DIGEST
    # ``sizing`` is excluded from a constant outcome's bytes, so nothing that
    # serialised this type before has moved.
    assert "sizing" not in outcome.model_dump(mode="json")
    assert hashlib.sha256(outcome.model_dump_json().encode()).hexdigest() == (
        _PRE_CHANGE_OUTCOME_SHA256
    )


def test_a_stressed_run_has_its_own_id_and_spellings_of_a_multiplier_share_one() -> None:
    bars = _ramp(60)
    one = _run_with_cost(bars, Decimal(1))
    stressed = _run_with_cost(bars, Decimal("1.5"))

    assert one.run_id == _PRE_CHANGE_RUN_ID
    assert stressed.run_id != one.run_id
    assert stressed.digest() != one.digest()
    assert (
        _run_with_cost(bars, Decimal("1.50")).run_id == _run_with_cost(bars, Decimal("1.5")).run_id
    )
    assert _run_with_cost(bars, Decimal("1.0")).run_id == one.run_id


def _run_with_cost(bars: tuple[Bar, ...], multiplier: Decimal) -> BacktestResult:
    return _run(
        bars=bars,
        strategy=ToyStrategy(every_n=20),
        cost_model=_cost_model(stress_multiplier=multiplier),
    )


def test_the_first_position_is_identical_in_both_sizing_modes() -> None:
    bars = _ramp(90)
    constant = _outcome(bars=bars, strategy=_long_hold(), firm_equity=_EQUITY)
    compounding = _outcome(
        bars=bars, strategy=_long_hold(), firm_equity=_EQUITY, sizing=SizingMode.COMPOUNDING
    )

    assert len(constant.result.trades) >= 2
    assert constant.result.trades[0] == compounding.result.trades[0]
    assert constant.result.trades[0].net_pnl > 0


def test_a_compounding_run_has_a_different_id_from_the_constant_run_of_one_request() -> None:
    bars = _ramp(90)
    constant = _outcome(bars=bars, strategy=_long_hold(), firm_equity=_EQUITY)
    compounding = _outcome(
        bars=bars, strategy=_long_hold(), firm_equity=_EQUITY, sizing=SizingMode.COMPOUNDING
    )

    assert compounding.sizing is SizingMode.COMPOUNDING
    assert constant.sizing is SizingMode.CONSTANT_NOTIONAL
    assert compounding.result.run_id != constant.result.run_id
    assert compounding.result.digest() != constant.result.digest()
    assert compounding.model_dump(mode="json")["sizing"] == "compounding"


def test_a_win_grows_the_next_lot_and_a_loss_shrinks_it_at_the_equity_the_decision_saw() -> None:
    bars = _ramp(90)
    won = _outcome(
        bars=bars, strategy=_long_hold(), firm_equity=_EQUITY, sizing=SizingMode.COMPOUNDING
    ).result
    lost = _outcome(
        bars=bars,
        strategy=_long_hold(side=Side.SELL),
        firm_equity=_EQUITY,
        sizing=SizingMode.COMPOUNDING,
    ).result

    assert won.trades[0].net_pnl > 0 > lost.trades[0].net_pnl
    assert won.trades[1].lots > won.trades[0].lots
    assert lost.trades[1].lots < lost.trades[0].lots
    # The same sizing path, not a second opinion: a constant-notional run whose
    # capital IS the equity after the first trade sizes its first trade the way
    # compounding sized the second.
    for result in (won, lost):
        at_decision = _outcome(
            bars=bars,
            strategy=_long_hold(side=result.trades[0].side),
            firm_equity=_EQUITY + result.trades[0].net_pnl,
        ).result
        assert at_decision.trades[0].lots == result.trades[1].lots


def test_constant_notional_never_changes_its_lots() -> None:
    result = _run(bars=_ramp(90), strategy=_long_hold(), firm_equity=_EQUITY)

    assert len({trade.lots for trade in result.trades}) == 1


def test_a_non_positive_sizing_equity_rejects_without_reaching_the_risk_engine() -> None:
    calls: list[Decimal] = []

    class SpyRisk(RiskEngine):
        def evaluate_for_execution(self, proposal, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs["firm_equity"])
            return super().evaluate_for_execution(proposal, **kwargs)

    def spy(clock: ReplayClock) -> RiskEngine:
        return SpyRisk(_constitution(), clock)

    # A commission large enough that one round trip costs more than the account.
    ruinous = _cost_model(commission_per_lot_per_side=Decimal("100000"))
    bars = _ramp(90)

    compounding = _outcome(
        bars=bars,
        strategy=_long_hold(),
        firm_equity=_EQUITY,
        cost_model=ruinous,
        sizing=SizingMode.COMPOUNDING,
        risk_factory=spy,
    )
    calls_compounding = list(calls)
    calls.clear()
    constant = _outcome(
        bars=bars,
        strategy=_long_hold(),
        firm_equity=_EQUITY,
        cost_model=ruinous,
        risk_factory=spy,
    )

    assert len(compounding.result.trades) == 1
    assert compounding.result.trades[0].net_pnl < -_EQUITY
    assert ("equity_exhausted",) in compounding.result.rejections
    assert calls_compounding == [_EQUITY]  # the one decision taken while solvent
    assert all(equity > 0 for equity in calls_compounding)
    # The same run at constant notional is never exhausted, and keeps asking.
    assert ("equity_exhausted",) not in constant.result.rejections
    assert len(constant.result.trades) >= 2
    assert len(calls) > 1
    assert set(calls) == {_EQUITY}
