from decimal import Decimal

import pytest

from trading_house.core.errors import ConfigurationError
from trading_house.core.exits import FixedTargetPolicy
from trading_house.strategies.impl.session_momentum import (
    SESSION_MOMENTUM_ID,
    SESSION_MOMENTUM_SPEC,
    SessionMomentum,
)
from trading_house.strategies.registry import REGISTERED_STRATEGY_IDS, registered


def test_the_registry_contains_the_real_strategy_and_not_the_toy() -> None:
    assert frozenset({SESSION_MOMENTUM_ID}) == REGISTERED_STRATEGY_IDS
    assert "toy" not in REGISTERED_STRATEGY_IDS


def test_registered_returns_a_fresh_session_momentum() -> None:
    first = registered(SESSION_MOMENTUM_ID)
    second = registered(SESSION_MOMENTUM_ID)

    assert isinstance(first, SessionMomentum)
    assert first is not second


def test_registered_refuses_an_unknown_strategy() -> None:
    with pytest.raises(ConfigurationError):
        registered("toy")


def test_registered_applies_the_selected_exit_policy() -> None:
    policy = FixedTargetPolicy(kind="fixed_target", r_multiple=Decimal("1.0"))

    strategy = registered(SESSION_MOMENTUM_ID, exit_policy=policy)

    assert strategy.exit_policy() == policy


def test_the_registered_strategy_has_a_complete_specification() -> None:
    assert SESSION_MOMENTUM_SPEC.universe == ("fx.eurusd",)
    assert SESSION_MOMENTUM_SPEC.trial_count == 3
    assert SESSION_MOMENTUM_SPEC.trail_decision
