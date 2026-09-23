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
    event_time: datetime = NOW,
) -> Bar:
    return Bar(
        instrument_id="fx.eurusd",
        timeframe=Timeframe.M1,
        event_time=event_time,
        availability_time=event_time + duration(Timeframe.M1),
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


def test_a_fill_is_stamped_at_the_instant_of_its_price() -> None:
    """``at`` is when the fill happened, not when the bar became readable.

    ``entry_fill`` prices at the bar's OPEN, so stamping availability_time --
    one whole bar later -- misreports the fill instant, and the engine's
    deadline arithmetic inherits the error as a hold one bar longer than the
    strategy declared.
    """

    bar = _bar(event_time=datetime(2026, 9, 21, 9, 0, tzinfo=UTC), open=Decimal("1.10000"))

    fill = entry_fill(bar=bar, side=Side.BUY, contract=_contract(), model=_zero_slip())

    assert fill.at == bar.event_time
    assert fill.at != bar.availability_time


def test_an_entry_fills_at_the_next_bars_open_plus_half_the_spread() -> None:
    """Section 11.1's named violation is executing at the same close that
    generated the signal. The bar passed here is already the NEXT one; the
    spread is that bar's own, converted through point_size."""

    bar = _bar(open=Decimal("1.10000"), spread=20)
    fill = entry_fill(bar=bar, side=Side.BUY, contract=_contract(), model=_zero_slip())

    assert fill.price == Decimal("1.10010")  # half of 20 points at 0.00001
    assert fill.at == bar.event_time


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


def test_a_sell_target_gapped_past_fills_at_the_target_never_better() -> None:
    """Mirrors case 3 for a SELL. A bar that gaps below a short's take-profit
    must fill at the target, never at the more favourable bar.open or bar.low
    -- D-6's "never better" half, unprotected on the short side until now."""

    bar = _bar(open=Decimal("1.10000"), high=Decimal("1.10100"), low=Decimal("1.09900"))
    exit_ = resolve_exit(
        bar=bar,
        side=Side.SELL,
        stop=Decimal("1.11700"),
        target=Decimal("1.10500"),
        contract=_contract(),
        model=_zero_slip(),
    )

    assert exit_ is not None
    assert exit_.kind is ExitKind.TARGET
    assert exit_.fill.price == Decimal("1.10500")  # the target, NOT 1.10000 or 1.09900
    assert exit_.fill.at == bar.event_time


_FOUR_POINTS = Decimal(4) * Decimal("0.00001")
"""What ``slippage_points_per_side=4`` is worth as a price distance, at the
``point_size`` every contract in this module uses."""


def test_an_entry_fill_pays_slippage_on_top_of_the_half_spread() -> None:
    """``slippage_price_offset``'s four signs are pinned in ``test_costs.py``,
    but nothing until now put a non-zero slippage through ``entry_fill``
    itself: every other call in this suite states zero. Delete the offset from
    this fill site and the half-spread assertions above still pass, because
    they were computed with slippage at zero.

    Hand-derived: a buy pays half the 20-point spread up (0.00010) and then
    slips 4 more points up (0.00004); a sell crosses the same half-spread down
    and slips 4 points further down. Both move against the trade.
    """

    bar = _bar(open=Decimal("1.10000"), spread=20)
    slipping = _zero_slip(slippage_points_per_side=Decimal(4))
    half_spread = Decimal("0.00010")

    buy = entry_fill(bar=bar, side=Side.BUY, contract=_contract(), model=slipping)
    sell = entry_fill(bar=bar, side=Side.SELL, contract=_contract(), model=slipping)

    assert buy.price == Decimal("1.10000") + half_spread + _FOUR_POINTS  # 1.10014
    assert sell.price == Decimal("1.10000") - half_spread - _FOUR_POINTS  # 1.09986
    # Not the zero-slippage prices the rest of this module asserts.
    assert buy.price != Decimal("1.10010")
    assert sell.price != Decimal("1.09990")


def test_an_exit_fill_pays_slippage_against_the_trade_on_every_exit_kind() -> None:
    """``resolve_exit``'s own fill site, for the same reason: the exit half of
    the offset reaches the suite nowhere else.

    On the way out the signs reverse -- a long sells lower than the level it
    exited at, a short buys back higher -- so an offset added with the entry's
    sign, or dropped entirely, is worse for the trade in exactly one of these
    four assertions and better in another. Every price below is the resolved
    level (a stop, a gapped open, or the target) plus or minus the same
    0.00004.
    """

    slipping = _zero_slip(slippage_points_per_side=Decimal(4))

    long_stop = resolve_exit(
        bar=_bar(open=Decimal("1.10000"), high=Decimal("1.10100"), low=Decimal("1.09650")),
        side=Side.BUY,
        stop=Decimal("1.09700"),
        target=None,
        contract=_contract(),
        model=slipping,
    )
    long_target = resolve_exit(
        bar=_bar(open=Decimal("1.10000"), high=Decimal("1.10600"), low=Decimal("1.09900")),
        side=Side.BUY,
        stop=Decimal("1.09700"),
        target=Decimal("1.10500"),
        contract=_contract(),
        model=slipping,
    )
    short_stop = resolve_exit(
        bar=_bar(open=Decimal("1.11000"), high=Decimal("1.11100"), low=Decimal("1.10900")),
        side=Side.SELL,
        stop=Decimal("1.10300"),
        target=None,
        contract=_contract(),
        model=slipping,
    )
    short_target = resolve_exit(
        bar=_bar(open=Decimal("1.10000"), high=Decimal("1.10100"), low=Decimal("1.09900")),
        side=Side.SELL,
        stop=Decimal("1.11700"),
        target=Decimal("1.10500"),
        contract=_contract(),
        model=slipping,
    )

    assert long_stop is not None
    assert long_target is not None
    assert short_stop is not None
    assert short_target is not None
    # A long exits by selling: lower is worse.
    assert long_stop.fill.price == Decimal("1.09700") - _FOUR_POINTS  # 1.09696
    assert long_target.fill.price == Decimal("1.10500") - _FOUR_POINTS  # 1.10496
    # A short exits by buying back: higher is worse. 1.11000 is the gapped
    # open, which is already worse than the 1.10300 stop.
    assert short_stop.fill.price == Decimal("1.11000") + _FOUR_POINTS  # 1.11004
    assert short_target.fill.price == Decimal("1.10500") + _FOUR_POINTS  # 1.10504
