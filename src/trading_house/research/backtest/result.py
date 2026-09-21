"""What a backtest run hands back: the trades, the rejections, and a digest.

Phase 8 hashes ``BacktestResult.digest()`` into a trial ledger alongside a
Sharpe ratio. A Sharpe nobody can reproduce is not evidence, so the digest
must change whenever anything the run assumed -- including its costs --
changes, and two runs with identical assumptions must hash identically.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Self

from pydantic import NonNegativeInt, model_validator

from trading_house.core.schemas import Side
from trading_house.core.values import CanonicalModel, InstrumentId, NonEmptyStr
from trading_house.marketdata.models import Timeframe
from trading_house.research.backtest.costs import CostModel
from trading_house.research.backtest.fills import ExitKind


class RefusalKind(str, Enum):  # noqa: UP042
    COVERAGE = "coverage"
    DEFECTIVE_BAR = "defective_bar"
    LOOKAHEAD = "lookahead"
    HORIZON = "horizon"


class SimulatedTrade(CanonicalModel):
    """The audit record of one round trip. ``net_pnl`` is not trusted as
    given -- it is checked against the trade's own ``gross_pnl``,
    ``commission`` and ``swap`` (swap is signed; a charge is negative), so a
    trade that claims a net its own terms do not produce is refused here
    rather than surfacing later as an inexplicable equity curve."""

    proposal_id: NonEmptyStr
    side: Side
    lots: Decimal
    entry_price: Decimal
    entry_at: datetime
    exit_price: Decimal
    exit_at: datetime
    exit_kind: ExitKind
    gross_pnl: Decimal
    commission: Decimal
    swap: Decimal
    net_pnl: Decimal

    @model_validator(mode="after")
    def net_reconciles_with_its_own_terms(self) -> Self:
        if self.net_pnl != self.gross_pnl - self.commission + self.swap:
            raise ValueError("net_pnl must equal gross_pnl - commission + swap")
        return self


class BacktestResult(CanonicalModel):
    """What one backtest run produced.

    Deliberately carries no equity-curve field, though section 4 of the spec
    lists one. Under D-4 equity is constant, so the curve is exactly the
    running sum of ``net_pnl`` over ``trades`` -- a caller that wants it
    accumulates it. Storing it here would duplicate state that can disagree
    with the trades it was derived from, which is exactly the class of
    defect ``net_pnl_reconciles_with_trades`` and
    ``SimulatedTrade.net_reconciles_with_its_own_terms`` exist to catch. Do
    not add the field back.
    """

    run_id: NonEmptyStr
    strategy_id: NonEmptyStr
    strategy_version: NonEmptyStr
    instrument_id: InstrumentId
    timeframe: Timeframe
    start: datetime
    end: datetime
    firm_equity: Decimal
    cost_model: CostModel
    trades: tuple[SimulatedTrade, ...]
    rejections: tuple[tuple[NonEmptyStr, ...], ...]  # D-8: each decision's reasons
    bars_seen: NonNegativeInt
    net_pnl: Decimal

    @model_validator(mode="after")
    def net_pnl_reconciles_with_trades(self) -> Self:
        total = sum((trade.net_pnl for trade in self.trades), Decimal(0))
        if self.net_pnl != total:
            raise ValueError("net_pnl must equal the sum of trades' net_pnl")
        return self

    def digest(self) -> str:
        """A hash of every field, including ``cost_model``, so two runs that
        differ in anything they assumed -- costs included -- cannot share a
        digest. Field order in ``model_dump_json()`` comes from each model's
        declaration order, fixed at class definition rather than from
        hash-randomised runtime state, so two field-equal results should
        serialise identically. This module's test covers that within one
        process; Task 6 verifies it holds across processes."""

        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()
