"""The dataset digest of a window, and the refusals that compare it (Phase 8E).

``research/backtest/dataset.py`` owns the digest and ``replay_window_bars`` owns what a replay
reads. This is the one place that reads the bar store for them outside a run: ``window_digest``
is what ``research dataset digest`` prints and what the three run commands take before their first
write, so an operator declares a protocol's hash from the store instead of typing one and a run
whose data is not the declared data is refused while nothing has been appended.

Both comparisons are ``ScenarioEvidenceError`` (exit 19) with an opaque public message and both
digests on the private cause. A window the store does not hold is ``CoverageError``; one that
cannot be digested at all (no bar, a repeated time, two series) is ``StatisticalInputError``
(exit 20), from ``dataset_sha256`` itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trading_house.core.errors import CoverageError, ScenarioEvidenceError
from trading_house.features.engine import BarReader
from trading_house.marketdata.models import Timeframe
from trading_house.research.backtest.dataset import dataset_sha256
from trading_house.research.backtest.engine import replay_window_bars


@dataclass(frozen=True, slots=True)
class WindowDigest:
    """The digest of one window, and what it was taken over."""

    dataset_sha256: str
    bar_count: int
    first_event_time: datetime
    last_event_time: datetime


def window_digest(
    reader: BarReader, *, instrument_id: str, timeframe: Timeframe, start: datetime, end: datetime
) -> WindowDigest:
    """The digest of the bars a replay of ``[start, end]`` reads, from ``reader``.

    Read through ``replay_window_bars``, the function the engine reads with. The window must lie
    inside the stored bars, as a run requires (``CoverageError`` otherwise, the store's own
    refusal): a digest of a window the store only partly holds would name data no run can use.
    A window inside the store that holds no bar is ``StatisticalInputError``.
    """

    latest = reader.coverage(instrument_id, timeframe).latest_event_time
    if latest is None or end > latest:
        raise CoverageError()
    bars = replay_window_bars(reader, instrument_id, timeframe, start, end)
    return WindowDigest(
        dataset_sha256=dataset_sha256(bars),
        bar_count=len(bars),
        first_event_time=bars[0].event_time,
        last_event_time=bars[-1].event_time,
    )


def refuse_dataset_mismatch(declared: str | None, computed: str) -> None:
    """The window's computed digest must be the one the protocol declared.

    Names both, because the operator's next move is to check the store or to declare the digest
    ``research dataset digest`` prints in a new protocol.
    """

    if declared != computed:
        raise ScenarioEvidenceError() from ValueError(
            f"the protocol declares dataset {declared}; the stored bars of its window hash to "
            f"{computed}"
        )


def refuse_changed_dataset(before: str, after: str | None) -> None:
    """A run's computed digest must be the one the pre-flight took: the store did not change."""

    if before != after:
        raise ScenarioEvidenceError() from ValueError(
            f"the bar store changed during the run: the pre-flight hashed the window to {before} "
            f"and the run read {after}"
        )
