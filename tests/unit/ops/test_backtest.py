"""The composition shim's one load-bearing property: the shared clock.

``tests/unit/research/backtest/`` wires its own ``ReplayClock`` into both the
``RiskEngine`` and the ``Backtester`` by hand, so every engine test there is
blind to how ``backtest run`` actually wires them. This module drives the real
composition function over the same synthetic ramp, so a ``SystemClock`` reaching
the risk engine fails here rather than shipping as a run with no trades.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tests.unit.ops.test_scenarios import BARS as RAMP
from tests.unit.ops.test_scenarios import _bundle_at, _protocol
from tests.unit.research.backtest.conftest import (
    ATR_PERIOD,
    SPREAD_WINDOW,
    FakeBarReader,
    _contract,
    _cost_model,
    _loaded_constitution,
    _session_ramp,
)
from tests.unit.research.backtest.test_dataset import _reference
from trading_house.core.errors import ConfigurationError
from trading_house.core.exits import (
    ChandelierPolicy,
    ExitPolicy,
    FixedTargetPolicy,
    NoExitPolicy,
)
from trading_house.core.schemas import Side
from trading_house.marketdata.models import Bar, Timeframe
from trading_house.ops.backtest import (
    NeverBindingMargin,
    build_backtester,
    build_strategy,
    mark_to_market_bundle,
)
from trading_house.research.backtest.engine import BacktestRequest
from trading_house.research.backtest.fills import ExitKind
from trading_house.research.backtest.mark import BacktestOutcome
from trading_house.research.backtest.result import BacktestResult
from trading_house.research.canonical import canonical_sha256
from trading_house.research.evidence import EvidenceBundle
from trading_house.risk.engine import MARGIN_HEADROOM_MULTIPLE
from trading_house.strategies.impl.session_momentum import SESSION_MOMENTUM_ID

BARS = 65


def _composed_run(policy: ExitPolicy) -> BacktestResult:
    bars = _session_ramp(BARS)
    loaded = _loaded_constitution()
    tester = build_backtester(bars=FakeBarReader(bars), contract=_contract(), constitution=loaded)
    return tester.run(
        BacktestRequest(
            strategy=build_strategy(SESSION_MOMENTUM_ID, exit_policy=policy),
            instrument_id="fx.eurusd",
            timeframe=Timeframe.M15,
            start=bars[0].event_time,
            end=bars[-1].event_time,
            firm_equity=Decimal("100000"),
            cost_model=_cost_model(),
            atr_period=ATR_PERIOD,
            spread_window=SPREAD_WINDOW,
        )
    ).result


def test_the_composed_backtester_shares_its_clock_with_its_risk_engine() -> None:
    """The real strategy must trade through the composed risk path."""

    bars = _session_ramp(BARS)
    result = _composed_run(NoExitPolicy(kind="none"))

    assert result.rejections == ()
    assert result.constitution_sha256 == _loaded_constitution().constitution_sha256
    assert len(result.trades) == 1
    assert result.bars_seen == BARS
    trade = result.trades[0]
    assert trade.exit_kind is ExitKind.TIME
    assert trade.entry_at == bars[29].event_time
    assert trade.exit_at == bars[-1].event_time


def test_the_real_strategy_actually_trades_in_every_ab_arm() -> None:
    policies = (
        NoExitPolicy(kind="none"),
        FixedTargetPolicy(kind="fixed_target", r_multiple=Decimal("1.0")),
        ChandelierPolicy(
            kind="chandelier", atr_multiple=Decimal("3.0"), min_step_points=Decimal(10)
        ),
    )

    results = tuple(_composed_run(policy) for policy in policies)

    assert all(result.trades for result in results)
    assert tuple(result.exit_policy for result in results) == policies


def test_the_ops_registry_builds_the_real_strategy_and_refuses_the_toy() -> None:
    strategy = build_strategy(SESSION_MOMENTUM_ID, exit_policy=NoExitPolicy(kind="none"))

    assert strategy.id == SESSION_MOMENTUM_ID
    with pytest.raises(ConfigurationError):
        build_strategy("toy", exit_policy=NoExitPolicy(kind="none"))


@pytest.mark.parametrize(
    "policy",
    [
        NoExitPolicy(kind="none"),
        FixedTargetPolicy(kind="fixed_target", r_multiple=Decimal("1.0")),
        ChandelierPolicy(
            kind="chandelier", atr_multiple=Decimal("3.0"), min_step_points=Decimal(10)
        ),
    ],
)
def test_the_ops_registry_applies_the_selected_arm(
    policy: NoExitPolicy | FixedTargetPolicy | ChandelierPolicy,
) -> None:
    strategy = build_strategy(SESSION_MOMENTUM_ID, exit_policy=policy)

    assert strategy.exit_policy() == policy


def test_the_simulated_margin_port_never_binds() -> None:
    """It is not an account: it is the headroom gate switched off, and the one
    thing it must never do is reject. ``required_margin`` must also stay
    strictly positive -- the engine rejects a non-positive requirement
    outright, which would turn "not modelled" into "every proposal refused"."""

    margin = NeverBindingMargin()
    required = margin.required_margin(
        instrument_id="fx.eurusd",
        side=Side.BUY,
        quantity=Decimal("1000000"),
        price=Decimal("1.10000"),
    )

    assert required > 0
    assert margin.free_margin() >= required * MARGIN_HEADROOM_MULTIPLE


# --- Phase 8E: the bundle carries the digest the engine computed ---------------------------

_PRE_8E_BUNDLE_AT_1_0 = "096285bf69b7e21f72204b028381369ec77b68104650fcca1ad670b4d0ffeffd"
_PRE_8E_BUNDLE_AT_1_5 = "4884c76ad2d29a27ebdd7b5ffe87a4485b6b5d445ebd444c168de89550dc66ac"
"""``canonical_sha256`` of ``test_scenarios._bundle_at(_protocol(), BARS, level, trial_id=...)``
as built by the code at the commit before Phase 8E (``a843646``), where every bundle's
provenance carried no dataset hash. Read off that commit's tree, not from this one."""


def _outcome_and_bars() -> tuple[BacktestOutcome, tuple[Bar, ...]]:
    bars = _session_ramp(BARS)
    outcome = build_backtester(
        bars=FakeBarReader(bars), contract=_contract(), constitution=_loaded_constitution()
    ).run(
        BacktestRequest(
            strategy=build_strategy(SESSION_MOMENTUM_ID, exit_policy=NoExitPolicy(kind="none")),
            instrument_id="fx.eurusd",
            timeframe=Timeframe.M15,
            start=bars[0].event_time,
            end=bars[-1].event_time,
            firm_equity=Decimal("100000"),
            cost_model=_cost_model(),
            atr_period=ATR_PERIOD,
            spread_window=SPREAD_WINDOW,
        )
    )
    return outcome, bars


def _seal(outcome: BacktestOutcome) -> EvidenceBundle:
    return mark_to_market_bundle(
        outcome,
        trial_id="trial-1",
        attempt_id="attempt-1",
        spec_sha256="a" * 64,
        agent_run_id="run-1",
        occurred_at=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
        registered_at=datetime(2026, 9, 20, 13, 0, tzinfo=UTC),
    )


def test_a_bundle_from_a_real_run_carries_the_digest_of_the_bars_it_replayed() -> None:
    outcome, bars = _outcome_and_bars()
    expected = _reference(bars)

    assert outcome.dataset_sha256 == expected
    assert _seal(outcome).provenance.dataset_sha256 == expected


def test_an_outcome_without_a_digest_seals_none() -> None:
    outcome, _ = _outcome_and_bars()

    bare = outcome.model_copy(update={"dataset_sha256": None})

    assert _seal(bare).provenance.dataset_sha256 is None


@pytest.mark.parametrize(
    ("level", "pinned"),
    [(Decimal(1), _PRE_8E_BUNDLE_AT_1_0), (Decimal("1.5"), _PRE_8E_BUNDLE_AT_1_5)],
)
def test_a_bundle_whose_provenance_carries_no_digest_keeps_its_pre_8e_bytes(
    level: Decimal, pinned: str
) -> None:
    """Nothing but the provenance field moved: clear it and the bundle is the one 8B/8D sealed."""

    bundle = _bundle_at(_protocol(), RAMP, level, trial_id="trial-1", dataset_sha256=None)

    assert bundle.provenance.dataset_sha256 is None
    assert canonical_sha256(bundle) == pinned
