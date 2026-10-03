"""The digest of the bars a replay reads (Phase 8E).

Every protocol declares a ``dataset_sha256`` and, until 8E, nothing computed one. This is the
computation: a domain-separated SHA-256 over the canonical bytes of an ordered bar series. The
engine takes it over the bars it replays and ``ops/dataset.py`` takes it over the same bars
before a run starts, so a declared hash and a sealed one name the same thing.

It lives under ``research/backtest/`` and not in ``research/`` (where the 8E design placed it)
because the engine must call it and ``BACKTEST_ALLOWED`` admits ``trading_house.research.backtest``
and not ``trading_house.research``. The canonical-bytes rules are therefore restated here, over
the twelve fields of a ``Bar``, rather than imported from ``research/canonical.py``: sorted keys,
compact separators, UTF-8, no NaN, ``Decimal`` as ``format(value, "f")`` and UTC as a ``Z`` stamp.
Trailing zeros of a ``Decimal`` are significant (``1.10`` and ``1.1`` are different bytes), which
is the store's own representation and not something this function normalises.

The digest proves what bytes the replay read. It says nothing about where they came from, and a
store that is later corrected hashes differently, by design.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime

from trading_house.core.errors import StatisticalInputError
from trading_house.marketdata.models import Bar

DATASET_DOMAIN = b"trading-house:dataset:v1"
"""Hashed in front of the canonical bytes, so a dataset digest can never be mistaken for the
research chain's (``trading-house:research:v1``) or the audit chain's."""


def _utc_stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _record(bar: Bar) -> dict[str, str | int]:
    """The twelve fields of one bar, and nothing else."""

    return {
        "instrument_id": bar.instrument_id,
        "timeframe": bar.timeframe.value,
        "event_time": _utc_stamp(bar.event_time),
        "availability_time": _utc_stamp(bar.availability_time),
        "open": format(bar.open, "f"),
        "high": format(bar.high, "f"),
        "low": format(bar.low, "f"),
        "close": format(bar.close, "f"),
        "tick_volume": bar.tick_volume,
        "spread": bar.spread,
        "real_volume": bar.real_volume,
        "quality": bar.quality.value,
    }


def dataset_sha256(bars: Sequence[Bar]) -> str:
    """``sha256(DATASET_DOMAIN + canonical JSON array of the bars' records)``.

    Refuses (``StatisticalInputError``) an empty series, an event time that does not strictly
    increase (a duplicate included), and a series of more than one instrument or timeframe: a
    digest of nothing, or of an ambiguous series, is not a content hash. Defective bars are
    hashed like any other; deciding whether to run on them is the replay's business.
    """

    if not bars:
        raise StatisticalInputError() from ValueError("a dataset digest needs at least one bar")
    first = bars[0]
    digest = hashlib.sha256(DATASET_DOMAIN)
    digest.update(b"[")
    previous: datetime | None = None
    for index, bar in enumerate(bars):
        if (bar.instrument_id, bar.timeframe) != (first.instrument_id, first.timeframe):
            raise StatisticalInputError() from ValueError(
                "a dataset digest covers one instrument and one timeframe"
            )
        if previous is not None and bar.event_time <= previous:
            raise StatisticalInputError() from ValueError("bar event times must strictly increase")
        previous = bar.event_time
        if index:
            digest.update(b",")
        digest.update(
            json.dumps(
                _record(bar),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    digest.update(b"]")
    return digest.hexdigest()
