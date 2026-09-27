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

``mark_to_market_bundle`` is here for the same reason ``ops/ledger.py`` holds the
trial events rather than ``cli.py``: a payload shape is not the CLI's to invent,
and the questions a bundle answers -- what the costs were, where the series came
from, what is *not* known about it -- are ones about the run, not about how it
was typed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from trading_house.constitution.loader import LoadedConstitution
from trading_house.core.exits import ExitPolicy, Strategy
from trading_house.core.instruments import InstrumentContract
from trading_house.core.schemas import Side
from trading_house.features.engine import BarReader
from trading_house.research.backtest.engine import Backtester, ReplayClock
from trading_house.research.backtest.mark import BacktestOutcome, derive_daily_returns
from trading_house.research.canonical import canonical_sha256
from trading_house.research.evidence import CostSummary, EvidenceBundle, EvidenceProvenance
from trading_house.research.trial_ledger import (
    CostAttributionStatus,
    HoldoutState,
    RegistrationState,
    ReturnSeriesBasis,
)
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
        risk=RiskEngine(constitution.constitution, clock),
        margin=NeverBindingMargin(),
        contract=contract,
        clock=clock,
        constitution_sha256=constitution.constitution_sha256,
    )


def mark_to_market_bundle(
    outcome: BacktestOutcome,
    *,
    trial_id: str,
    attempt_id: str,
    spec_sha256: str,
    agent_run_id: str,
    occurred_at: datetime,
    registered_at: datetime,
) -> EvidenceBundle:
    """The bundle ``research trial record`` seals for one mark-to-market run.

    ``costs`` is PARTIAL and never COMPLETE: 8B1 does not separate spread from
    slippage, because the fill model folds both into the entry price and
    discards the components. Writing a zero for either would turn an unmeasured
    term into a measured one, which is the substitution this framework exists
    to prevent.

    ``dataset_sha256`` is None rather than a digest of the bar store. 8B1 does
    not compute one, and an unavailable hash is the honest record; fabricating
    one from a query the store cannot reproduce is what the legacy importer
    refuses to do.

    ``source_artifact_sha256`` is the domain-separated canonical digest of the
    result, not of a file: ``backtest run`` builds this bundle in memory and
    writes no artifact, so there is no file to hash. It is a real digest of real
    bytes and it is *not* the same value as ``source_result_sha256``, which is
    the result's own declaration-ordered digest -- two different serialisations
    of the same model, deliberately.
    """

    result = outcome.result
    return EvidenceBundle(
        result_schema_version=1,
        trial_id=trial_id,
        attempt_id=attempt_id,
        spec_sha256=spec_sha256,
        source_result_sha256=result.digest(),
        result=result,
        daily_returns=derive_daily_returns(
            outcome.equity, first_day=result.start.date(), last_day=result.end.date()
        ),
        return_series_basis=ReturnSeriesBasis.MARK_TO_MARKET,
        costs=CostSummary(
            status=CostAttributionStatus.PARTIAL,
            commission=sum((trade.commission for trade in result.trades), Decimal(0)),
            swap=sum((trade.swap for trade in result.trades), Decimal(0)),
            spread_cost=None,
            slippage_cost=None,
        ),
        provenance=EvidenceProvenance(
            agent_run_id=agent_run_id,
            source_artifact_sha256=canonical_sha256(result),
            dataset_sha256=None,
            registered_at=registered_at,
            occurred_at=occurred_at,
            registration_state=RegistrationState.PROSPECTIVE,
            holdout_state=HoldoutState.NOT_DEFINED,
        ),
    )
