"""The bar fill model: where an entry fills, and whether an open position
exited on a given bar.

A bar carries only OHLC, not tick order -- it cannot say whether a range that
touches both the stop and the target was hit stop-first or target-first
within the bar. ``resolve_exit`` resolves that ambiguity by checking the stop
before the target (D-2): assuming the target lets a losing strategy look
profitable indefinitely, while assuming the stop only costs some genuine
winners, which is the recoverable direction to be wrong in.

``resolve_exit`` handles the stop and the target only. It never produces
``ExitKind.TIME`` -- it takes no time input, so it cannot know when the
position opened or what a strategy's ``max_holding_seconds`` is. The engine
owns the time stop, because it holds both pieces this function does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from trading_house.core.instruments import InstrumentContract
from trading_house.core.schemas import Side
from trading_house.marketdata.models import Bar
from trading_house.research.backtest.costs import CostModel, slippage_price_offset

_HALF: Decimal = Decimal(2)


class ExitKind(str, Enum):  # noqa: UP042
    STOP = "stop"
    TARGET = "target"
    TIME = "time"


@dataclass(frozen=True, slots=True)
class Fill:
    price: Decimal
    at: datetime


@dataclass(frozen=True, slots=True)
class Exit:
    kind: ExitKind
    fill: Fill


def entry_fill(*, bar: Bar, side: Side, contract: InstrumentContract, model: CostModel) -> Fill:
    """Fill at the given bar's own open, crossing half its spread -- never the
    close that generated the signal (section 11.1's named violation)."""

    half_spread = Decimal(bar.spread) * contract.point_size / _HALF
    raw_price = bar.open + half_spread if side is Side.BUY else bar.open - half_spread
    offset = slippage_price_offset(model=model, side=side, contract=contract, opening=True)
    # The stamp is one bar later than the price. ``bar.open`` is the instant
    # this fills at, but ``at`` is ``bar.availability_time`` -- that bar's
    # close. So every ``entry_at`` and ``exit_at`` in a result misreports the
    # fill instant by one bar, the engine's time stop honours
    # ``max_holding_seconds`` as H plus one bar, and ``swap_cost`` sees a date
    # pair shifted by the same amount. Plan-mandated -- the plan's own test
    # demands this stamp -- and every known-answer number in this phase was
    # derived against it, so correcting the stamps would move all of them.
    # Carried to Phase 7 rather than fixed here; the README states the
    # H-plus-one-bar effect so Phase 7 finds it rather than discovers it.
    return Fill(price=raw_price + offset, at=bar.availability_time)


def resolve_exit(
    *,
    bar: Bar,
    side: Side,
    stop: Decimal,
    target: Decimal | None,
    contract: InstrumentContract,
    model: CostModel,
) -> Exit | None:
    """Whether this bar closed the position, and at what price.

    The stop is checked before the target -- deliberately, this order is the
    decision (D-2). A bar's range cannot say which was touched first, and
    silently reordering this into "whichever is closer" reintroduces the
    defect.
    """

    offset = slippage_price_offset(model=model, side=side, contract=contract, opening=False)

    if side is Side.BUY:
        if bar.low <= stop:
            raw_price = min(stop, bar.open)
            return Exit(
                kind=ExitKind.STOP, fill=Fill(price=raw_price + offset, at=bar.availability_time)
            )
        if target is not None and bar.high >= target:
            return Exit(
                kind=ExitKind.TARGET, fill=Fill(price=target + offset, at=bar.availability_time)
            )
        return None

    if bar.high >= stop:
        raw_price = max(stop, bar.open)
        return Exit(
            kind=ExitKind.STOP, fill=Fill(price=raw_price + offset, at=bar.availability_time)
        )
    if target is not None and bar.low <= target:
        return Exit(
            kind=ExitKind.TARGET, fill=Fill(price=target + offset, at=bar.availability_time)
        )
    return None
