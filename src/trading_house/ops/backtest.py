"""The backtester's composition shim.

``cli.py`` is the composition root for every other command and would have been
the obvious home, but it is ~1000 lines against a 558-line next-largest module;
Phase 5 put ``guard``'s wiring here for exactly that reason and this is the same
shape.

It also carries one thing the guard's wiring did not: a clock that *must* be
shared. ``RiskEngine``'s tick-freshness gate compares its own clock to the
snapshot's ``tick_time``, and during a replay the only clock that can satisfy it
is the ``ReplayClock`` the ``Backtester`` advances to each bar's
``availability_time``. Hand the engine a ``SystemClock`` instead and every
stored bar is stale by years: the run finishes with zero trades and one
``tick_stale`` rejection per proposal, which is a plausible-looking result
rather than an error -- the worst failure shape available. ``build_backtester``
constructs the clock and hands it to both, so there is no call site at which the
two can drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from trading_house.constitution.loader import LoadedConstitution
from trading_house.core.exits import ExitPolicy, Strategy
from trading_house.core.instruments import InstrumentContract
from trading_house.core.schemas import Side
from trading_house.features.engine import BarReader, FeatureEngine
from trading_house.research.backtest.engine import Backtester, ReplayClock
from trading_house.risk.engine import MARGIN_HEADROOM_MULTIPLE, RiskEngine
from trading_house.strategies.registry import registered

_REQUIRED_MARGIN: Final[Decimal] = Decimal(1)


@dataclass(frozen=True, slots=True)
class NeverBindingMargin:
    """The ``MarginPort`` a backtest has: none.

    Nothing in this repo can supply a margin requirement --
    ``InstrumentContract`` carries no leverage or margin field, and a replay has
    no account to read free margin from -- so section 8.1's free-margin headroom
    gate is disabled here rather than fed an invented number. D-5 says a cost
    this simulator cannot know is declared rather than guessed, and there is
    nothing to declare it through: a ``--free-margin`` would be a number the
    operator made up, dressed as a broker fact.

    ``1`` and ``MARGIN_HEADROOM_MULTIPLE`` are deliberately not money. They are
    the smallest pair that clears the gate, sized off the engine's own constant
    so they still clear it if that constant moves, and small enough that nobody
    reads them as a modelled account. The README states plainly that a run
    assumes margin was always available.
    """

    def free_margin(self) -> Decimal:
        return _REQUIRED_MARGIN * MARGIN_HEADROOM_MULTIPLE

    def required_margin(
        self, *, instrument_id: str, side: Side, quantity: Decimal, price: Decimal
    ) -> Decimal:
        return _REQUIRED_MARGIN


def build_strategy(strategy_id: str, *, exit_policy: ExitPolicy) -> Strategy:
    return registered(strategy_id, exit_policy=exit_policy)


def build_backtester(
    *, bars: BarReader, contract: InstrumentContract, constitution: LoadedConstitution
) -> Backtester:
    """One ``ReplayClock``, handed to both the engine and the simulator.

    The whole reason this function exists rather than six lines at a call site:
    the risk engine and the backtester must read the same clock instance, and
    nothing in either type enforces it.
    """

    clock = ReplayClock()
    return Backtester(
        bars=bars,
        features=FeatureEngine(bars),
        risk=RiskEngine(constitution.constitution, clock),
        margin=NeverBindingMargin(),
        contract=contract,
        clock=clock,
        constitution_sha256=constitution.constitution_sha256,
    )
