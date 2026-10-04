from decimal import Decimal

import pytest

from trading_house.core.errors import ConfigurationError
from trading_house.core.exits import ChandelierPolicy, FixedTargetPolicy, NoExitPolicy
from trading_house.marketdata.models import Timeframe
from trading_house.strategies.registry import REGISTERED_STRATEGY_IDS, strategy_scope


def test_session_momentum_keeps_the_scope_the_cli_constants_gave_it() -> None:
    """Exactly the values ``cli.py`` hard-coded before Phase 9; anything else
    would move its pinned outputs."""

    scope = strategy_scope("session_momentum_eurusd")

    assert scope.instrument_id == "fx.eurusd"
    assert scope.timeframe == "M15"
    assert scope.exit_arm("none") == NoExitPolicy(kind="none")
    assert scope.exit_arm("fixed_target") == FixedTargetPolicy(
        kind="fixed_target", r_multiple=Decimal("1.0")
    )
    assert scope.exit_arm("chandelier") == ChandelierPolicy(
        kind="chandelier", atr_multiple=Decimal("3.0"), min_step_points=Decimal(10)
    )


@pytest.mark.parametrize("strategy_id", sorted(REGISTERED_STRATEGY_IDS))
def test_every_scope_names_a_real_timeframe(strategy_id: str) -> None:
    """``strategies/`` may not import ``Timeframe``, so the scope carries the
    string; this is where a typo in it would be caught rather than at a run."""

    Timeframe(strategy_scope(strategy_id).timeframe)


def test_an_unregistered_strategy_has_no_scope() -> None:
    with pytest.raises(ConfigurationError):
        strategy_scope("nope")


def test_an_unknown_arm_is_refused() -> None:
    with pytest.raises(ConfigurationError):
        strategy_scope("session_momentum_eurusd").exit_arm("martingale")
