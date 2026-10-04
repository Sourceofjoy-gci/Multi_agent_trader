"""The capacity measurement: a declared volume-to-lots model, read over a sealed run.

Phase 12 replaces 8B3's permanent ``UNAVAILABLE``. A protocol may now declare a
``CapacitySpec`` before any result exists, and a run under that protocol seals the market
volume at each of its fills (``EvidenceBundle.liquidity``). With both, capacity is
measured; without either, it is unavailable and says which is missing.

This module measures and states no verdict, like every other 8C measurement: the declared
limits ride beside the measured values, and gate 9 is what compares them.

The model, ``tick_volume_participation_v1``, at the declared ``target_equity``:

- a fill's participation is ``lots x scale`` over ``tick_volume x lots_per_tick``, where
  ``scale`` is target equity over the run's equity (constant-notional lots, leverage cap
  included, scale linearly with equity);
- its impact is ``impact_points_at_full_participation x sqrt(participation)`` points, priced
  at the run's own money per point per lot;
- the run's net edge at that scale is the sum of its trades' net P&L times ``scale``.

A fill on a bar that reported no tick volume had no measurable market to take from. It is
counted, never divided by, and gate 9 fails on any. Tick volume is MetaTrader 5's count of
price updates; that it stands for lots at all is the protocol's declared assumption.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from pydantic import Field, NonNegativeInt

from trading_house.core.values import CanonicalModel, NonEmptyStr
from trading_house.research.trial_ledger import CapacitySpec, TrialProtocol

if TYPE_CHECKING:  # pragma: no cover
    # Type-only: evidence.py imports promotion.py, which imports this module, so a runtime
    # import of the bundle here would be a cycle. Nothing below needs the class itself.
    from trading_house.research.evidence import EvidenceBundle


def _is_absent(value: object) -> bool:
    return value is None


class CapacityStatus(str, Enum):  # noqa: UP042
    UNAVAILABLE = "unavailable"
    MEASURED = "measured"


class CapacityDiagnostic(CanonicalModel):
    """Every measured field is absent unless ``status`` is ``measured``.

    The fields are excluded when absent so a validation report sealed before Phase 12 --
    ``unavailable`` with a reason and nothing else -- re-reads to its own bytes.
    """

    status: CapacityStatus
    reason: NonEmptyStr
    spec: CapacitySpec | None = Field(default=None, exclude_if=_is_absent)
    fills: NonNegativeInt | None = Field(default=None, exclude_if=_is_absent)
    zero_volume_fills: NonNegativeInt | None = Field(default=None, exclude_if=_is_absent)
    max_participation: Decimal | None = Field(default=None, exclude_if=_is_absent)
    impact_cost: Decimal | None = Field(default=None, exclude_if=_is_absent)
    net_edge: Decimal | None = Field(default=None, exclude_if=_is_absent)
    impact_fraction_of_edge: Decimal | None = Field(default=None, exclude_if=_is_absent)
    """``None`` while measured means the edge at scale is not positive: nothing to spend."""


def _unavailable(reason: str) -> CapacityDiagnostic:
    return CapacityDiagnostic(status=CapacityStatus.UNAVAILABLE, reason=reason)


def capacity_diagnostic(
    protocol: TrialProtocol, baseline: EvidenceBundle | None = None
) -> CapacityDiagnostic:
    """Capacity of the trial's 1.0x constant-notional baseline under the declared model."""

    spec = protocol.capacity
    if spec is None:
        return _unavailable(
            "the protocol declares no volume-to-lots model, and tick volume alone is never "
            "presented as capital capacity"
        )
    if baseline is None:
        return _unavailable("there is no sealed baseline run to measure")
    if baseline.liquidity is None:
        return _unavailable("the baseline run sealed no liquidity record to measure against")
    trades = baseline.result.trades
    if not trades:
        return _unavailable("the baseline run has no trades, so no fill to measure")

    scale = spec.target_equity / baseline.result.firm_equity
    fills = zero = 0
    max_participation = Decimal(0)
    impact = Decimal(0)
    for trade, liquidity in zip(trades, baseline.liquidity.trades, strict=True):
        lots = trade.lots * scale
        for volume in (liquidity.entry_tick_volume, liquidity.exit_tick_volume):
            fills += 1
            market = volume * spec.lots_per_tick
            if market == 0:
                zero += 1
                continue
            participation = lots / market
            max_participation = max(max_participation, participation)
            impact += (
                spec.impact_points_at_full_participation
                * participation.sqrt()
                * liquidity.money_per_point_per_lot
                * lots
            )
    edge = sum((trade.net_pnl for trade in trades), Decimal(0)) * scale
    return CapacityDiagnostic(
        status=CapacityStatus.MEASURED,
        reason=(
            f"{fills} fills at a target equity of {spec.target_equity} "
            f"({scale} times the run's own)"
        ),
        spec=spec,
        fills=fills,
        zero_volume_fills=zero,
        max_participation=max_participation,
        impact_cost=impact,
        net_edge=edge,
        impact_fraction_of_edge=impact / edge if edge > 0 else None,
    )
