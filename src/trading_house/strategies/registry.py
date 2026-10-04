"""The registry of strategies that have a complete specification, and the one
place each strategy's backtest scope is declared.

The scope -- instrument, timeframe and the parameters of the three exit arms --
lives beside the strategy rather than in ``cli.py`` so a second strategy can
exist at all, and stays off the command line for the reason it always was: an
operator who can choose the data after seeing an outcome is running a sweep.
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, cast

from trading_house.core.errors import ConfigurationError
from trading_house.core.exits import (
    ChandelierPolicy,
    ExitPolicy,
    FixedTargetPolicy,
    NoExitPolicy,
    Strategy,
)
from trading_house.core.values import CanonicalModel, InstrumentId, NonEmptyStr
from trading_house.strategies.impl.session_momentum import (
    SESSION_MOMENTUM_ID,
    SESSION_MOMENTUM_SPEC,
    SessionMomentum,
)
from trading_house.strategies.spec import StrategySpec


class StrategyScope(CanonicalModel):
    instrument_id: InstrumentId
    timeframe: NonEmptyStr
    """A ``Timeframe`` value. A string because ``strategies/`` may not import
    ``marketdata``; ``cli.py`` converts it, and a test proves every one converts."""

    fixed_target: FixedTargetPolicy
    chandelier: ChandelierPolicy

    def exit_arm(self, name: str) -> ExitPolicy:
        if name == "none":
            return NoExitPolicy(kind="none")
        if name == "fixed_target":
            return self.fixed_target
        if name == "chandelier":
            return self.chandelier
        raise ConfigurationError()


@dataclass(frozen=True, slots=True)
class _Entry:
    spec: StrategySpec
    factory: Callable[[ExitPolicy | None], object]
    scope: StrategyScope


_SWING_CHANDELIER: Final = ChandelierPolicy(
    kind="chandelier", atr_multiple=Decimal("3.0"), min_step_points=Decimal(10)
)

_REGISTRY: Final[dict[str, _Entry]] = {
    SESSION_MOMENTUM_ID: _Entry(
        spec=SESSION_MOMENTUM_SPEC,
        factory=SessionMomentum,
        scope=StrategyScope(
            instrument_id="fx.eurusd",
            timeframe="M15",
            fixed_target=FixedTargetPolicy(kind="fixed_target", r_multiple=Decimal("1.0")),
            chandelier=_SWING_CHANDELIER,
        ),
    ),
}
REGISTERED_STRATEGY_IDS: Final[frozenset[str]] = frozenset(_REGISTRY)


def _entry(strategy_id: str) -> _Entry:
    try:
        return _REGISTRY[strategy_id]
    except KeyError:
        raise ConfigurationError() from None


def registered(strategy_id: str, *, exit_policy: ExitPolicy | None = None) -> Strategy:
    return cast(Strategy, _entry(strategy_id).factory(exit_policy))


def strategy_scope(strategy_id: str) -> StrategyScope:
    return _entry(strategy_id).scope
