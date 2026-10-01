"""The seven research trial commands, against a real second PostgreSQL database.

Every command here is a composition root: it reads settings, opens the research
database, and calls one service. A unit test can prove the exit-code mapping and
the JSON shape by faking all three, which is exactly why faking all three proves
nothing about whether ``research trial record`` writes the evidence it then seals
or ``import-legacy`` is idempotent *in a database that refuses duplicate events*.
So nothing in this file is faked: the chain, the append-only triggers and the
content-addressed store are the production ones.

Two things only a real database can catch, and both are here for that reason:

* the import's idempotency. ``import_phase7_artifact`` reads its retry answer out
  of a ``LedgerRecord`` row -- the jsonb projection of an event the database
  computed the hash of -- so the retry branch is unreachable through a fake.
* the privilege boundary. The runtime role holds ``SELECT`` and ``EXECUTE`` and
  no ``INSERT``, and that is what the chain's append-only guarantee rests on.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from pydantic import SecretStr
from typer.testing import CliRunner

from tests.conftest import DatabaseHarness
from trading_house import cli
from trading_house.core.exits import NoExitPolicy
from trading_house.core.schemas import Side
from trading_house.database.connection import open_runtime_connection
from trading_house.marketdata.models import Timeframe
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
    HoldoutSpec,
    HoldoutState,
    LedgerEventType,
    RegimeSpec,
    RegistrationState,
    ReturnSeriesBasis,
    TrialProtocol,
    TrialSpec,
    ValidationSpec,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("isolated_research_ledger"),
]

runner = CliRunner()

# The two UTC days the fixture's trades fall on, and the moment the importer is
# told to declare as its registration clock. A fixed value rather than a read of
# the wall clock is the whole point: it is what makes a retry byte-identical.
IMPORT_CLOCK = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
_IMPORT_CLOCK_ARGUMENT = "2026-03-01T12:00:00"
# The same reasoning for ``start``: a start event's canonical bytes include its
# declared clock, so a retry only matches the first run's bytes if it declares the
# same one. Two values, because a second start under a *different* clock is the
# conflict case and has to be typeable from the same helper.
_START_CLOCK_ARGUMENT = "2026-03-01T13:00:00"
_OTHER_START_CLOCK_ARGUMENT = "2026-03-01T14:00:00"
_RUN_START = datetime(2024, 1, 1, tzinfo=UTC)


# --- fixtures ----------------------------------------------------------------


def _trade(**overrides: Any) -> SimulatedTrade:
    fields: dict[str, Any] = {
        "proposal_id": "p-1",
        "side": Side.BUY,
        "lots": Decimal("0.10"),
        "entry_price": Decimal("1.10000"),
        "entry_at": _RUN_START,
        "exit_price": Decimal("1.10100"),
        "exit_at": _RUN_START + timedelta(minutes=12),
        "exit_kind": ExitKind.TIME,
        "gross_pnl": Decimal("100"),
        "commission": Decimal("0"),
        "swap": Decimal("0"),
        "net_pnl": Decimal("100"),
    }
    return SimulatedTrade(**{**fields, **overrides})


def _result(**overrides: Any) -> BacktestResult:
    trades = overrides.pop("trades", (_trade(),))
    fields: dict[str, Any] = {
        "run_id": "run-1",
        "strategy_id": "session_momentum",
        "strategy_version": "1",
        "exit_policy": NoExitPolicy(kind="none"),
        "constitution_sha256": "a" * 64,
        "contract_sha256": "b" * 64,
        "instrument_id": "fx.eurusd",
        "timeframe": Timeframe.M1,
        "start": _RUN_START,
        "end": _RUN_START + timedelta(hours=1),
        "firm_equity": Decimal("100000"),
        "cost_model": CostModel(
            commission_per_lot_per_side=Decimal("0"),
            slippage_points_per_side=Decimal("0"),
            swap_long_points_per_day=Decimal("0"),
            swap_short_points_per_day=Decimal("0"),
            triple_swap_weekday=2,
        ),
        "atr_period": 14,
        "spread_window": 20,
        "defective_bar_tolerance": Fraction(0),
        "trades": trades,
        "rejections": (),
        "bars_seen": 60,
        "snapshots_skipped": 0,
        "net_pnl": sum((trade.net_pnl for trade in trades), Decimal(0)),
    }
    return BacktestResult(**{**fields, **overrides})


def _candidate(index: int) -> TrialSpec:
    return TrialSpec(
        trial_id=f"trial-{index}",
        spec_id=f"spec-{index}",
        rationale="declared before execution",
        parameter_space=(("window", f"value-{index}"),),
    )


def _protocol() -> TrialProtocol:
    return TrialProtocol(
        protocol_id="protocol-cli",
        protocol_version="1",
        agent_run_id="run-cli",
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
                stress_multiplier=Decimal("1"),
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
        candidates=(_candidate(1), _candidate(2)),
    )


def declared_spec_sha256(trial_id: str = "trial-1") -> str:
    """The digest a start for this trial must carry: its declared candidate's.

    The one place a test takes a start's digest from, because the ledger refuses
    any other (Phase 8A.1). It is the ``TrialSpec``'s digest and not the
    protocol's: a protocol's digest is a different kind of address.
    """

    (candidate,) = (c for c in _protocol().candidates if c.trial_id == trial_id)
    return canonical_sha256(candidate)


def _bundle(trial_id: str, attempt_id: str, result: BacktestResult) -> EvidenceBundle:
    return EvidenceBundle(
        result_schema_version=1,
        trial_id=trial_id,
        attempt_id=attempt_id,
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
            registered_at=IMPORT_CLOCK,
            occurred_at=result.end,
            registration_state=RegistrationState.PROSPECTIVE,
            holdout_state=HoldoutState.NOT_DEFINED,
        ),
    )


def _write_json(path: Path, model: Any) -> Path:
    path.write_text(model.model_dump_json(), encoding="utf-8")
    return path


def _write_phase7_artifact(path: Path, result: BacktestResult) -> Path:
    """The outer document ``trading-house backtest run`` wrote in Phase 7."""

    path.write_text(
        json.dumps(
            {
                "status": "ok",
                "result": json.loads(result.model_dump_json()),
                "digest": result.digest(),
                "margin_modelled": False,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _drop_research_schema(database: DatabaseHarness) -> None:
    """Leave the research database with no schema at all.

    What a host that has never run ``alembic upgrade head`` looks like, and the
    state ``assert_at_head`` exists to refuse. ``isolated_research_ledger``
    re-upgrades the database in its teardown, whether this test passes or fails.
    """

    command.downgrade(database.research_alembic_config, "base")


def _store(root: Path) -> EvidenceStore:
    return EvidenceStore(root)


def _ledger(dsn: str) -> PostgresTrialLedger:
    return PostgresTrialLedger(lambda: open_runtime_connection(SecretStr(dsn)))


def _register(tmp_path: Path) -> dict[str, Any]:
    protocol = _write_json(tmp_path / "p.json", _protocol())
    result = runner.invoke(
        cli.app,
        ["research", "trial", "register", "--protocol", str(protocol)],
    )
    assert result.exit_code == cli.ExitCode.OK, result.stderr
    return json.loads(result.stdout)


def _start(
    *,
    trial_id: str = "trial-1",
    attempt_id: str = "attempt-1",
    spec_sha256: str | None = None,
    started_at: str | None = None,
) -> Any:
    """``start`` as an operator runs it, with no assertion on the outcome.

    Returned rather than asserted so the refusal and the retry cases can read the
    exit code themselves. ``--spec-sha256`` defaults to the trial's declared
    candidate digest, which is the only value the ledger accepts, and
    ``--started-at`` is left off entirely unless the case is about the declared
    clock -- the default-clock invocation is the one most operators will run.
    """

    argv = [
        "research",
        "trial",
        "start",
        "--trial-id",
        trial_id,
        "--attempt-id",
        attempt_id,
        "--spec-sha256",
        spec_sha256 or declared_spec_sha256(trial_id),
    ]
    if started_at is not None:
        argv += ["--started-at", started_at]
    return runner.invoke(cli.app, argv)


def _record(
    tmp_path: Path, *, trial_id: str = "trial-1", attempt_id: str = "attempt-1"
) -> dict[str, Any]:
    bundle = _write_json(tmp_path / "bundle.json", _bundle(trial_id, attempt_id, _result()))
    result = runner.invoke(
        cli.app,
        [
            "research",
            "trial",
            "record",
            "--trial-id",
            trial_id,
            "--attempt-id",
            attempt_id,
            "--evidence",
            str(bundle),
        ],
    )
    assert result.exit_code == cli.ExitCode.OK, result.stderr
    return json.loads(result.stdout)


def _import_legacy(path: Path, clock: str = _IMPORT_CLOCK_ARGUMENT) -> dict[str, Any]:
    result = runner.invoke(
        cli.app,
        ["research", "trial", "import-legacy", "--artifact", str(path), "--registered-at", clock],
    )
    assert result.exit_code == cli.ExitCode.OK, result.stderr
    return json.loads(result.stdout)


def _import_legacy_on_the_default_clock(path: Path) -> Any:
    """The command as an operator runs it: no ``--registered-at`` at all."""

    return runner.invoke(cli.app, ["research", "trial", "import-legacy", "--artifact", str(path)])


def _verify() -> Any:
    return runner.invoke(cli.app, ["research", "trial", "verify"])


# --- register ----------------------------------------------------------------


def test_register_reports_the_declared_candidate_family(tmp_path: Path, research_env: Path) -> None:
    payload = _register(tmp_path)

    assert payload["status"] == "ok"
    assert payload["protocol_id"] == "protocol-cli"
    assert payload["trial_count"] == 2
    assert payload["trial_ids"] == ["trial-1", "trial-2"]


def test_registering_the_same_protocol_twice_adds_no_second_candidate(
    tmp_path: Path, research_env: Path, research_ledger_dsn: str
) -> None:
    """Design 5.9: a repeated registration cannot silently become a new candidate.

    The event id is derived from the protocol's canonical digest, so the retry is
    recognised by id and the chain keeps one row -- which is what keeps a
    repeated registration from quietly widening the deflation denominator.
    """

    _register(tmp_path)
    _register(tmp_path)

    records = _ledger(research_ledger_dsn).events()
    assert [record.event_type for record in records] == [LedgerEventType.PREREGISTERED]
    assert _verify().exit_code == cli.ExitCode.OK


# --- start -------------------------------------------------------------------


def test_start_moves_all_three_denominators(tmp_path: Path, research_env: Path) -> None:
    """The regression this command exists for: ``count`` used to report zeros.

    A ``register`` + ``record`` pair emits ``PREREGISTERED``, ``RESULT_RECORDED``
    and ``EVIDENCE_SEALED``, and none of those three is one of the event types
    ``trial_counters`` reads -- so the operator who followed the README verbatim
    was told ``audit_attempts=0, selection_lotteries=0, effective_specifications=0``
    as a fact about their own record. ``start`` is the only event type that moves
    the denominators, and this is the assertion that fails the day it stops.
    """

    _register(tmp_path)
    started = _start()

    assert started.exit_code == cli.ExitCode.OK, started.stderr
    counters = runner.invoke(cli.app, ["research", "trial", "count"])
    assert json.loads(counters.stdout) == {
        "status": "ok",
        "audit_attempts": 1,
        "selection_lotteries": 1,
        "effective_specifications": 1,
    }


def test_start_refuses_a_trial_the_protocol_never_declared(
    tmp_path: Path, research_env: Path, research_ledger_dsn: str
) -> None:
    """Fail-closed: the denominator is not inflatable by anybody.

    Without this the deflation argument is worth nothing -- an operator could
    start attempts against trials no protocol ever sealed and the audit count
    would rise for work nobody declared. The store's containment check is the
    boundary, so the event count afterwards is the half of the assertion that
    distinguishes "refused" from "appended something invisible".
    """

    _register(tmp_path)
    before = len(_ledger(research_ledger_dsn).events())

    result = _start(
        trial_id="trial-never-declared",
        attempt_id="attempt-9",
        spec_sha256=declared_spec_sha256(),
    )

    assert result.exit_code == cli.ExitCode.TRIAL_LEDGER_APPEND
    assert len(_ledger(research_ledger_dsn).events()) == before
    assert _verify().exit_code == cli.ExitCode.OK


def test_start_refuses_a_digest_its_trial_never_declared_and_writes_nothing(
    tmp_path: Path, research_env: Path, research_ledger_dsn: str
) -> None:
    """Phase 8A.1: the ledger vouches for the digest, and the CLI inherits the refusal.

    The digest is a real one -- the protocol's own, and the other candidate's --
    so what is refused is that *this trial* never declared it, not that it is
    malformed. Exit 15 is the ledger's existing append-refusal code, and the
    event count and counters afterwards are what separate "refused" from "wrote
    something the operator cannot see".
    """

    _register(tmp_path)
    before = _ledger(research_ledger_dsn).events()

    for digest in (canonical_sha256(_protocol()), declared_spec_sha256("trial-2"), "d" * 64):
        result = _start(spec_sha256=digest)
        assert result.exit_code == cli.ExitCode.TRIAL_LEDGER_APPEND, result.stderr
        assert result.stdout == ""
        assert json.loads(result.stderr) == {
            "status": "error",
            "detail": "trial ledger append failed",
        }

    assert _ledger(research_ledger_dsn).events() == before
    counters = runner.invoke(cli.app, ["research", "trial", "count"])
    assert json.loads(counters.stdout)["effective_specifications"] == 0
    assert _verify().exit_code == cli.ExitCode.OK


def test_starting_the_same_attempt_twice_appends_one_event(
    tmp_path: Path, research_env: Path, research_ledger_dsn: str
) -> None:
    """A retried ``start`` is recognised, appends nothing, and moves no denominator.

    The event id is derived from the trial and the attempt alone, so the retry
    carries the same id; what makes the chain recognise it instead of calling it a
    conflict is that its canonical bytes are *identical* to the bytes already
    stored, and the declared clock is the only part of those bytes the operator
    can repeat. So the retry below passes the same ``--started-at`` deliberately:
    a stable timestamp is what makes this idempotent, and reading the clock again
    here would "simplify" the command straight back to a refusal on every retry.
    """

    _register(tmp_path)

    first = _start(started_at=_START_CLOCK_ARGUMENT)
    second = _start(started_at=_START_CLOCK_ARGUMENT)

    assert first.exit_code == cli.ExitCode.OK, first.stderr
    assert second.exit_code == cli.ExitCode.OK, second.stderr
    events = _ledger(research_ledger_dsn).events()
    assert [record.event_type for record in events] == [
        LedgerEventType.PREREGISTERED,
        LedgerEventType.EXECUTION_STARTED,
    ]
    # The retry is not a second draw from the search space, so the denominators
    # are exactly what one start would have produced.
    counters = runner.invoke(cli.app, ["research", "trial", "count"])
    assert json.loads(counters.stdout) == {
        "status": "ok",
        "audit_attempts": 1,
        "selection_lotteries": 1,
        "effective_specifications": 1,
    }
    assert _verify().exit_code == cli.ExitCode.OK


def test_a_second_start_under_a_different_clock_is_refused_as_a_conflict(
    tmp_path: Path, research_env: Path, research_ledger_dsn: str
) -> None:
    """The other half of the contract: a different body under the same id stays refused.

    The retry above is recognised because its bytes match what the chain already
    holds. Were a different ``--started-at`` tolerated too, the append function
    could no longer tell an operator re-running one command from one rewriting the
    record under an id it already owns, and the first writer's claim would be
    worthless. This is the case that keeps conflict detection honest.
    """

    _register(tmp_path)

    first = _start(started_at=_START_CLOCK_ARGUMENT)
    refused = _start(started_at=_OTHER_START_CLOCK_ARGUMENT)

    assert first.exit_code == cli.ExitCode.OK, first.stderr
    assert refused.exit_code == cli.ExitCode.TRIAL_LEDGER_APPEND, refused.stderr
    assert [record.event_type for record in _ledger(research_ledger_dsn).events()] == [
        LedgerEventType.PREREGISTERED,
        LedgerEventType.EXECUTION_STARTED,
    ]
    assert _verify().exit_code == cli.ExitCode.OK


# --- record ------------------------------------------------------------------


def test_record_on_an_unmigrated_ledger_leaves_no_evidence_behind(
    tmp_path: Path, research_env: Path, database: DatabaseHarness
) -> None:
    """The migration check runs before the first byte is written.

    A host that has never run ``alembic upgrade head`` cannot record the seal
    either, so writing first would leave a document on disk that no ledger row
    references and that the operator was told nothing about. The order is the
    whole assertion here: the exit code alone would be satisfied either way.
    """

    bundle = _write_json(tmp_path / "bundle.json", _bundle("trial-1", "attempt-1", _result()))
    _drop_research_schema(database)

    result = runner.invoke(
        cli.app,
        [
            "research",
            "trial",
            "record",
            "--trial-id",
            "trial-1",
            "--attempt-id",
            "attempt-1",
            "--evidence",
            str(bundle),
        ],
    )

    assert result.exit_code == cli.ExitCode.MIGRATION
    assert not list(research_env.rglob("*.json"))


def test_record_writes_the_bundle_and_seals_its_digest(
    tmp_path: Path, research_env: Path, research_ledger_dsn: str
) -> None:
    """The whole point of the command: bytes on disk, and a chain that names them.

    ``EVIDENCE_SEALED`` is keyed on the digest rather than the attempt, so a
    reader can hold the digest and find the file -- and the event's payload has to
    carry the digest the store actually produced, not one recomputed here.
    """

    _register(tmp_path)
    payload = _record(tmp_path)
    ledger = _ledger(research_ledger_dsn)

    assert payload["trial_id"] == "trial-1"
    assert [record.event_type for record in ledger.events()] == [
        LedgerEventType.PREREGISTERED,
        LedgerEventType.RESULT_RECORDED,
        LedgerEventType.EVIDENCE_SEALED,
    ]
    assert _store(research_env).read(payload["evidence_sha256"]).trial_id == "trial-1"
    assert _verify().exit_code == cli.ExitCode.OK


def test_recording_the_same_attempt_twice_is_idempotent(
    tmp_path: Path, research_env: Path, research_ledger_dsn: str
) -> None:
    """Both event ids are derived from content, so a retried ``record`` adds nothing.

    Nothing in the command reads a clock: the events take their occurrence time
    from the bundle's own provenance. An event whose bytes changed between two
    attempts at recording it would be refused by the chain as a conflict, and the
    operator would be told to retry something that can never succeed.
    """

    _register(tmp_path)
    first = _record(tmp_path)
    second = _record(tmp_path)

    assert second["evidence_sha256"] == first["evidence_sha256"]
    assert len(_ledger(research_ledger_dsn).events()) == 3
    assert _verify().exit_code == cli.ExitCode.OK


def test_record_refuses_a_bundle_whose_identity_disagrees_with_the_options(
    tmp_path: Path, research_env: Path, research_ledger_dsn: str
) -> None:
    """The bundle is the single source of identity, and the options only restate it.

    Without this the command would seal a bundle under a trial the operator named
    and an attempt the database never checked -- a ledger row whose ``trial_id``
    column and whose sealed payload disagree, and nothing would notice until
    somebody read the evidence back.
    """

    _register(tmp_path)
    _write_json(tmp_path / "bundle.json", _bundle("trial-1", "attempt-1", _result()))

    result = runner.invoke(
        cli.app,
        [
            "research",
            "trial",
            "record",
            "--trial-id",
            "trial-2",
            "--attempt-id",
            "attempt-1",
            "--evidence",
            str(tmp_path / "bundle.json"),
        ],
    )

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert [record.event_type for record in _ledger(research_ledger_dsn).events()] == [
        LedgerEventType.PREREGISTERED
    ]


def test_record_refuses_a_trial_the_protocol_never_declared(
    tmp_path: Path, research_env: Path, research_ledger_dsn: str
) -> None:
    """A result recorded against an undeclared trial is a refused append.

    The store's containment check is the boundary, and the command must not paper
    over it by writing the evidence and then reporting success. The empty root is
    the half of the assertion that a unit test with a fake ledger could not make:
    the refusal has to arrive *before* the write, because a sealed document
    nothing points at is the one artefact of a refused trial that no later reader
    would recognise as a mistake.
    """

    _register(tmp_path)
    _write_json(tmp_path / "bundle.json", _bundle("trial-never-declared", "attempt-9", _result()))

    result = runner.invoke(
        cli.app,
        [
            "research",
            "trial",
            "record",
            "--trial-id",
            "trial-never-declared",
            "--attempt-id",
            "attempt-9",
            "--evidence",
            str(tmp_path / "bundle.json"),
        ],
    )

    assert result.exit_code == cli.ExitCode.TRIAL_LEDGER_APPEND
    assert not list(research_env.rglob("*.json"))
    assert len(_ledger(research_ledger_dsn).events()) == 1


# --- the two-database boundary -------------------------------------------------


def test_a_trial_command_refuses_a_research_dsn_naming_the_main_database(
    tmp_path: Path,
    research_env: Path,
    research_ledger_dsn: str,
    database: DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The misconfiguration no downstream check can see.

    With both DSNs on one database every other property this phase promises still
    holds: the ledger tables exist, the head is a chain, ``verify`` answers valid,
    and every trial command reports success. What is false is the claim the second
    database exists for, and once the connection is open there is no evidence left
    to notice -- so the refusal has to happen in the composition boundary, and it
    has to happen before the evidence bytes are written.

    ``research_env`` has already set the two DSNs correctly; the override here
    reproduces the operator's mistake on top of that, which is why the assertion
    is a *changed* environment rather than a specially built one.
    """

    _register(tmp_path)
    bundle = _write_json(tmp_path / "bundle.json", _bundle("trial-1", "attempt-1", _result()))
    # The main database, spelled with the research role's own credentials, so the
    # only thing wrong with it is the one thing under test.
    monkeypatch.setenv("TRADING_HOUSE_RESEARCH_LEDGER_DSN", database.runtime_dsn)

    result = runner.invoke(
        cli.app,
        [
            "research",
            "trial",
            "record",
            "--trial-id",
            "trial-1",
            "--attempt-id",
            "attempt-1",
            "--evidence",
            str(bundle),
        ],
    )

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert not list(research_env.rglob("*.json"))
    assert research_ledger_dsn not in result.stderr


# --- import-legacy -----------------------------------------------------------


def test_import_legacy_is_idempotent_and_locks_the_declared_clock(
    tmp_path: Path, research_env: Path, research_ledger_dsn: str
) -> None:
    """Design 5.7: a second import adds no trial, attempt, or selection candidate.

    The retry answers from the row the database stored, so this exercises the
    production read path -- ``events_for`` returns ``LedgerRecord`` with the
    payload still JSON text, which a fake returning ``LedgerEvent`` would never
    produce. The declared clock is also asserted, because that is the value the
    command has to thread through for a retry to be byte-identical at all.
    """

    artifact = _write_phase7_artifact(tmp_path / "phase7.json", _result())
    result = _result()

    first = _import_legacy(artifact)
    second = _import_legacy(artifact)

    assert first["already_present"] is False
    assert second["already_present"] is True
    assert second["trial_id"] == first["trial_id"]
    assert second["evidence_sha256"] == first["evidence_sha256"]
    assert second["source_result_sha256"] == result.digest()

    records = _ledger(research_ledger_dsn).events()
    assert [record.event_type for record in records] == [LedgerEventType.LEGACY_IMPORTED]
    assert records[0].legacy is True
    assert records[0].legacy_reason is not None

    bundle = _store(research_env).read(first["evidence_sha256"])
    assert bundle.provenance.registered_at == IMPORT_CLOCK
    # Exactly one file for one import, and one event for it: the retry wrote
    # nothing, because it recognised its own event before building a bundle.
    assert len(list(research_env.rglob("*.json"))) == 1
    assert _verify().exit_code == cli.ExitCode.OK


def test_a_repeated_import_under_the_same_clock_cannot_add_a_second_file(
    tmp_path: Path, research_env: Path, research_ledger_dsn: str
) -> None:
    """The orphan window, closed.

    The importer writes the bundle *before* it appends, so a crash in between
    leaves a document nothing references. A retry under a later clock would build
    different bundle bytes and seal a second file; reusing the first run's
    ``--registered-at`` makes the retry's bytes identical, and the store's
    identical-bytes path collapses the write onto the one address. This asserts
    the property the mitigation buys: same clock in, one file and one event out.
    """

    artifact = _write_phase7_artifact(tmp_path / "phase7.json", _result())

    first = _import_legacy(artifact)
    for _ in range(3):
        assert _import_legacy(artifact)["evidence_sha256"] == first["evidence_sha256"]

    assert len(list(research_env.rglob("*.json"))) == 1
    assert len(_ledger(research_ledger_dsn).events()) == 1


def test_import_legacy_on_the_default_clock_declares_a_fresh_time_and_stays_idempotent(
    tmp_path: Path, research_env: Path, research_ledger_dsn: str
) -> None:
    """The command an operator actually runs, with no ``--registered-at`` at all.

    The wall-clock default is a *fresh declared* time, not a memoised one and not
    the run's own end: bracketing the invocation with two clock reads here is what
    pins that without depending on the platform's tick resolution. What matters
    for idempotency is that the default does not need to be stable at all -- the
    retry recognises its own event by the source result digest and answers from
    the row the chain holds, so a later clock never produces a second trial, a
    second attempt, or a second file. That is the property the option exists to
    *strengthen* (the crash-window orphan), not to create.
    """

    artifact = _write_phase7_artifact(tmp_path / "phase7.json", _result())

    before = datetime.now(UTC)
    first_result = _import_legacy_on_the_default_clock(artifact)
    after = datetime.now(UTC)

    assert first_result.exit_code == cli.ExitCode.OK, first_result.stderr
    first = json.loads(first_result.stdout)
    assert first["already_present"] is False
    assert first["trial_id"] == f"legacy-{_result().digest()[:16]}"

    sealed = _store(research_env).read(first["evidence_sha256"])
    assert before <= sealed.provenance.registered_at <= after
    assert sealed.provenance.registration_state is RegistrationState.LEGACY_UNPREGISTERED
    assert sealed.provenance.occurred_at == _result().end

    # A second invocation on a later wall clock: recognised, unchanged, and
    # writing nothing.
    second = json.loads(_import_legacy_on_the_default_clock(artifact).stdout)
    records = _ledger(research_ledger_dsn).events()

    assert second["already_present"] is True
    assert second["trial_id"] == first["trial_id"]
    assert second["evidence_sha256"] == first["evidence_sha256"]
    assert [record.event_type for record in records] == [LedgerEventType.LEGACY_IMPORTED]
    assert records[0].trial_id == first["trial_id"]
    assert len(list(research_env.rglob("*.json"))) == 1
    assert _verify().exit_code == cli.ExitCode.OK


def test_two_artifacts_import_as_two_independent_trials(
    tmp_path: Path, research_env: Path, research_ledger_dsn: str
) -> None:
    """Idempotency is keyed on the source result digest, not on the file.

    Two genuinely different Phase 7 results are two trials, which is the whole
    reason the key is the digest: keying on the path would have merged them.
    """

    first = _write_phase7_artifact(tmp_path / "one.json", _result())
    second = _write_phase7_artifact(
        tmp_path / "two.json", _result(run_id="run-2", net_pnl=Decimal("100"))
    )

    one = _import_legacy(first)
    other = _import_legacy(second)

    assert one["trial_id"] != other["trial_id"]
    assert other["already_present"] is False
    counters = json.loads(runner.invoke(cli.app, ["research", "trial", "count"]).stdout)
    assert counters["audit_attempts"] == 2
    assert counters["selection_lotteries"] == 2
    assert counters["effective_specifications"] == 2


def test_import_legacy_refuses_a_doctored_artifact(
    tmp_path: Path, research_env: Path, research_ledger_dsn: str
) -> None:
    """An artifact whose result was edited after the run is not evidence.

    The document's ``digest`` field is re-derived rather than trusted, so a
    well-formed 64 characters is not enough; and nothing is sealed or recorded,
    because a failed import must leave no half-written trial behind.
    """

    artifact = _write_phase7_artifact(tmp_path / "phase7.json", _result())
    document = json.loads(artifact.read_text(encoding="utf-8"))
    document["result"]["firm_equity"] = 999999
    artifact.write_text(json.dumps(document), encoding="utf-8")

    result = runner.invoke(
        cli.app,
        [
            "research",
            "trial",
            "import-legacy",
            "--artifact",
            str(artifact),
            "--registered-at",
            _IMPORT_CLOCK_ARGUMENT,
        ],
    )

    assert result.exit_code == cli.ExitCode.EVIDENCE_INTEGRITY
    assert not list(research_env.rglob("*.json"))
    assert _ledger(research_ledger_dsn).events() == ()


# --- show / count ------------------------------------------------------------


def test_show_replays_only_the_requested_trial(tmp_path: Path, research_env: Path) -> None:
    _register(tmp_path)
    _record(tmp_path, trial_id="trial-1", attempt_id="attempt-1")
    _write_json(tmp_path / "other.json", _bundle("trial-2", "attempt-2", _result()))

    result = runner.invoke(
        cli.app,
        [
            "research",
            "trial",
            "record",
            "--trial-id",
            "trial-2",
            "--attempt-id",
            "attempt-2",
            "--evidence",
            str(tmp_path / "other.json"),
        ],
    )
    assert result.exit_code == cli.ExitCode.OK, result.stderr

    shown = json.loads(
        runner.invoke(cli.app, ["research", "trial", "show", "--trial-id", "trial-1"]).stdout
    )

    assert shown["trial_id"] == "trial-1"
    assert {event["trial_id"] for event in shown["events"]} == {"trial-1"}
    assert [event["event_type"] for event in shown["events"]] == [
        "result_recorded",
        "evidence_sealed",
    ]
    # The evidence reference a reader needs is in the row, not reachable only
    # through a second query.
    sealed = shown["events"][-1]
    assert sealed["event_json"]["payload"]["evidence_sha256"]


def test_count_reports_the_three_denominators(tmp_path: Path, research_env: Path) -> None:
    """Design 5.6, counted from the chain rather than kept in a summary row.

    Section 5.7 wants one imported artifact to be one audit attempt, one selection
    lottery and one effective specification -- a refusal must not silently
    exclude the completed Phase 7 trials from the deflation denominators.
    """

    _import_legacy(_write_phase7_artifact(tmp_path / "phase7.json", _result()))

    result = runner.invoke(cli.app, ["research", "trial", "count"])

    assert result.exit_code == cli.ExitCode.OK
    assert json.loads(result.stdout) == {
        "status": "ok",
        "audit_attempts": 1,
        "selection_lotteries": 1,
        "effective_specifications": 1,
    }


# --- verify ------------------------------------------------------------------


def test_verify_reports_a_clean_chain(tmp_path: Path, research_env: Path) -> None:
    _register(tmp_path)
    _record(tmp_path)

    result = _verify()

    assert result.exit_code == cli.ExitCode.OK
    assert json.loads(result.stdout) == {
        "status": "ok",
        "valid": True,
        "checked_events": 3,
        "reason": None,
    }


@pytest.mark.parametrize("damage", ["altered", "missing"])
def test_verify_reports_evidence_it_can_no_longer_read(
    tmp_path: Path, research_env: Path, damage: str
) -> None:
    """An intact chain pointing at a document that is gone is not an intact ledger.

    This is the check a hash chain cannot do for itself: every event hash is
    perfect, and the evidence those events name is not there. The typed evidence
    error rather than ``valid: false`` because the *chain* is intact -- the
    report's own vocabulary is about the chain, and reporting the document as a
    chain failure would send an operator to rebuild rows that are fine.
    """

    _register(tmp_path)
    digest = _record(tmp_path)["evidence_sha256"]
    document = next(research_env.rglob(f"{digest}.json"))
    if damage == "altered":
        document.write_text(document.read_text(encoding="utf-8") + " ", encoding="utf-8")
    else:
        document.unlink()

    result = _verify()

    assert result.exit_code == cli.ExitCode.EVIDENCE_INTEGRITY
    assert "correlation_id" not in result.stderr
    assert "verification failed" in result.stderr


def test_verify_reports_a_tampered_chain_as_invalid(
    tmp_path: Path, research_env: Path, research_migration_dsn: str
) -> None:
    """The chain's own answer, with a reason and a count of how far it was good.

    Tampered by the owner with the trigger disabled, which is the only role that
    can do it -- and is exactly why ``verify`` cannot assume the database
    protected itself.
    """

    _register(tmp_path)
    _record(tmp_path)

    with psycopg.connect(research_migration_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SET ROLE trading_house_owner")
        cursor.execute(
            "ALTER TABLE research.trial_ledger_events "
            "DISABLE TRIGGER reject_trial_ledger_row_mutation"
        )
        cursor.execute(
            "UPDATE research.trial_ledger_events SET event_hash = "
            "pg_catalog.decode(pg_catalog.repeat('00', 32), 'hex') WHERE sequence = 2"
        )
        connection.commit()

    result = _verify()

    assert result.exit_code == cli.ExitCode.TRIAL_LEDGER_INTEGRITY
    assert json.loads(result.stdout) == {
        "status": "invalid",
        "valid": False,
        "checked_events": 1,
        "reason": "event_hash_mismatch",
    }


# --- privilege boundary ------------------------------------------------------


def test_the_runtime_role_the_commands_open_cannot_insert_a_row(
    research_ledger_dsn: str,
) -> None:
    """The chain is append-only because the runtime holds no ``INSERT``.

    A unit test cannot see a grant, and an ``UPDATE``/``DELETE`` trigger test
    alone would still pass against a table whose only protection was the
    trigger. The command's own connection is the one asserted here, so this is
    the privilege the commands actually run with.
    """

    with (
        open_runtime_connection(SecretStr(research_ledger_dsn)) as connection,
        connection.cursor() as cursor,
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cursor.execute("INSERT INTO research.trial_ledger_events DEFAULT VALUES")
        connection.rollback()
