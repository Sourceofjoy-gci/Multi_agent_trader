from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_house.core.exits import FixedTargetPolicy
from trading_house.core.schemas import Side
from trading_house.core.snapshot import BollingerFeatures, FeatureBlock, FeatureSnapshot
from trading_house.features.sessions import session_of
from trading_house.marketdata.models import Bar, BarQuality, Timeframe, duration
from trading_house.strategies.impl.vol_breakout import VOL_BREAKOUT_ID, VolBreakout

LONDON_BAR = datetime(2026, 9, 16, 14, 0, tzinfo=UTC)
ROLLOVER_BAR = datetime(2026, 9, 16, 22, 0, tzinfo=UTC)


def _block(**overrides: object) -> BollingerFeatures:
    """A block for a clean long breakout at close 1.10700: above the upper
    band, previous close inside its band, above the trend, squeeze one bar ago."""

    values: dict[str, object] = {
        "middle": Decimal("1.10415"),
        "upper": Decimal("1.10546"),
        "lower": Decimal("1.10284"),
        "bandwidth": Decimal("0.0024"),
        "previous_close": Decimal("1.10405"),
        "previous_upper": Decimal("1.10410"),
        "previous_lower": Decimal("1.10390"),
        "bars_since_squeeze": 1,
        "sma_200": Decimal("1.10260"),
    }
    values.update(overrides)
    return BollingerFeatures(**values)  # type: ignore[arg-type]


def _snapshot(
    *,
    close: Decimal = Decimal("1.10700"),
    block: BollingerFeatures | None = None,
    event_time: datetime = LONDON_BAR,
    timeframe: Timeframe = Timeframe.H1,
    instrument_id: str = "fx.eurusd",
    with_block: bool = True,
) -> FeatureSnapshot:
    as_of = event_time + duration(timeframe)
    bar = Bar(
        instrument_id=instrument_id,
        timeframe=timeframe,
        event_time=event_time,
        availability_time=as_of,
        open=close,
        high=close + Decimal("0.0001"),
        low=close - Decimal("0.0001"),
        close=close,
        tick_volume=100,
        spread=10,
        real_volume=0,
        quality=BarQuality.OK,
    )
    return FeatureSnapshot(
        as_of=as_of,
        instrument_id=instrument_id,
        timeframe=timeframe,
        bar=bar,
        atr=Decimal("0.00040"),
        median_spread_points=Decimal(10),
        tick_spread_points=Decimal(10),
        tick_time=as_of,
        session=session_of(event_time),
        prior_session_return=None,
        session_open_price=close,
        bars_since_session_open=0,
        bollinger=(block if block is not None else _block()) if with_block else None,
    )


def _short_block(**overrides: object) -> BollingerFeatures:
    values: dict[str, object] = {
        "middle": Decimal("1.10385"),
        "upper": Decimal("1.10516"),
        "lower": Decimal("1.10254"),
        "previous_close": Decimal("1.10395"),
        "previous_upper": Decimal("1.10410"),
        "previous_lower": Decimal("1.10390"),
        "sma_200": Decimal("1.10540"),
    }
    values.update(overrides)
    return _block(**values)


def test_it_declares_the_bollinger_block() -> None:
    assert VolBreakout().required_features == frozenset({FeatureBlock.BOLLINGER})


def test_a_fresh_cross_above_the_upper_band_in_an_uptrend_goes_long() -> None:
    proposal = VolBreakout().evaluate(_snapshot())

    assert proposal is not None
    assert proposal.side is Side.BUY
    assert proposal.strategy_id == VOL_BREAKOUT_ID
    assert proposal.book == "fx_swing"
    assert proposal.entry_price_ref == Decimal("1.10700")
    assert proposal.invalidation_price == Decimal("1.10415")
    assert proposal.max_holding_seconds == 432_000
    assert proposal.horizon_seconds == 432_000


def test_a_fresh_cross_below_the_lower_band_in_a_downtrend_goes_short() -> None:
    proposal = VolBreakout().evaluate(_snapshot(close=Decimal("1.10100"), block=_short_block()))

    assert proposal is not None
    assert proposal.side is Side.SELL
    assert proposal.invalidation_price == Decimal("1.10385")


@pytest.mark.parametrize(
    ("label", "close", "block"),
    [
        (
            "continuation, previous close already above",
            None,
            _block(previous_close=Decimal("1.10420")),
        ),
        ("long against the trend", None, _block(sma_200=Decimal("1.10800"))),
        ("no squeeze in the last ten bars", None, _block(bars_since_squeeze=None)),
        ("close inside the band", Decimal("1.10500"), None),
        ("short against the trend", Decimal("1.10100"), _short_block(sma_200=Decimal("1.10000"))),
        ("short continuation", Decimal("1.10100"), _short_block(previous_close=Decimal("1.10380"))),
    ],
)
def test_no_entry(label: str, close: Decimal | None, block: BollingerFeatures | None) -> None:
    snapshot = _snapshot(
        close=close if close is not None else Decimal("1.10700"),
        block=block,
    )
    assert VolBreakout().evaluate(snapshot) is None, label


def test_no_entry_in_the_rollover_window() -> None:
    assert VolBreakout().evaluate(_snapshot(event_time=ROLLOVER_BAR)) is None


def test_no_entry_without_the_block() -> None:
    assert VolBreakout().evaluate(_snapshot(with_block=False)) is None


@pytest.mark.parametrize(
    ("instrument_id", "timeframe"),
    [("fx.gbpusd", Timeframe.H1), ("fx.eurusd", Timeframe.M15)],
)
def test_no_entry_outside_eurusd_h1(instrument_id: str, timeframe: Timeframe) -> None:
    snapshot = _snapshot(instrument_id=instrument_id, timeframe=timeframe)
    assert VolBreakout().evaluate(snapshot) is None


def test_the_fixed_target_arm_stamps_its_multiple() -> None:
    policy = FixedTargetPolicy(kind="fixed_target", r_multiple=Decimal("2.0"))
    proposal = VolBreakout(policy).evaluate(_snapshot())

    assert proposal is not None
    assert proposal.target_r_multiple == Decimal("2.0")


def test_the_same_snapshot_gives_the_same_proposal() -> None:
    """§10.2: ``evaluate`` is a pure function of the snapshot."""

    snapshot = _snapshot()
    assert VolBreakout().evaluate(snapshot) == VolBreakout().evaluate(snapshot)
