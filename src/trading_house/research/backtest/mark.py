"""Mark-to-market equity observations and the canonical daily return series.

Phase 8B1. The engine emits one observation per processed bar; this module owns
the shape those take, the arithmetic that must hold at every one of them, and the
rectangular daily reduction ``derive_daily_returns`` performs on them.

A mark is a **valuation**, not a liquidation value. An open position is marked
at the bar's mid close, so the series reports it worth more than closing it
would actually fetch, because the fill model charges the half-spread crossing
into the entry fill and a stop or target exit crosses no spread at all. (Slippage
is a separate matter and *is* applied on the exit side: ``resolve_exit`` computes
one ``slippage_price_offset`` for the closing leg and uses it for both the stop
and the target branch. What those exits skip is the spread, not the slippage.)
Every drawdown figure a later phase derives from this series is mark-to-market
and never realizable, and the basis field on the bundle is what says so.

``DailyReturnPoint`` is defined here rather than in ``research/evidence.py``
because ``BACKTEST_ALLOWED`` admits ``trading_house.research.backtest`` and not
``trading_house.research``: a module in this subtree may not import from the one
above it. ``research/evidence.py`` re-exports the name, and the serialized shape
is unchanged, so no digest moves.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Self

from pydantic import NonNegativeInt, field_validator, model_validator

from trading_house.core.clock import ensure_utc
from trading_house.core.errors import EquityEvidenceError, TimestampError
from trading_house.core.values import CanonicalModel
from trading_house.research.backtest.costs_attribution import (
    CostAttribution,
    attribution_disagreement,
)
from trading_house.research.backtest.result import BacktestResult

MAX_EQUITY_OBSERVATIONS = 2_000_000
# ponytail: an O(1) guard on a count the loop already knows. It is here to fail
# with a number instead of filling the disk; raise it only if a real run needs
# more, and prefer a coarser timeframe to a larger budget. The four-year M15
# Phase 7 run produced 99,988 observations, so this sits ~20x above realistic.


def _utc(value: datetime) -> datetime:
    """``research/evidence.py``'s own timestamp helper, mirrored rather than
    imported. Private there, and importing a private name across modules is how
    a second copy of a rule becomes two rules."""

    try:
        return ensure_utc(value)
    except TimestampError as error:
        raise ValueError(str(error)) from error


class DailyReturnPoint(CanonicalModel):
    """One day's return. A ``date`` and not a timestamp: a daily series whose
    entries carry a time of day has to be truncated before anyone can compare
    two of them."""

    day: date
    value: Decimal


class EquityObservation(CanonicalModel):
    """The state of the book at one bar's close, after that bar's own fills and
    exits have been applied."""

    marked_at: datetime
    equity: Decimal
    cumulative_realized_pnl: Decimal
    unrealized_pnl: Decimal
    open_positions: NonNegativeInt

    @field_validator("marked_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


class EquitySeries(CanonicalModel):
    """Every processed bar's observation, over a fixed capital base.

    Validated against itself alone: the three assertions that need the trades
    live on ``BacktestOutcome`` and, for a sealed document, on
    ``EvidenceBundle`` -- the two types that hold the pair.
    """

    firm_equity: Decimal
    observations: tuple[EquityObservation, ...]

    @model_validator(mode="after")
    def time_only_moves_forward_and_every_point_reconciles(self) -> Self:
        if not self.observations:
            raise ValueError("an equity series needs at least one observation")
        # pairwise, not zip(series, series[1:]): the two are different lengths by
        # exactly one, so strict=True would raise on every real series.
        for earlier, later in pairwise(self.observations):
            if later.marked_at <= earlier.marked_at:
                raise ValueError("equity observations must be strictly increasing in UTC time")
        for point in self.observations:
            reconciled = self.firm_equity + point.cumulative_realized_pnl + point.unrealized_pnl
            if point.equity != reconciled:
                raise ValueError("equity must equal firm equity plus realized plus unrealized")
            # Nothing open means nothing unrealized, and that is what makes
            # ``BacktestOutcome``'s conditional reconciliation sound: its flat
            # branch reduces to ``equity == firm_equity + net_pnl`` only because
            # the unrealized term is zero, which nothing else checks. Enforced
            # per-point beside the identity it depends on, because a flag that
            # could disagree with the count is the duplicated state
            # ``result.py`` refuses to carry, not a cheaper place to ask.
            if point.open_positions == 0 and point.unrealized_pnl != 0:
                raise ValueError("an observation with nothing open cannot carry unrealized PnL")
        return self

    @property
    def is_flat(self) -> bool:
        """Whether the run ended with nothing open.

        Derived rather than stored: a boolean field would duplicate
        ``open_positions`` on the final observation and could disagree with it.
        """
        return self.observations[-1].open_positions == 0


class BacktestOutcome(CanonicalModel):
    """What one backtest run produced: its reconciled result, the equity path
    that produced it, and the per-trade cost split that decomposes it.

    ``result.py`` refuses to carry an equity curve because, under D-4, the curve
    was exactly the running sum of ``net_pnl`` and storing it "would duplicate
    state that can disagree with the trades it was derived from". The first half
    of that stopped being true once an open position is marked. The second half
    is answered here rather than dismissed: the assertions below are
    exactly the disagreement checks, and they fail closed.
    """

    result: BacktestResult
    equity: EquitySeries
    attribution: CostAttribution

    @model_validator(mode="after")
    def series_is_the_result_it_came_from(self) -> Self:
        if self.equity.firm_equity != self.result.firm_equity:
            raise ValueError("the series and the result must share one firm equity")
        if len(self.equity.observations) != self.result.bars_seen:
            raise ValueError("the series must hold one observation per processed bar")
        if self.equity.is_flat:
            final = self.equity.observations[-1].cumulative_realized_pnl
            if final != self.result.net_pnl:
                raise ValueError(
                    "a flat run's final realized total must equal the result's net PnL"
                )
        return self

    @model_validator(mode="after")
    def the_attribution_is_the_result_it_decomposes(self) -> Self:
        """The split is checked against the trades rather than trusted.

        The rule and its wording live in
        ``costs_attribution.attribution_disagreement``, and this calls it rather
        than restating it. The import constraint runs downward only:
        ``BACKTEST_ALLOWED`` admits ``trading_house.research.backtest`` and not
        ``trading_house.research``, so a module in *this* subtree may not import
        from the one above it, while ``research/evidence.py`` already imports
        from here. Task 3's ``EvidenceBundle`` therefore calls the same
        predicate, and a disagreement reads the same whether it was caught at
        construction or at read time.
        """

        reason = attribution_disagreement(self.attribution, self.result)
        if reason is not None:
            raise ValueError(reason)
        return self


def derive_daily_returns(
    series: EquitySeries, *, first_day: date, last_day: date
) -> tuple[DailyReturnPoint, ...]:
    """The canonical daily return series: every UTC calendar day, inclusive.

    End-of-day equity is the final mark within that day. A day with no mark
    carries the prior end-of-day equity forward and therefore returns a literal
    zero -- a calendar-day series has no way to omit a day, and the bundle's
    ``return_series_basis`` is what stops a reader mistaking that zero for a
    measured one. A range holding no mark at all is therefore an all-zero
    rectangle, which is the honest answer for a period with no trading; leave it
    that way rather than turning it into a refusal, because a flat series and an
    untraded one are the same measurement. A range that runs backwards is the
    opposite: there is no honest answer to give, so it is refused. The first
    day's denominator is initial firm equity; later ones are the preceding day's
    end equity and must be strictly positive.
    """

    if first_day > last_day:
        raise EquityEvidenceError()

    closes: dict[date, Decimal] = {}
    for point in series.observations:
        closes[point.marked_at.date()] = point.equity

    points: list[DailyReturnPoint] = []
    previous_close = series.firm_equity
    day = first_day
    while day <= last_day:
        end_of_day = closes.get(day, previous_close)
        if previous_close <= 0:
            raise EquityEvidenceError()
        points.append(
            DailyReturnPoint(day=day, value=(end_of_day - previous_close) / previous_close)
        )
        previous_close = end_of_day
        day += timedelta(days=1)
    if previous_close <= 0:
        # The final day took the account to zero or below and the walk has run
        # out of days to notice it on. Same refusal as the in-loop check, and for
        # the same reason: the next day's divisor would be this equity. Kept in
        # step with ``legacy_import.derive_realized_daily_returns`` on purpose --
        # two reductions over the same kind of series must not disagree about
        # whether a zero equity is a result.
        raise EquityEvidenceError()
    return tuple(points)
