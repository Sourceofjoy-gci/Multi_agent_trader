from datetime import UTC, datetime
from decimal import Decimal

from trading_house.core.instruments import FillPolicy, FinancingModel, InstrumentContract
from trading_house.core.schemas import Side
from trading_house.core.values import AssetClass
from trading_house.marketdata.models import Bar, BarQuality, Timeframe, duration
from trading_house.research.backtest.costs import CostModel
from trading_house.research.backtest.fills import ExitKind, entry_fill, resolve_exit

NOW = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
_TEN_POINTS = Decimal(10) * Decimal("0.00001")


def _bar(
    *,
    open: Decimal,
    high: Decimal | None = None,
    low: Decimal | None = None,
    close: Decimal | None = None,
    spread: int = 10,
    quality: BarQuality = BarQuality.OK,
) -> Bar:
    return Bar(
        instrument_id="fx.eurusd",
        timeframe=Timeframe.M1,
        event_time=NOW,
        availability_time=NOW + duration(Timeframe.M1),
        open=open,
        high=high if high is not None else open + _TEN_POINTS,
        low=low if low is not None else open - _TEN_POINTS,
        close=close if close is not None else open,
        tick_volume=0,
        spread=spread,
        real_volume=0,
        quality=quality,
    )


def _contract(**overrides: object) -> InstrumentContract:
    defaults: dict[str, object] = {
        "instrument_id": "fx.eurusd",
        "asset_class": AssetClass.FX,
        "base_currency": "EUR",
        "quote_currency": "USD",
        "price_increment": Decimal("0.00001"),
        "point_size": Decimal("0.00001"),
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


def _zero_slip(**overrides: object) -> CostModel:
    defaults: dict[str, object] = {
        "commission_per_lot_per_side": Decimal("0"),
        "slippage_points_per_side": Decimal("0"),
        "swap_long_points_per_day": Decimal("0"),
        "swap_short_points_per_day": Decimal("0"),
        "triple_swap_weekday": 2,
    }
    return CostModel(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_an_entry_fills_at_the_next_bars_open_plus_half_the_spread() -> None:
    """Section 11.1's named violation is executing at the same close that
    generated the signal. The bar passed here is already the NEXT one; the
    spread is that bar's own, converted through point_size."""

    bar = _bar(open=Decimal("1.10000"), spread=20)
    fill = entry_fill(bar=bar, side=Side.BUY, contract=_contract(), model=_zero_slip())

    assert fill.price == Decimal("1.10010")  # half of 20 points at 0.00001
    assert fill.at == bar.availability_time


def test_a_stop_gapped_through_fills_at_the_open_not_the_stop() -> None:
    """The single most common lie in bar backtesting. Price gapped past the
    stop overnight; filling AT the stop invents liquidity that never existed,
    and it flatters exactly the trades that hurt most."""

    bar = _bar(open=Decimal("1.09000"), high=Decimal("1.09100"), low=Decimal("1.08900"))
    exit_ = resolve_exit(
        bar=bar,
        side=Side.BUY,
        stop=Decimal("1.09700"),
        target=None,
        contract=_contract(),
        model=_zero_slip(),
    )

    assert exit_ is not None
    assert exit_.kind is ExitKind.STOP
    assert exit_.fill.price == Decimal("1.09000")  # the open, NOT 1.09700


def test_a_stop_touched_within_the_bar_fills_at_the_stop() -> None:
    """The ordinary case: the bar opened above the stop and traded down through
    it, so the stop is the honest fill."""

    bar = _bar(open=Decimal("1.10000"), high=Decimal("1.10100"), low=Decimal("1.09650"))
    exit_ = resolve_exit(
        bar=bar,
        side=Side.BUY,
        stop=Decimal("1.09700"),
        target=None,
        contract=_contract(),
        model=_zero_slip(),
    )

    assert exit_ is not None
    assert exit_.fill.price == Decimal("1.09700")


def test_a_target_gapped_past_fills_at_the_target_never_better() -> None:
    """D-6's other half. A favourable gap is where an optimistic simulator
    hands the strategy money the market never offered."""

    bar = _bar(open=Decimal("1.11000"), high=Decimal("1.11200"), low=Decimal("1.10900"))
    exit_ = resolve_exit(
        bar=bar,
        side=Side.BUY,
        stop=Decimal("1.09700"),
        target=Decimal("1.10500"),
        contract=_contract(),
        model=_zero_slip(),
    )

    assert exit_ is not None
    assert exit_.kind is ExitKind.TARGET
    assert exit_.fill.price == Decimal("1.10500")  # the target, NOT 1.11000


def test_a_bar_touching_both_resolves_to_the_stop() -> None:
    """D-2. The bar cannot say which came first. Assuming the target lets a
    losing strategy look profitable indefinitely; assuming the stop costs some
    genuine winners, which is the recoverable direction to be wrong in."""

    bar = _bar(open=Decimal("1.10000"), high=Decimal("1.10600"), low=Decimal("1.09650"))
    exit_ = resolve_exit(
        bar=bar,
        side=Side.BUY,
        stop=Decimal("1.09700"),
        target=Decimal("1.10500"),
        contract=_contract(),
        model=_zero_slip(),
    )

    assert exit_ is not None
    assert exit_.kind is ExitKind.STOP


def test_a_bar_touching_neither_leaves_the_position_open() -> None:
    bar = _bar(open=Decimal("1.10000"), high=Decimal("1.10100"), low=Decimal("1.09900"))

    assert (
        resolve_exit(
            bar=bar,
            side=Side.BUY,
            stop=Decimal("1.09700"),
            target=Decimal("1.10500"),
            contract=_contract(),
            model=_zero_slip(),
        )
        is None
    )


def test_the_same_rules_hold_mirrored_for_a_sell() -> None:
    """Every price comparison in this module has a side. A sign error shows up
    only on the side nobody tested, and a strategy that only goes long would
    never reveal it."""

    bar = _bar(open=Decimal("1.11000"), high=Decimal("1.11100"), low=Decimal("1.10900"))
    exit_ = resolve_exit(
        bar=bar,
        side=Side.SELL,
        stop=Decimal("1.10300"),
        target=None,
        contract=_contract(),
        model=_zero_slip(),
    )

    assert exit_ is not None
    assert exit_.kind is ExitKind.STOP
    assert exit_.fill.price == Decimal("1.11000")  # gapped up through a sell's stop
