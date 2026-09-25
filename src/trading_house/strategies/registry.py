"""The registry of strategies that have a complete specification."""

from collections.abc import Callable
from typing import Final, cast

from trading_house.core.errors import ConfigurationError
from trading_house.core.exits import ExitPolicy, Strategy
from trading_house.strategies.impl.session_momentum import (
    SESSION_MOMENTUM_ID,
    SESSION_MOMENTUM_SPEC,
    SessionMomentum,
)
from trading_house.strategies.spec import StrategySpec

_REGISTRY: Final[dict[str, tuple[StrategySpec, Callable[[ExitPolicy | None], object]]]] = {
    SESSION_MOMENTUM_ID: (SESSION_MOMENTUM_SPEC, SessionMomentum),
}
REGISTERED_STRATEGY_IDS: Final[frozenset[str]] = frozenset(_REGISTRY)


def registered(strategy_id: str, *, exit_policy: ExitPolicy | None = None) -> Strategy:
    try:
        _spec, factory = _REGISTRY[strategy_id]
    except KeyError:
        raise ConfigurationError() from None
    return cast(Strategy, factory(exit_policy))
