"""The validated statistical input: a daily return series and a trade sample.

Phase 8C1. Money stays ``Decimal`` everywhere it is held; this module is the one
place a sealed bundle's ``Decimal`` becomes ``float64`` (umbrella 7.1), and it
does so once, with a finite check, so no later statistic has to wonder whether a
``NaN`` crept in at the boundary. Every refusal is a ``StatisticalInputError``
whose specifics ride on the private cause; nothing is defaulted or repaired.

These are frozen slotted dataclasses rather than ``CanonicalModel``s because
pydantic cannot hold an ``ndarray`` (the ``BacktestRequest`` precedent), and
``eq=False`` because ``==`` on an array is not a bool. The arrays are made
read-only so a statistic cannot mutate the input it was handed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt

from trading_house.core.errors import StatisticalInputError
from trading_house.features.sessions import session_of
from trading_house.research.trial_ledger import ReturnSeriesBasis

if TYPE_CHECKING:
    from trading_house.research.evidence import EvidenceBundle

MIN_SERIES_DAYS = 30
"""The fewest days a return series may hold. Fixed here, before any 8C output exists."""

_ONE_DAY = timedelta(days=1)


def refusal(reason: str) -> StatisticalInputError:
    """The one way this package refuses: opaque public message, the specifics on the cause."""

    error = StatisticalInputError()
    error.__cause__ = ValueError(reason)
    return error


def _floats(values: Iterable[Decimal]) -> npt.NDArray[np.float64]:
    """``Decimal`` to ``float64``. A value no float can hold is refused later
    by the finite check; one that cannot be converted at all (a signalling NaN) here."""

    try:
        array = np.array([float(value) for value in values], dtype=np.float64)
    except (ValueError, OverflowError) as error:
        raise refusal("a value cannot be converted to a float") from error
    return array


def _as_tuple(value: Any, what: str) -> tuple[Any, ...]:
    if not isinstance(value, Iterable):
        raise refusal(f"{what} must be a sequence")
    return tuple(value)


def _readonly_vector(values: object, length: int, what: str) -> npt.NDArray[np.float64]:
    """A checked, read-only copy: the caller's array is neither kept nor made read-only."""

    if not isinstance(values, np.ndarray):
        raise refusal(f"{what} must be a numpy array")
    if values.ndim != 1:
        raise refusal(f"{what} must be one-dimensional")
    if values.dtype != np.float64:
        raise refusal(f"{what} must be float64")
    if len(values) != length:
        raise refusal(f"{what} must hold one value per entry")
    if not bool(np.isfinite(values).all()):
        raise refusal(f"{what} must be finite")
    copy = values.copy()
    copy.flags.writeable = False
    return copy


def _is_day(value: object) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


@dataclass(frozen=True, slots=True, eq=False)
class ReturnSeries:
    """One candidate's daily returns: contiguous UTC dates, ``float64`` values.

    ``promotion_grade`` is a statement about the basis and nothing else: only the
    mark-to-market series is one a later gate may rely on; a realized-trades
    series stays reportable and is not (spec S-3).
    """

    days: tuple[date, ...]
    values: npt.NDArray[np.float64]
    basis: ReturnSeriesBasis
    evidence_sha256: str

    def __post_init__(self) -> None:
        if self.basis is ReturnSeriesBasis.MISSING:
            raise refusal("the bundle's return series basis is missing")
        days = _as_tuple(self.days, "the return series days")
        if not all(_is_day(day) for day in days):
            raise refusal("the return series days must be dates")
        if not days:
            raise refusal("the return series is empty")
        for earlier, later in pairwise(days):
            if later <= earlier:
                raise refusal("the return series days are not strictly increasing")
            if later - earlier != _ONE_DAY:
                raise refusal("the return series days are not contiguous")
        values = _readonly_vector(self.values, len(days), "the return series values")
        if len(days) < MIN_SERIES_DAYS:
            raise refusal(
                f"the return series holds {len(days)} days; at least {MIN_SERIES_DAYS} are required"
            )
        object.__setattr__(self, "days", days)
        object.__setattr__(self, "values", values)

    @property
    def promotion_grade(self) -> bool:
        return self.basis is ReturnSeriesBasis.MARK_TO_MARKET

    @classmethod
    def from_bundle(cls, bundle: EvidenceBundle, evidence_sha256: str) -> ReturnSeries:
        return cls(
            days=tuple(point.day for point in bundle.daily_returns),
            values=_floats(point.value for point in bundle.daily_returns),
            basis=bundle.return_series_basis,
            evidence_sha256=evidence_sha256,
        )


@dataclass(frozen=True, slots=True, eq=False)
class TradeSample:
    """The closed trades of one bundle, as parallel columns, in the bundle's order.

    Zero trades is a legitimate sample (a candidate may not have traded), so it is
    not refused. ``session`` is the entry instant's session label, the only regime
    classifier this repository has.
    """

    entry_day: tuple[date, ...]
    exit_day: tuple[date, ...]
    entry_at: tuple[datetime, ...]
    exit_at: tuple[datetime, ...]
    net_pnl: npt.NDArray[np.float64]
    session: tuple[str, ...]

    def __post_init__(self) -> None:
        entry_day = _as_tuple(self.entry_day, "the trade sample entry days")
        exit_day = _as_tuple(self.exit_day, "the trade sample exit days")
        entry_at = _as_tuple(self.entry_at, "the trade sample entry instants")
        exit_at = _as_tuple(self.exit_at, "the trade sample exit instants")
        session = _as_tuple(self.session, "the trade sample sessions")
        count = len(entry_at)
        if any(len(column) != count for column in (entry_day, exit_day, exit_at, session)):
            raise refusal("the trade sample columns differ in length")
        if not all(isinstance(moment, datetime) for moment in (*entry_at, *exit_at)):
            raise refusal("the trade sample instants must be datetimes")
        if not all(_is_day(day) for day in (*entry_day, *exit_day)):
            raise refusal("the trade sample days must be dates")
        net_pnl = _readonly_vector(self.net_pnl, count, "the trade sample net P&L")
        if any(exit_ < entry for entry, exit_ in zip(entry_at, exit_at, strict=True)):
            raise refusal("a trade exits before it enters")
        if any(day != moment.date() for day, moment in zip(entry_day, entry_at, strict=True)):
            raise refusal("a trade's entry day is not the day of its entry instant")
        if any(day != moment.date() for day, moment in zip(exit_day, exit_at, strict=True)):
            raise refusal("a trade's exit day is not the day of its exit instant")
        for name, value in (
            ("entry_day", entry_day),
            ("exit_day", exit_day),
            ("entry_at", entry_at),
            ("exit_at", exit_at),
            ("net_pnl", net_pnl),
            ("session", session),
        ):
            object.__setattr__(self, name, value)

    def __len__(self) -> int:
        return len(self.entry_at)

    @classmethod
    def from_bundle(cls, bundle: EvidenceBundle) -> TradeSample:
        trades = bundle.result.trades
        return cls(
            entry_day=tuple(trade.entry_at.date() for trade in trades),
            exit_day=tuple(trade.exit_at.date() for trade in trades),
            entry_at=tuple(trade.entry_at for trade in trades),
            exit_at=tuple(trade.exit_at for trade in trades),
            net_pnl=_floats(trade.net_pnl for trade in trades),
            session=tuple(session_of(trade.entry_at).value for trade in trades),
        )
