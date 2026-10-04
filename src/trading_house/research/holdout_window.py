"""The windows the unseen-holdout rule compares (Phase 12).

Kept out of ``research/promotion.py``, which is pure and imports no time module at all (a
test walks its import graph): these are values and comparisons of instants, not reads of a
clock, but the boundary is drawn at the import so nobody has to argue about it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import field_validator

from trading_house.core.clock import ensure_utc
from trading_house.core.errors import TimestampError
from trading_house.core.values import CanonicalModel, NonEmptyStr
from trading_house.marketdata.models import Timeframe

RecordedAt = datetime
"""When the ledger recorded a row: the database's clock on the append, never a declared time."""


class SealedWindow(CanonicalModel):
    """The bars one sealed bundle read."""

    instrument_id: NonEmptyStr
    timeframe: Timeframe
    start: datetime
    end: datetime
    opened: bool
    """The bundle is a holdout opening's (its provenance says opened or consumed)."""

    @field_validator("start", "end")
    @classmethod
    def normalize(cls, value: datetime) -> datetime:
        try:
            return ensure_utc(value)
        except TimestampError as error:
            raise ValueError(str(error)) from error

    def reaches_into(self, start: datetime, end: datetime) -> bool:
        """Whether any bar this run read lies in ``[start, end]``. A replay reads every bar
        whose open time is in ``[start, end]``, both inclusive (``replay_window_bars``)."""

        return windows_overlap(self.start, self.end, start, end)


def windows_overlap(
    start: datetime, end: datetime, other_start: datetime, other_end: datetime
) -> bool:
    return start <= other_end and other_start <= end


def research_reaches_into(
    data_start: datetime, data_end: datetime, start: datetime, end: datetime
) -> bool:
    """Whether a research window's bars reach ``[start, end]``: both are inclusive open times."""

    return windows_overlap(data_start, data_end, start, end)
