"""MetaTrader 5 gateway.

``terminal.py`` imports ``MetaTrader5``, which exists only on Windows, so it is
never imported eagerly here. Import it lazily through attribute access, and only
on a platform that has it.
"""

from typing import Any

__all__ = ["Mt5BrokerAdapter"]


def __getattr__(name: str) -> Any:
    if name == "Mt5BrokerAdapter":
        from trading_house.brokers.mt5.adapter import (  # type: ignore[import-not-found]
            Mt5BrokerAdapter,
        )

        return Mt5BrokerAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
