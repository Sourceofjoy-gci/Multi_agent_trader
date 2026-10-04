"""Phase 12: the declared capacity model, measured over a sealed baseline.

The arithmetic cases use a stand-in bundle with round numbers so every figure can be
worked on paper; one case runs the real fixture bundle to prove the wiring.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.unit.ops.test_scenarios import BARS, _bundle_at, _protocol, _rebuild, _trial_id
from trading_house.research.backtest.liquidity import Liquidity, TradeLiquidity
from trading_house.research.trial_ledger import CapacitySpec
from trading_house.research.validation.capacity import (
    CapacityDiagnostic,
    CapacityStatus,
    capacity_diagnostic,
)

SPEC = CapacitySpec(
    model="tick_volume_participation_v1",
    lots_per_tick=Decimal("0.01"),
    target_equity=Decimal(200000),
    max_participation=Decimal("0.05"),
    impact_points_at_full_participation=Decimal(4),
    max_impact_fraction_of_edge=Decimal("0.5"),
)


def _declaring(spec: CapacitySpec | None = SPEC):
    return _protocol().model_copy(update={"capacity": spec})


def _liquidity(entry: int, exit_: int, proposal_id: str) -> TradeLiquidity:
    return TradeLiquidity(
        proposal_id=proposal_id,
        entry_tick_volume=entry,
        exit_tick_volume=exit_,
        money_per_point_per_lot=Decimal(1),
    )


def _stand_in(trades, liquidity) -> SimpleNamespace:
    return SimpleNamespace(
        result=SimpleNamespace(firm_equity=Decimal(100000), trades=trades),
        liquidity=None if liquidity is None else SimpleNamespace(trades=liquidity),
    )


TWO_TRADES = (
    SimpleNamespace(lots=Decimal("1.0"), net_pnl=Decimal(100)),
    SimpleNamespace(lots=Decimal("0.5"), net_pnl=Decimal(50)),
)
TWO_LIQUIDITY = (_liquidity(10000, 2500, "a"), _liquidity(10000, 0, "b"))


def test_no_declared_model_is_unavailable_and_says_so() -> None:
    diagnostic = capacity_diagnostic(_declaring(None))

    assert diagnostic.status is CapacityStatus.UNAVAILABLE
    assert "declares no volume-to-lots model" in diagnostic.reason
    assert "tick volume alone is never presented as capital capacity" in diagnostic.reason


@pytest.mark.parametrize(
    ("baseline", "reason"),
    [
        (None, "no sealed baseline"),
        (_stand_in(TWO_TRADES, None), "no liquidity record"),
        (_stand_in((), ()), "no trades"),
    ],
    ids=["no-baseline", "no-liquidity", "no-trades"],
)
def test_a_declared_model_with_nothing_to_measure_is_unavailable(baseline, reason) -> None:
    diagnostic = capacity_diagnostic(_declaring(), baseline)

    assert diagnostic.status is CapacityStatus.UNAVAILABLE
    assert reason in diagnostic.reason


def test_the_measurement_by_hand() -> None:
    """Target 200,000 over the run's 100,000: scale 2. 0.01 lots per tick.

    trade a, 2.0 lots at scale: entry 10,000 ticks = 100 lots, participation 0.02,
        impact 4 x sqrt(0.02) x $1 x 2.0 = 1.13137...
        exit 2,500 ticks = 25 lots, participation 0.08,
        impact 4 x sqrt(0.08) x 2.0 = 2.26274...
    trade b, 1.0 lot: entry 100 lots, participation 0.01, impact 4 x 0.1 x 1.0 = 0.4;
        exit on a zero-volume bar: counted, not divided by
    max participation 0.08; impact 3.79411...; edge (100 + 50) x 2 = 300;
    fraction 3.79411 / 300 = 0.0126470...
    """

    diagnostic = capacity_diagnostic(_declaring(), _stand_in(TWO_TRADES, TWO_LIQUIDITY))

    assert diagnostic.status is CapacityStatus.MEASURED
    assert diagnostic.fills == 4
    assert diagnostic.zero_volume_fills == 1
    assert diagnostic.max_participation == Decimal("0.08")
    assert float(diagnostic.impact_cost) == pytest.approx(3.7941125497, rel=1e-9)
    assert diagnostic.net_edge == Decimal(300)
    assert float(diagnostic.impact_fraction_of_edge) == pytest.approx(0.0126470418, rel=1e-8)
    assert diagnostic.spec == SPEC


def test_no_positive_edge_leaves_the_fraction_undefined() -> None:
    losing = (SimpleNamespace(lots=Decimal(1), net_pnl=Decimal(-10)),)

    diagnostic = capacity_diagnostic(
        _declaring(), _stand_in(losing, (_liquidity(10000, 10000, "a"),))
    )

    assert diagnostic.net_edge == Decimal(-20)
    assert diagnostic.impact_fraction_of_edge is None


def test_an_unavailable_diagnostic_serialises_as_it_did_before_phase_12() -> None:
    """A report sealed before the measured fields existed must re-read to its own bytes."""

    diagnostic = CapacityDiagnostic(status=CapacityStatus.UNAVAILABLE, reason="no model")

    assert diagnostic.model_dump(mode="json") == {"status": "unavailable", "reason": "no model"}


def test_a_real_sealed_bundle_is_measured_fill_by_fill() -> None:
    protocol = _protocol()
    bundle = _bundle_at(protocol, BARS, Decimal(1), trial_id=_trial_id(protocol))
    assert bundle.result.trades
    liquidity = Liquidity(
        trades=tuple(_liquidity(5000, 5000, trade.proposal_id) for trade in bundle.result.trades)
    )
    sealed = _rebuild(bundle, _with(liquidity))

    diagnostic = capacity_diagnostic(_declaring(), sealed)

    assert diagnostic.status is CapacityStatus.MEASURED
    assert diagnostic.fills == 2 * len(bundle.result.trades)
    assert diagnostic.zero_volume_fills == 0


def test_a_bundle_whose_liquidity_is_not_its_trades_is_refused() -> None:
    protocol = _protocol()
    bundle = _bundle_at(protocol, BARS, Decimal(1), trial_id=_trial_id(protocol))
    wrong = Liquidity(trades=(_liquidity(1, 1, "someone-else"),))

    with pytest.raises(ValueError, match="exactly the result's trades"):
        _rebuild(bundle, _with(wrong))


def _with(liquidity: Liquidity):
    def change(payload: dict) -> None:
        payload["liquidity"] = liquidity.model_dump(mode="json")

    return change


def test_the_statuses_are_unavailable_and_measured() -> None:
    assert set(CapacityStatus) == {CapacityStatus.UNAVAILABLE, CapacityStatus.MEASURED}
