"""The one way a test declares a dataset hash (Phase 8E).

Before 8E every protocol the suite built declared a placeholder (``"a" * 64``) because nothing
could compute one. A run is now refused unless its protocol declares the digest of the window it
replays, so a fixture declares the REAL digest of its own bars through this function, and a test
that needs a hash the data does not have says so by writing a literal beside a name that says it
is wrong.

Computed through the production ``replay_window_bars`` and ``dataset_sha256``, over the bars the
fixture holds, with the window the run is given. That is the digest the engine seals and the
pre-flight takes, which is the point: the fixtures agree with the code under test. The code is
proved against an independent re-implementation (``tests/unit/research/backtest/test_dataset.py``)
and a known-answer literal, not against this helper.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from tests.unit.research.backtest.conftest import FakeBarReader
from trading_house.marketdata.models import Bar
from trading_house.research.backtest.dataset import dataset_sha256
from trading_house.research.backtest.engine import replay_window_bars


def declared_digest(
    bars: Sequence[Bar], *, start: datetime | None = None, end: datetime | None = None
) -> str:
    """The digest a protocol declares for a replay of ``[start, end]`` over ``bars``.

    ``start`` and ``end`` default to the first and last bar, the usual window of a fixture.
    """

    held = tuple(bars)
    return dataset_sha256(
        replay_window_bars(
            FakeBarReader(held),
            held[0].instrument_id,
            held[0].timeframe,
            held[0].event_time if start is None else start,
            held[-1].event_time if end is None else end,
        )
    )
