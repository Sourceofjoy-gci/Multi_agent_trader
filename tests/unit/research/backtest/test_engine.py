from datetime import timedelta, timezone
from decimal import Decimal

import pytest

from tests.unit.research.backtest.conftest import (
    FIRST_SNAPSHOT_BAR,
    HALF_SPREAD,
    ORIGIN,
    POINT,
    PeekingStrategy,
    ToyStrategy,
    _bar,
    _contract,
    _ramp,
    _run,
    ramp_price,
)
from trading_house.core.errors import TimestampError
from trading_house.core.schemas import Side
from trading_house.marketdata.models import BarQuality, Timeframe, duration
from trading_house.research.backtest.engine import (
    BacktestRefused,
    trail_candidate,
)
from trading_house.research.backtest.fills import ExitKind
from trading_house.research.backtest.result import RefusalKind
from trading_house.research.backtest.strategy import (
    ChandelierPolicy,
    FixedTargetPolicy,
    NoExitPolicy,
)


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

    result = _run(bars=_ramp(60), strategy=ToyStrategy(every_n=20))

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

    result = _run(bars=dipped, strategy=ToyStrategy(every_n=20))

    assert [trade.exit_kind for trade in result.trades] == [ExitKind.STOP]
    assert result.trades[0].exit_price == stop
    assert result.trades[0].exit_at == bars[deadline_bar].event_time


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

    result = _run(bars=_ramp(40), strategy=ToyStrategy(every_n=20, proposal_holding_seconds=7200))

    assert result.trades == ()
    assert result.net_pnl == Decimal(0)
    assert result.bars_seen == 40


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
    with pytest.raises(BacktestRefused) as caught:
        _run(bars=_ramp(30), strategy=ToyStrategy(every_n=5, horizon_seconds=60))

    assert caught.value.kind is RefusalKind.HORIZON


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


def test_a_range_outside_the_stores_coverage_refuses_the_run() -> None:
    """Simulating across data we do not have is the worst kind of silent lie:
    the equity curve simply has fewer bars than the period claims."""

    with pytest.raises(BacktestRefused) as caught:
        _run(
            bars=_ramp(30),
            strategy=ToyStrategy(every_n=5),
            start=_ramp(30)[0].event_time - timedelta(days=30),
        )

    assert caught.value.kind is RefusalKind.COVERAGE


def test_a_range_ending_past_the_stores_last_bar_refuses_the_run() -> None:
    """The other half of the coverage boundary, and the half that does work.

    A start reaching further back than anything held is refused twice over:
    the store raises ``CoverageError`` on it anyway. A too-far end is not --
    the store answers with fewer bars than the period claims and says nothing,
    which is the short equity curve with no one to blame.
    """

    bars = _ramp(30)

    with pytest.raises(BacktestRefused) as caught:
        _run(
            bars=bars,
            strategy=ToyStrategy(every_n=5),
            end=bars[-1].event_time + timedelta(days=1),
        )

    assert caught.value.kind is RefusalKind.COVERAGE


def test_an_end_before_the_start_refuses_the_run() -> None:
    """A backwards range reaches no bar in any store. Both coverage
    comparisons pass, the loop never runs, and the result is bars_seen=0 with
    no trades and no refusal -- indistinguishable from a strategy that proposed
    nothing. The same silent lie, through a different door."""

    bars = _ramp(30)

    with pytest.raises(BacktestRefused) as caught:
        _run(
            bars=bars,
            strategy=ToyStrategy(every_n=5),
            start=bars[-1].event_time,
            end=bars[0].event_time,
        )

    assert caught.value.kind is RefusalKind.COVERAGE


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
