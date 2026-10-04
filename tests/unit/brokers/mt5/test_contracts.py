# tests/unit/brokers/mt5/test_contracts.py
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_house.brokers.mt5.boundary import (
    SYMBOL_FILLING_FOK,
    SYMBOL_FILLING_IOC,
    SYMBOL_TRADE_EXECUTION_INSTANT,
    SYMBOL_TRADE_EXECUTION_MARKET,
    SYMBOL_TRADE_MODE_CLOSEONLY,
    SYMBOL_TRADE_MODE_DISABLED,
    SYMBOL_TRADE_MODE_FULL,
    SYMBOL_TRADE_MODE_LONGONLY,
    SYMBOL_TRADE_MODE_SHORTONLY,
    Mt5Position,
    Mt5SymbolInfo,
)
from trading_house.brokers.mt5.contracts import (
    asset_class_for,
    decimal_of,
    position_mark_of,
    position_record_of,
    supported_fills,
    to_instrument_contract,
)
from trading_house.core.errors import ConfigurationError
from trading_house.core.instruments import FillPolicy, FinancingModel
from trading_house.core.values import AssetClass


def _info(**overrides: object) -> Mt5SymbolInfo:
    base: dict[str, object] = {
        "name": "EURUSD",
        "digits": 5,
        "point": 1e-05,
        "trade_tick_size": 1e-05,
        "trade_tick_value_loss": 1.0,
        "volume_min": 0.01,
        "volume_step": 0.01,
        "volume_max": 200.0,
        "trade_stops_level": 10,
        "trade_freeze_level": 5,
        "trade_mode": SYMBOL_TRADE_MODE_FULL,
        "trade_exemode": SYMBOL_TRADE_EXECUTION_INSTANT,
        "filling_mode": SYMBOL_FILLING_FOK | SYMBOL_FILLING_IOC,
        "currency_base": "EUR",
        "currency_profit": "USD",
    }
    base.update(overrides)
    return Mt5SymbolInfo(**base)  # type: ignore[arg-type]


def test_float_conversion_avoids_binary_artifacts() -> None:
    """Decimal(0.1) is 0.1000000000000000055511151231257827 — never use it."""

    assert decimal_of(0.1) == Decimal("0.1")
    assert decimal_of(1e-05) == Decimal("0.00001")
    assert str(decimal_of(0.1)) == "0.1"


def test_distances_convert_from_points_to_price_units() -> None:
    contract = to_instrument_contract(_info(), instrument_id="fx.eurusd")

    assert contract.min_stop_distance == Decimal("10") * Decimal("0.00001")
    assert contract.freeze_distance == Decimal("5") * Decimal("0.00001")


def test_zero_stops_level_floors_to_one_price_increment() -> None:
    """PositiveDecimal cannot express 'no minimum', and a stop must still be
    at least one tick from the market."""

    contract = to_instrument_contract(
        _info(trade_stops_level=0, trade_freeze_level=0), instrument_id="fx.eurusd"
    )

    assert contract.min_stop_distance == Decimal("0.00001")
    assert contract.freeze_distance == Decimal("0.00001")


def test_sizing_uses_tick_value_loss_not_tick_value() -> None:
    contract = to_instrument_contract(_info(trade_tick_value_loss=0.97), instrument_id="fx.eurusd")

    assert contract.value_per_price_increment == Decimal("0.97")


def test_price_increment_uses_tick_size_not_point() -> None:
    """point and trade_tick_size differ on some instruments; sizing needs tick size."""

    contract = to_instrument_contract(
        _info(point=1e-05, trade_tick_size=1e-04), instrument_id="fx.eurusd"
    )

    assert contract.price_increment == Decimal("0.0001")


def test_point_size_is_carried_through_from_the_symbol_info() -> None:
    """A stored Bar.spread is an integer in MT5 points. Without the point size
    on the neutral contract, nothing downstream -- the backtester included --
    can convert that spread into a price. contracts.py already reads `point`
    to build min_stop_distance and then throws it away."""

    info = _info(point=0.00001, trade_tick_size=0.00001)

    contract = to_instrument_contract(info, instrument_id="fx.eurusd")

    assert contract.point_size == Decimal("0.00001")


def test_point_size_is_independent_of_price_increment() -> None:
    """point and trade_tick_size are equal on most FX symbols and are not the
    same field. A mapping that aliased one to the other would pass the test
    above and be wrong on any symbol where they differ."""

    info = _info(point=0.001, trade_tick_size=0.005)

    contract = to_instrument_contract(info, instrument_id="metal.xauusd")

    assert contract.point_size == Decimal("0.001")
    assert contract.price_increment == Decimal("0.005")


@pytest.mark.parametrize(
    ("filling_mode", "exemode", "expected"),
    [
        (SYMBOL_FILLING_FOK, SYMBOL_TRADE_EXECUTION_INSTANT, {FillPolicy.FOK}),
        (SYMBOL_FILLING_IOC, SYMBOL_TRADE_EXECUTION_INSTANT, {FillPolicy.IOC}),
        (
            SYMBOL_FILLING_FOK | SYMBOL_FILLING_IOC,
            SYMBOL_TRADE_EXECUTION_INSTANT,
            {FillPolicy.FOK, FillPolicy.IOC},
        ),
        (
            SYMBOL_FILLING_IOC,
            SYMBOL_TRADE_EXECUTION_MARKET,
            {FillPolicy.IOC, FillPolicy.RETURN},
        ),
    ],
)
def test_supported_fills_needs_both_bitmask_and_execution_mode(
    filling_mode: int, exemode: int, expected: set[FillPolicy]
) -> None:
    """RETURN availability depends on exemode, not the bitmask — deriving from
    the bitmask alone silently drops a valid policy."""

    assert supported_fills(filling_mode, exemode) == frozenset(expected)


def test_a_symbol_offering_no_usable_fill_is_refused_not_defaulted() -> None:
    """RETURN is not permitted under Request or Instant execution. Defaulting
    to it to satisfy ``supported_fills``'s min_length=1 would put a policy the
    broker never offered into a domain model."""

    with pytest.raises(ConfigurationError):
        supported_fills(0, SYMBOL_TRADE_EXECUTION_INSTANT)


def test_a_disabled_symbol_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        to_instrument_contract(
            _info(trade_mode=SYMBOL_TRADE_MODE_DISABLED), instrument_id="fx.eurusd"
        )


def test_long_only_symbols_cannot_open_short() -> None:
    contract = to_instrument_contract(
        _info(trade_mode=SYMBOL_TRADE_MODE_LONGONLY), instrument_id="equity_cfd.aapl"
    )

    assert contract.can_open_short is False


@pytest.mark.parametrize(
    ("trade_mode", "can_open_long", "can_open_short"),
    [
        (SYMBOL_TRADE_MODE_FULL, True, True),
        (SYMBOL_TRADE_MODE_LONGONLY, True, False),
        (SYMBOL_TRADE_MODE_SHORTONLY, False, True),
        (SYMBOL_TRADE_MODE_CLOSEONLY, False, False),
    ],
)
def test_trade_mode_drives_both_permission_flags(
    trade_mode: int, can_open_long: bool, can_open_short: bool
) -> None:
    """SHORTONLY must be distinguishable from FULL, and CLOSEONLY from LONGONLY:
    each direction of new-order permission has to be legible on its own."""

    contract = to_instrument_contract(_info(trade_mode=trade_mode), instrument_id="fx.eurusd")

    assert contract.can_open_long is can_open_long
    assert contract.can_open_short is can_open_short


def test_no_two_trade_modes_produce_the_same_permissions() -> None:
    """A collapse here means a consumer cannot tell what it may actually do."""

    modes = [
        SYMBOL_TRADE_MODE_FULL,
        SYMBOL_TRADE_MODE_LONGONLY,
        SYMBOL_TRADE_MODE_SHORTONLY,
        SYMBOL_TRADE_MODE_CLOSEONLY,
    ]
    pairs = [
        (c.can_open_long, c.can_open_short)
        for c in (
            to_instrument_contract(_info(trade_mode=m), instrument_id="fx.eurusd") for m in modes
        )
    ]

    assert len(set(pairs)) == len(modes)


@pytest.mark.parametrize(
    ("instrument_id", "expected"),
    [
        ("fx.eurusd", AssetClass.FX),
        ("metal.xauusd", AssetClass.METAL),
        ("equity_cfd.aapl", AssetClass.EQUITY_CFD),
    ],
)
def test_asset_class_derives_from_the_instrument_id_prefix(
    instrument_id: str, expected: AssetClass
) -> None:
    assert asset_class_for(instrument_id) is expected


def test_an_unknown_prefix_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        asset_class_for("crypto.btcusd")


def test_equity_cfds_are_financed_by_dividend_adjustment() -> None:
    contract = to_instrument_contract(
        _info(trade_mode=SYMBOL_TRADE_MODE_FULL), instrument_id="equity_cfd.aapl"
    )

    assert contract.financing is FinancingModel.DIVIDEND_ADJUSTMENT


def test_fx_and_metals_are_financed_by_swap() -> None:
    assert (
        to_instrument_contract(_info(), instrument_id="fx.eurusd").financing is FinancingModel.SWAP
    )
    assert (
        to_instrument_contract(_info(), instrument_id="metal.xauusd").financing
        is FinancingModel.SWAP
    )


def test_currencies_map_from_base_and_profit() -> None:
    contract = to_instrument_contract(
        _info(currency_base="XAU", currency_profit="USD"), instrument_id="metal.xauusd"
    )

    assert contract.base_currency == "XAU"
    assert contract.quote_currency == "USD"


def _mt5_position(**overrides: object) -> Mt5Position:
    fields: dict[str, object] = {
        "ticket": 1001,
        "magic": 110042,
        "server_symbol": "EURUSD",
        "volume": 0.1,
        "price_open": 1.10000,
        "sl": 1.09500,
        "tp": 1.11000,
        "is_buy": True,
        "opened_at": datetime(2026, 8, 25, tzinfo=UTC),
        "price_current": 1.10200,
        "profit": 20.0,
        "swap": -1.5,
    }
    fields.update(overrides)
    return Mt5Position(**fields)  # type: ignore[arg-type]


def test_a_zero_stop_becomes_none_not_zero() -> None:
    """MT5 reports "no stop" as 0.0, which is also a valid price. Carrying the
    zero through makes unprotected indistinguishable from protected-at-zero,
    and telling those apart is the guard's entire job."""

    assert position_record_of(_mt5_position(sl=0.0)).stop_loss is None
    assert position_record_of(_mt5_position(sl=1.09700)).stop_loss == Decimal("1.09700")


def test_a_mark_carries_the_brokers_price_and_profit_plus_swap() -> None:
    mark = position_mark_of(_mt5_position())

    assert mark.current_price == Decimal("1.102")
    assert mark.unrealized_money == Decimal("18.5")
    assert mark.stop_loss == Decimal("1.095")


def test_a_mark_with_a_zero_stop_has_no_stop() -> None:
    assert position_mark_of(_mt5_position(sl=0.0)).stop_loss is None
