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


def test_session_momentum_spec_records_the_completed_phase_7_evidence() -> None:
    spec = SESSION_MOMENTUM_SPEC

    assert spec.trial_count == 3
    assert spec.trail_decision == "none"
    assert spec.cost_model_description == (
        "Commission 0.0 per lot per side (official FBS publishes no commission); "
        "slippage 0.4 points per side (predeclared prior); "
        "swap long -7.7 points/day and short +2.0 points/day (terminal audit); "
        "triple swap Wednesday; stress multiplier 1; defective tolerance 0."
    )
    assert spec.invalidation == (
        "Invalidated: the completed three-arm trial lost money after costs in every arm "
        "(none -1174.29200000015850; fixed_target(1.0R) -15578.74700000010180; "
        "chandelier(3.0 ATR, 10-point step) -25120.06200000013010); "
        "no tuning or promotion."
    )
    assert spec.versioning == (
        "strategy=session_momentum_eurusd; version=1; "
        "constitution_sha256=a87e63fb8c46912b1bc21bae3e55535b88ae4abc613cb87988399c4c28e5d58b; "
        "contract_sha256=59121ba95a21afb81e48f4de9c9358705375c678954fa2249dbbd80b41b86f90; "
        "none_digest=a10b0fa43fc4ddb0185b3a376caab7c9369ec49e3e347d13171d31ab989f022b; "
        "fixed_target_digest=fe5efadef7f5ea2448957e0bec507f2133755df676ade140b05a120cfb5fbc6b; "
        "chandelier_digest=69867de09ef3993e5aabca78b225b21bb1ef82a4aa4fbcd63a9fd52dfe2e0c54"
    )
