from datetime import timedelta
from decimal import Decimal

import pytest

from tests.unit.research.backtest.conftest import (
    HALF_SPREAD,
    PeekingStrategy,
    ToyStrategy,
    _expected_net,
    _ramp,
    _run,
    ramp_price,
)
from trading_house.marketdata.models import BarQuality, Timeframe, duration
from trading_house.research.backtest.engine import BacktestRefused
from trading_house.research.backtest.fills import ExitKind
from trading_house.research.backtest.result import RefusalKind


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
    660-second deadline, which lands on the open of bar 33 and bar 53.

    Every expectation is derived from the ramp's closed form and the fill rule,
    never from the simulator:

        entry = ramp_price(21) + half spread,  exit = ramp_price(33) - half
        stop distance = 0.00100 (the structural term is the widest of 8.2's
            four), so 100 ticks at $1 a tick a lot
        book equity = 100000 x 0.45, risked at 0.75% = $337.50
        lots = 337.50 / 100 = 3.375, floored to the 0.01 grid = 3.37
        gross = 2 ticks x 3.37 = $6.74 a trade
        commission = 2 sides x 3.37 lots x $3.50 = $23.59 a trade
        swap = 0 (opened and closed the same calendar day)
        net = 6.74 - 23.59 = -$16.85 a trade, -$33.70 over the run
    """

    result = _run(bars=_ramp(60), strategy=ToyStrategy(every_n=20))

    assert [trade.exit_kind for trade in result.trades] == [ExitKind.TIME, ExitKind.TIME]
    assert result.trades[0].entry_price == ramp_price(21) + HALF_SPREAD
    assert result.trades[0].exit_price == ramp_price(33) - HALF_SPREAD
    assert result.trades[1].entry_price == ramp_price(41) + HALF_SPREAD
    assert result.trades[1].exit_price == ramp_price(53) - HALF_SPREAD
    # D-3: the lot size comes off the risk engine's decision. Sized against
    # firm equity instead of the book's 0.45 slice it would be 7.50 lots.
    assert [trade.lots for trade in result.trades] == [Decimal("3.37"), Decimal("3.37")]
    assert result.trades[0].gross_pnl == Decimal("6.74")
    assert result.trades[0].commission == Decimal("23.59")
    assert result.trades[0].swap == Decimal(0)
    assert result.net_pnl == Decimal("-33.70")
    assert result.net_pnl == _expected_net(result)
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


def test_the_strategy_never_sees_a_bar_that_had_not_closed() -> None:
    """Point-in-time discipline, checked on the snapshots the strategy actually
    received rather than on the store's filtering. Bar.availability_time >
    event_time is a schema invariant; this asserts the simulator honours it."""

    strategy = ToyStrategy(every_n=5)
    _run(bars=_ramp(30), strategy=strategy)

    assert strategy.seen != []
    for snapshot in strategy.seen:
        assert snapshot.as_of == snapshot.bar.availability_time
        assert snapshot.bar.event_time < snapshot.as_of


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


def test_a_strategy_that_peeks_is_refused() -> None:
    """The classic leak. TradeProposal is Stamped, so a proposal claiming
    availability later than the snapshot that produced it is detectable for
    free -- and a strategy that peeked must be refused, not scored."""

    with pytest.raises(BacktestRefused) as caught:
        _run(bars=_ramp(30), strategy=PeekingStrategy())

    assert caught.value.kind is RefusalKind.LOOKAHEAD
