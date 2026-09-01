"""The only thing market-data ingest asks of a venue.

One method. Ingest depends on this and nothing else, which is why
``marketdata/`` contains no MetaTrader5 import and an acceptance test can say
so. Bars are a data concern, so this stays off ``BrokerAdapter``'s
eight-method trading surface while keeping the venue-neutral promise true for
market data too.

This module imports ``brokers.mt5.boundary`` for ``Mt5Bar``, and
``brokers/mt5/adapter.py`` imports this module's ``Timeframe`` in turn. That
is a package-level cycle but not a module-level one -- ``boundary.py``
imports nothing from ``marketdata`` and ``models.py`` imports nothing from
``brokers`` -- so it resolves cleanly. It is deliberate: the alternative is a
translation layer whose only job would be renaming a dataclass. The
constraint that actually matters is one-directional and unchanged:
``marketdata/`` must not reach MetaTrader5.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from trading_house.brokers.mt5.boundary import Mt5Bar
from trading_house.marketdata.models import Timeframe


@runtime_checkable
class HistoryProvider(Protocol):
    def history(
        self, instrument_id: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> Sequence[Mt5Bar]: ...
