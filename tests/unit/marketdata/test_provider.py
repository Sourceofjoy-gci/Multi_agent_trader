from trading_house.marketdata.provider import HistoryProvider


def test_the_provider_surface_is_one_method() -> None:
    """A wide data port is a leaky one. Everything else the gateway can do is
    a trading concern and belongs on BrokerAdapter."""

    assert {name for name in vars(HistoryProvider) if not name.startswith("_")} == {"history"}


def test_the_mt5_adapter_satisfies_it() -> None:
    from trading_house.brokers.mt5.adapter import Mt5BrokerAdapter

    assert hasattr(Mt5BrokerAdapter, "history")
