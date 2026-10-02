"""Phase 8A acceptance: the evidence store, the trial ledger, and the line
between them and the trading plane.

Eight things are asserted here, and each is one an operator would otherwise have
to take on trust from the README:

1. ``research`` is a command group, and ``research trial`` has exactly the seven
   commands the operator table documents.
2. ``TrialProtocol`` refuses a protocol with no candidate family -- the
   declaration that makes a trial countable has to exist before the trial does.
3. ``EvidenceStore`` writes a bundle and reads it back byte for byte, twice,
   without a second file.
4. Both databases are at the same Alembic head.
5. A synthetic protocol registers, records, counts and verifies through the
   *concrete* PostgreSQL store -- the production chain, the production
   append-only triggers, the production content-addressed files.
6. The three Phase 7 digests are pinned as constants and agree with the only
   independent record of them in this repository.
7. The architecture guard for ``research/`` fires on a forbidden import and
   stands down for a permitted one, so (8) is not a check that stopped looking.

Nothing here reads a developer's temporary Phase 7 files. A check that needs a
file only one machine has is not a gate, it is a note to self; the real
three-file import is an operator verification step, exercised end to end in
``tests/integration/research/test_trial_cli.py`` against artifacts it builds
itself. The digests are therefore asserted against a constant *and* against
``SESSION_MOMENTUM_SPEC.versioning``, which recorded them in Phase 7 -- never
recomputed from anything mutable.

The two database tests are marked ``integration`` individually rather than for
the module, so the other six run in the no-Docker Windows job too.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

import psycopg
import pytest
from pydantic import SecretStr, ValidationError
from typer.testing import CliRunner

from tests.acceptance.test_architecture import RESEARCH_ALLOWED, _reaches_outside
from trading_house import cli
from trading_house.core.exits import NoExitPolicy
from trading_house.core.schemas import Side
from trading_house.database.connection import open_runtime_connection
from trading_house.database.migrations import assert_at_head
from trading_house.marketdata.models import Timeframe
from trading_house.ops.ledger import evidence_sealed_event, result_recorded_event
from trading_house.research.backtest.costs import CostModel
from trading_house.research.backtest.fills import ExitKind
from trading_house.research.backtest.result import BacktestResult, SimulatedTrade
from trading_house.research.canonical import canonical_sha256
from trading_house.research.evidence import (
    CostSummary,
    EvidenceBundle,
    EvidenceProvenance,
    EvidenceStore,
)
from trading_house.research.ledger_store import PostgresTrialLedger
from trading_house.research.legacy_import import derive_realized_daily_returns
from trading_house.research.trial_ledger import (
    CostAttributionStatus,
    CostSpec,
    DataSpec,
    ExecutionSpec,
    ExecutionStartedPayload,
    HoldoutSpec,
    HoldoutState,
    LedgerEvent,
    LedgerEventType,
    RegimeSpec,
    RegistrationState,
    ReturnSeriesBasis,
    ScopeKind,
    TrialProtocol,
    TrialSpec,
    ValidationSpec,
)
from trading_house.strategies.impl.session_momentum import SESSION_MOMENTUM_SPEC

if TYPE_CHECKING:
    from ..conftest import DatabaseHarness

runner = CliRunner()

# The trial command table, in the order `research trial --help` prints it. Named
# here rather than derived, so a new command fails this file instead of quietly
# widening the operator table the README carries. Phase 8B2b added two:
# `scenarios` runs and seals a candidate's declared cost grid, and
# `scenario-report` reads one back. Phase 8D1 added three that speak in decisions:
# `decide`, `report` and `holdout`.
TRIAL_COMMANDS = (
    "register",
    "start",
    "record",
    "import-legacy",
    "show",
    "count",
    "scenarios",
    "scenario-report",
    "compounding",
    "compounding-report",
    "capacity",
    "splits",
    "validate",
    "decide",
    "report",
    "holdout",
    "verify",
)

# The three Phase 7 result digests, verbatim from the completed three-arm run
# (see the Phase 7 evidence table). An exact constant and never a recomputation:
# these name three artifacts that exist on no machine but the one that ran them,
# so the only honest thing a test can do is refuse to agree with them silently.
PHASE7_RESULT_DIGESTS = {
    "none": "a10b0fa43fc4ddb0185b3a376caab7c9369ec49e3e347d13171d31ab989f022b",
    "fixed_target": "fe5efadef7f5ea2448957e0bec507f2133755df676ade140b05a120cfb5fbc6b",
    "chandelier": "69867de09ef3993e5aabca78b225b21bb1ef82a4aa4fbcd63a9fd52dfe2e0c54",
}

_RUN_START = datetime(2024, 1, 1, tzinfo=UTC)
_REGISTERED_AT = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
_HEX = frozenset("0123456789abcdef")


# --- fixtures ----------------------------------------------------------------


def _trade() -> SimulatedTrade:
    return SimulatedTrade(
        proposal_id="p-1",
        side=Side.BUY,
        lots=Decimal("0.10"),
        entry_price=Decimal("1.10000"),
        entry_at=_RUN_START,
        exit_price=Decimal("1.10100"),
        exit_at=_RUN_START + timedelta(minutes=12),
        exit_kind=ExitKind.TIME,
        gross_pnl=Decimal("100"),
        commission=Decimal("0"),
        swap=Decimal("0"),
        net_pnl=Decimal("100"),
    )


def _result() -> BacktestResult:
    return BacktestResult(
        run_id="run-1",
        strategy_id="session_momentum",
        strategy_version="1",
        exit_policy=NoExitPolicy(kind="none"),
        constitution_sha256="a" * 64,
        contract_sha256="b" * 64,
        instrument_id="fx.eurusd",
        timeframe=Timeframe.M1,
        start=_RUN_START,
        end=_RUN_START + timedelta(hours=1),
        firm_equity=Decimal("100000"),
        cost_model=CostModel(
            commission_per_lot_per_side=Decimal("0"),
            slippage_points_per_side=Decimal("0"),
            swap_long_points_per_day=Decimal("0"),
            swap_short_points_per_day=Decimal("0"),
            triple_swap_weekday=2,
        ),
        atr_period=14,
        spread_window=20,
        defective_bar_tolerance=Fraction(0),
        trades=(_trade(),),
        rejections=(),
        bars_seen=60,
        snapshots_skipped=0,
        net_pnl=Decimal("100"),
    )


def _candidate(index: int) -> TrialSpec:
    return TrialSpec(
        trial_id=f"trial-{index}",
        spec_id=f"spec-{index}",
        rationale="declared before execution",
        parameter_space=(("window", f"value-{index}"),),
    )


def _protocol(candidates: tuple[TrialSpec, ...] | None = None) -> TrialProtocol:
    return TrialProtocol(
        protocol_id="protocol-8a",
        protocol_version="1",
        agent_run_id="run-8a",
        strategy_id="session_momentum",
        strategy_version="1",
        strategy_sha256="b" * 64,
        data=DataSpec(
            instrument_id="fx.eurusd",
            timeframe=Timeframe.M15,
            start=_RUN_START,
            end=_RUN_START + timedelta(days=90),
            dataset_sha256="a" * 64,
            point_in_time_policy="availability_time",
        ),
        execution=ExecutionSpec(
            seed="fixed",
            warmup_bars=20,
            fill_policy="pessimistic-bar",
            sizing_policy="risk-engine",
        ),
        costs=CostSpec(
            baseline=CostModel(
                commission_per_lot_per_side=Decimal("0"),
                slippage_points_per_side=Decimal("0.4"),
                swap_long_points_per_day=Decimal("-7.7"),
                swap_short_points_per_day=Decimal("2"),
                triple_swap_weekday=2,
            ),
            stress_multipliers=(Decimal("1.5"), Decimal("2")),
        ),
        validation=ValidationSpec(
            primary_metric="net_expectancy",
            wfa_train_months=24,
            wfa_validation_months=6,
            wfa_test_months=6,
            purge_hours=16,
            embargo_hours=16,
            cpcv_folds=6,
            bootstrap_replicates=10000,
            bootstrap_c=Decimal("6.7"),
            trial_count_rule="conservative-selection-lotteries",
        ),
        regimes=RegimeSpec(labels=("london", "new_york"), provenance_sha256="c" * 64),
        holdout=HoldoutSpec(state=HoldoutState.NOT_DEFINED),
        candidates=(_candidate(1), _candidate(2)) if candidates is None else candidates,
    )


def _bundle() -> EvidenceBundle:
    """A complete bundle for trial-1/attempt-1 of ``_protocol()``.

    Prospective, not legacy: this is what an operator's own run produces. The
    Phase 7 shape (``LEGACY_UNPREGISTERED``) belongs to ``import-legacy`` and is
    covered where the artifact exists.
    """

    result = _result()
    return EvidenceBundle(
        result_schema_version=1,
        trial_id="trial-1",
        attempt_id="attempt-1",
        spec_sha256=canonical_sha256(_protocol()),
        source_result_sha256=result.digest(),
        result=result,
        daily_returns=derive_realized_daily_returns(result),
        return_series_basis=ReturnSeriesBasis.REALIZED_CLOSED_TRADES,
        costs=CostSummary(
            status=CostAttributionStatus.PARTIAL,
            commission=Decimal("0"),
            swap=Decimal("0"),
            spread_cost=None,
            slippage_cost=None,
        ),
        provenance=EvidenceProvenance(
            agent_run_id=result.run_id,
            source_artifact_sha256="d" * 64,
            dataset_sha256="a" * 64,
            registered_at=_REGISTERED_AT,
            occurred_at=result.end,
            registration_state=RegistrationState.PROSPECTIVE,
            holdout_state=HoldoutState.NOT_DEFINED,
        ),
    )


def _execution_started() -> LedgerEvent:
    """The one event the deflation denominators are counted from.

    Declared here rather than taken from the CLI: the three counters only move
    for ``EXECUTION_STARTED`` and ``LEGACY_IMPORTED``, so a register/record pair
    on its own would report zeros and this check would prove nothing about
    counting.
    """

    return LedgerEvent(
        event_id=uuid5(NAMESPACE_URL, "acceptance:8a:execution-started"),
        scope_kind=ScopeKind.TRIAL,
        scope_id="trial-1",
        event_type=LedgerEventType.EXECUTION_STARTED,
        trial_id="trial-1",
        attempt_id="attempt-1",
        spec_sha256=canonical_sha256(_protocol().candidates[0]),
        occurred_at=_REGISTERED_AT,
        payload=ExecutionStartedPayload(
            event_type=LedgerEventType.EXECUTION_STARTED,
            attempt_id="attempt-1",
            execution_started_at=_REGISTERED_AT,
        ),
    )


def _ledger(dsn: str) -> PostgresTrialLedger:
    return PostgresTrialLedger(lambda: open_runtime_connection(SecretStr(dsn)))


# --- 1. the command surface --------------------------------------------------


def test_research_is_a_command_group_on_the_root_cli() -> None:
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == cli.ExitCode.OK
    assert "research" in result.stdout


@pytest.mark.parametrize("command", TRIAL_COMMANDS)
def test_every_trial_command_is_reachable_from_its_help(command: str) -> None:
    """One case per command, so a removed one names itself.

    Asserted against the printed help rather than the Typer object because the
    help is what an operator reads; rich wraps the description column, never the
    command column, so a whole-token match is safe here.
    """

    result = runner.invoke(cli.app, ["research", "trial", "--help"])

    assert result.exit_code == cli.ExitCode.OK
    assert command in result.stdout


def test_research_trial_has_exactly_the_documented_commands() -> None:
    result = runner.invoke(cli.app, ["research", "trial", "--help"])

    assert result.exit_code == cli.ExitCode.OK
    # Counted from the app rather than the help text, because ``--help`` proves a
    # command is *documented* and this proves none is undocumented-but-present --
    # a command wired up and left out of the README is the failure this catches,
    # and no amount of reading the help would show it.
    registered = {command.name for command in cli.trial_app.registered_commands}
    assert set(TRIAL_COMMANDS) == registered


# --- 2. the protocol ----------------------------------------------------------


def test_a_protocol_refuses_an_empty_candidate_family() -> None:
    """No candidates is not "one candidate whose values are unknown".

    That is the rule the whole deflation argument rests on: a trial that is not
    declared cannot be counted, and a protocol sealed with nothing in it would
    make a later run's trial count unverifiable rather than merely unknown.
    """

    with pytest.raises(ValidationError, match="at least one candidate"):
        _protocol(candidates=())


# --- 3. the evidence store ----------------------------------------------------


def test_the_evidence_store_writes_and_verifies_a_bundle(tmp_path: Path) -> None:
    bundle = _bundle()
    store = EvidenceStore(tmp_path / "evidence")

    stored = store.write(bundle)

    assert len(stored.sha256) == 64
    assert _HEX.issuperset(stored.sha256)
    # Relative to the root, because the ledger stores this string: an absolute
    # path from the writing machine is not evidence, it is a machine name.
    assert not stored.path.startswith(("/", "\\"))
    assert (store.root / stored.path).is_file()
    assert stored.size_bytes == (store.root / stored.path).stat().st_size

    store.verify(stored.sha256)
    assert store.read(stored.sha256) == bundle

    # One digest, one file, however many times it is written. This is the CAS
    # guarantee the ledger's retry-by-digest answer depends on.
    again = store.write(bundle)

    assert again == stored
    assert sorted(p.name for p in store.root.rglob("*.json")) == [f"{stored.sha256}.json"]


# --- 4/5. the two databases ---------------------------------------------------


@pytest.mark.integration
def test_both_databases_are_at_the_same_alembic_head(database: DatabaseHarness) -> None:
    """The ledger is a second *deployment target* for this repository's schema.

    Same head in both is therefore the claim, not "the research database has
    something in it": a research database one revision behind would refuse every
    trial command with a migration-mismatch exit code that says nothing about the
    ledger.
    """

    for dsn, config in (
        (database.runtime_dsn, database.alembic_config),
        (database.research_runtime_dsn, database.research_alembic_config),
    ):
        with psycopg.connect(dsn) as connection:
            assert_at_head(connection, config)


@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_a_synthetic_protocol_registers_records_counts_and_verifies(
    research_ledger_dsn: str, research_evidence_root: Path
) -> None:
    """The whole round trip through the concrete stores, not through a fake.

    The chain, the append-only triggers and the content-addressed files are the
    production ones, and the counters are read back out of the chain rather than
    asserted from the events this test built -- a counter computed from the
    events it just created would agree with itself whatever the database did.
    """

    ledger = _ledger(research_ledger_dsn)
    store = EvidenceStore(research_evidence_root)
    protocol = _protocol()

    registered = ledger.register(protocol)
    assert [trial.trial_id for trial in registered] == ["trial-1", "trial-2"]

    ledger.append(_execution_started())
    bundle = _bundle()
    stored = store.write(bundle)
    ledger.append(result_recorded_event(bundle))
    ledger.append(evidence_sealed_event(bundle, stored.sha256))

    counters = ledger.counters()
    assert counters.audit_attempts == 1
    assert counters.selection_lotteries == 1
    assert counters.effective_specifications == 1

    report = ledger.verify()
    assert report.valid is True
    assert report.checked_events == 4

    # The chain is only half the check: the digests prove the events were not
    # rewritten, and only reading the sealed file proves the evidence the chain
    # names still exists. This is what `research trial verify` does.
    store.verify(stored.sha256)

    rows = ledger.events_for("trial-2")
    assert rows == (), "a trial nobody ran has no chain rows, and inventing any is not a read"


# --- 6. the pinned Phase 7 digests -------------------------------------------


def test_the_three_phase7_digests_are_pinned_and_agree_with_the_record() -> None:
    assert set(PHASE7_RESULT_DIGESTS) == {"none", "fixed_target", "chandelier"}
    assert len(set(PHASE7_RESULT_DIGESTS.values())) == 3
    for digest in PHASE7_RESULT_DIGESTS.values():
        assert len(digest) == 64
        assert _HEX.issuperset(digest)

    # The independent record: Phase 7 wrote these three into the registered
    # strategy's own versioning string, and they are negative-result evidence
    # that a later phase must not quietly restate differently.
    for arm, digest in PHASE7_RESULT_DIGESTS.items():
        assert f"{arm}_digest={digest}" in SESSION_MOMENTUM_SPEC.versioning


def test_the_pinned_digests_agree_with_the_phase7_negative_result() -> None:
    """The digests are only evidence because the run they name lost money.

    Asserted so the constants cannot outlive the claim they support: an arm whose
    P&L the registry still records as a loss is a completed experiment, and a
    digest pinned to it is a record of a completed experiment rather than a
    promise of one.
    """

    assert "lost money after costs in every arm" in SESSION_MOMENTUM_SPEC.invalidation
    assert SESSION_MOMENTUM_SPEC.trial_count == 3


# --- 7. the architecture guard is live ----------------------------------------


def test_the_research_import_guard_fires_on_a_broker_import() -> None:
    """A research module that could open a gateway is not a research module."""

    assert _reaches_outside(
        ast.parse("from trading_house.brokers.mt5.gateway import Mt5Gateway\n"),
        RESEARCH_ALLOWED,
    )


@pytest.mark.parametrize(
    "statement",
    [
        "from trading_house.research.backtest.result import BacktestResult",
        "from trading_house.database.connection import open_runtime_connection",
    ],
)
def test_the_research_import_guard_permits_the_sealed_store_reaches(statement: str) -> None:
    assert _reaches_outside(ast.parse(statement + "\n"), RESEARCH_ALLOWED) == set()
