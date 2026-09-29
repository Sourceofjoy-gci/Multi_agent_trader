"""``research trial scenarios`` and ``scenario-report``, against real PostgreSQL.

Phase 8B2b's two operator commands, end to end: a preregistered protocol, three
real replays over the seeded bar store, three real sealed bundles, a real chain
that names them, and a report read back out of both. The unit suite in
``tests/unit/ops/test_scenarios.py`` already proves the six checks against real
bundles; what only a database can show is that ``events_for(trial_id)`` and
``replay()`` really do carry different things, that the chain refuses a repeated
event rather than duplicating it, and that the counters a candidate's grid moves
are the three the deflation argument divides by.

Shared with ``test_backtest_evidence.py`` and imported rather than copied: the
``seeded`` bar fixture, and the ``_args``/``_run``/``_record``/``_write`` helpers
that type the fourteen options ``backtest run`` takes. The S-4 case below compares
the orchestrator's baseline against a hand-run ``backtest run``, and it is
meaningless unless both sides type the *same* options -- so the shared options are
read out of ``_args`` here rather than written a third time, which is a list that
would drift from the command's own signature the first time an option changed.

``research_env`` and ``isolated_research_ledger`` come from this directory's
``conftest.py`` and are not redefined here: the first points the commands at the
two databases and an emptied evidence root, and the second gives each test a
fresh chain, which is what makes every count in this file a statement.
"""

# ruff: noqa: F811
# Every test here requests the ``seeded`` fixture that ``test_backtest_evidence``
# defines, and pytest resolves a fixture by the parameter name -- so the request
# is spelled the same way as the import that brings it into this module, and
# pyflakes reads that as a redefinition. F811 is switched off for this file rather
# than repeated on every test signature; nothing here shadows a module-level name
# by any other route.
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.integration.research.test_backtest_evidence import (  # noqa: F401
    OCCURRED_AT,
    REGISTERED_AT,
    Fixture,
    _args,
    _bundle_of,
    _record,
    _run,
    _write,
    seeded,
)
from tests.integration.research.test_trial_cli import _ledger, _start, _store
from trading_house import cli
from trading_house.core.errors import ScenarioEvidenceError
from trading_house.marketdata.models import Timeframe
from trading_house.research.backtest.costs import CostModel
from trading_house.research.canonical import canonical_sha256
from trading_house.research.trial_ledger import (
    CostSpec,
    DataSpec,
    ExecutionSpec,
    HoldoutSpec,
    HoldoutState,
    LedgerEventType,
    RegimeSpec,
    TrialProtocol,
    TrialSpec,
    ValidationSpec,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("isolated_research_ledger"),
]

runner = CliRunner()

TRIAL_ID = "trial-1"
OTHER_TRIAL_ID = "trial-2"
ATTEMPT_PREFIX = "grid"
AGENT_RUN_ID = "run-scenarios"
STRATEGY_ID = "session_momentum_eurusd"
# Declared, and identical on every invocation, for the reason ``test start``'s
# docstring gives: an event's canonical bytes include its declared clock, so a
# repeated command only derives the same bytes if it declares the same time.
# That is what makes the idempotency case below work at all, and it is why every
# timestamp the orchestrator needs is a required option rather than a clock read.
STARTED_AT = "2026-03-01T11:00:00"

# ``str(Decimal("1"))`` is ``"1"`` and not ``"1.0"``: ``declared_grid`` builds
# the baseline from the literal ``Decimal(1)``, so the attempt ids this file
# asserts are the ones the code actually mints. Written out rather than derived,
# so a formatting change to the attempt id is visible here.
ATTEMPT_IDS = ["grid-1", "grid-1.5", "grid-2"]
MULTIPLIERS = ["1", "1.5", "2"]


def _options(seeded: Fixture) -> dict[str, str]:
    """The options ``backtest run`` and ``research trial scenarios`` share.

    Read out of ``_args`` rather than written out again, which is the whole
    reason this helper exists: the S-4 assertion below is only a comparison of
    two cost levels if both sides were handed the same declared inputs, and a
    second list is a list that can quietly stop being that.

    This is the *full* ``backtest run`` option set including its five cost
    terms. ``_scenario_args`` subtracts the cost ones, because the orchestrator
    reads its costs from the protocol; ``_baseline`` reads them from here, which
    is what makes the preregistration and the hand-run agree.
    """

    argv = _args(seeded, start=seeded.first_bar, end=seeded.last_bar, identity=False)
    return dict(zip(argv[2::2], argv[3::2], strict=True))


def _baseline(options: dict[str, str]) -> CostModel:
    """The protocol's declared baseline, read from the hand-run's own cost options.

    The orchestrator takes no cost options at all -- it reads this field and
    multiplies it per level -- so the only thing that has to agree with it is the
    hand-run ``backtest run`` that the S-4 assertion compares against. Building
    it from that invocation's own options is what makes the two the same
    experiment, and a protocol declaring different costs would correctly refuse
    the grid rather than quietly report on a different one.
    """

    return CostModel(
        commission_per_lot_per_side=Decimal(options["--commission-per-lot-per-side"]),
        slippage_points_per_side=Decimal(options["--slippage-points-per-side"]),
        swap_long_points_per_day=Decimal(options["--swap-long-points-per-day"]),
        swap_short_points_per_day=Decimal(options["--swap-short-points-per-day"]),
        triple_swap_weekday=int(options["--triple-swap-weekday"]),
        stress_multiplier=Decimal(1),
    )


def _protocol(
    seeded: Fixture, *, candidate_ids: tuple[str, ...] = (TRIAL_ID, OTHER_TRIAL_ID)
) -> TrialProtocol:
    """A preregistration whose data window and baseline costs are this grid's own.

    Both halves are load-bearing rather than convenient. Check 5 compares every
    scenario's ``result.start``/``end`` against ``data``, and check 3 compares the
    level-1 scenario's whole ``CostModel`` against ``costs.baseline`` -- so a
    protocol declaring either one differently would fail every case in this file
    for a reason none of them is about.

    ``strategy_id`` and ``strategy_version`` are the registered strategy's own
    names, because check 6 reads them off the *protocol* rather than off the
    bundles: three runs agreeing with each other is not what makes them this
    candidate's runs.
    """

    return TrialProtocol(
        protocol_id="protocol-scenarios",
        protocol_version="1",
        agent_run_id=AGENT_RUN_ID,
        strategy_id=STRATEGY_ID,
        strategy_version="1",
        strategy_sha256="b" * 64,
        data=DataSpec(
            instrument_id="fx.eurusd",
            timeframe=Timeframe.M15,
            start=seeded.first_bar,
            end=seeded.last_bar,
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
            baseline=_baseline(_options(seeded)),
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
        candidates=tuple(
            TrialSpec(
                trial_id=trial_id,
                spec_id=f"spec-{trial_id}",
                rationale="declared before any result existed",
                parameter_space=(("window", "20"),),
            )
            for trial_id in candidate_ids
        ),
    )


def _register(tmp_path: Path, seeded: Fixture) -> Path:
    """Seal the protocol and return the path an operator would type as ``--protocol``."""

    path = tmp_path / "protocol.json"
    path.write_text(_protocol(seeded).model_dump_json(), encoding="utf-8")
    result = runner.invoke(cli.app, ["research", "trial", "register", "--protocol", str(path)])
    assert result.exit_code == cli.ExitCode.OK, result.stderr
    return path


_NOT_ON_THE_ORCHESTRATOR = (
    # The six cost terms, plus the window and the strategy: eight of
    # ``backtest run``'s options that ``scenarios`` deliberately does not take,
    # because ``scenario_report`` compares every one of them against the protocol
    # and a second copy typed on the command line could only agree or refuse the
    # whole grid. Subtracted rather than listed because ``_options`` is read out of
    # ``_args`` precisely so the two commands cannot drift, and a hand-written
    # replacement is exactly the second copy this file exists to avoid.
    "--commission-per-lot-per-side",
    "--slippage-points-per-side",
    "--swap-long-points-per-day",
    "--swap-short-points-per-day",
    "--triple-swap-weekday",
    "--strategy",
    "--start",
    "--end",
)


def _scenario_args(
    seeded: Fixture,
    protocol_path: Path,
    *,
    trial_id: str = TRIAL_ID,
    attempt_prefix: str = ATTEMPT_PREFIX,
    **overrides: str,
) -> list[str]:
    """One ``research trial scenarios`` invocation, in the shape an operator types it.

    The shared options come from ``_options`` minus the eight ``scenarios`` does
    not take, because that command reads its costs, its window and its strategy
    from ``--protocol``. Passing any of them would be refused as unknown options,
    which is the point: there is no second copy of a declared value to disagree
    with the registration, and therefore no way to seal a grid the report would
    then refuse.

    Nothing here is optional either: the command takes no ``--mark-to-market``,
    no ``--stress-multiplier`` and no identity trio, because the grid is the
    only way through it that can widen the declared set.
    """

    options = (
        {
            key: value
            for key, value in _options(seeded).items()
            if key not in _NOT_ON_THE_ORCHESTRATOR
        }
        | {
            "--protocol": str(protocol_path),
            "--trial-id": trial_id,
            "--attempt-prefix": attempt_prefix,
            "--started-at": STARTED_AT,
            "--occurred-at": OCCURRED_AT,
            "--registered-at": REGISTERED_AT,
            "--agent-run-id": AGENT_RUN_ID,
        }
        | overrides
    )
    return [
        "research",
        "trial",
        "scenarios",
        *[value for pair in options.items() for value in pair],
    ]


def _scenarios(seeded: Fixture, protocol_path: Path, **kwargs: Any) -> dict[str, Any]:
    result = runner.invoke(cli.app, _scenario_args(seeded, protocol_path, **kwargs))
    assert result.exit_code == cli.ExitCode.OK, result.stderr
    return json.loads(result.stdout)


def _scenario_report(trial_id: str = TRIAL_ID) -> Any:
    return runner.invoke(cli.app, ["research", "trial", "scenario-report", "--trial-id", trial_id])


def _counters() -> dict[str, Any]:
    result = runner.invoke(cli.app, ["research", "trial", "count"])
    assert result.exit_code == cli.ExitCode.OK, result.stderr
    return json.loads(result.stdout)


def _foreign_baseline_bundle(seeded: Fixture, spec_sha256: str, tmp_path: Path) -> Path:
    """A fourth, *present* 1.0 bundle naming a different candidate.

    Produced the way an operator could produce it rather than by hand-building a
    document: the same replay at the baseline multiplier, sealed under this
    trial's id with another candidate's specification digest. Every field the
    engine sealed is real, and the one thing that is wrong is the one a report has
    to notice.
    """

    hand = _run(
        seeded,
        marked=True,
        trial_id=TRIAL_ID,
        attempt_id="foreign-1.0",
        **{"--spec-sha256": spec_sha256},
    )
    return _write(tmp_path / "foreign.json", hand)


# --- the orchestrator ---------------------------------------------------------


def test_the_orchestrator_seals_every_declared_level_and_reports_them(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    """One invocation, three attempts, three sealed bundles, three reported rows.

    The attempt ids are the prefix plus the multiplier, which is what makes each
    level a distinct ``EXECUTION_STARTED`` and therefore a distinct audit
    attempt. The digests are the store's own and the chain's, cross-checked in
    both directions here: three files on disk, three ``EVIDENCE_SEALED`` rows, and
    three report rows naming the same three addresses.
    """

    protocol_path = _register(tmp_path, seeded)
    payload = _scenarios(seeded, protocol_path)

    assert sorted(payload) == ["report", "scenarios", "status", "trial_id"]
    assert payload["trial_id"] == TRIAL_ID
    assert [row["multiplier"] for row in payload["scenarios"]] == MULTIPLIERS
    assert [row["attempt_id"] for row in payload["scenarios"]] == ATTEMPT_IDS
    assert len(list(research_env.rglob("*.json"))) == 3

    ledger = _ledger(research_ledger_dsn)
    started = [
        r for r in ledger.events_for(TRIAL_ID) if r.event_type is LedgerEventType.EXECUTION_STARTED
    ]
    sealed = [
        r for r in ledger.events_for(TRIAL_ID) if r.event_type is LedgerEventType.EVIDENCE_SEALED
    ]
    assert [r.attempt_id for r in started] == ATTEMPT_IDS
    assert len(sealed) == 3

    report = payload["report"]
    assert report["trial_id"] == TRIAL_ID
    assert report["declared_multipliers"] == MULTIPLIERS
    assert [row["multiplier"] for row in report["scenarios"]] == MULTIPLIERS
    assert [row["attempt_id"] for row in report["scenarios"]] == ATTEMPT_IDS
    assert [row["evidence_sha256"] for row in report["scenarios"]] == [
        row["evidence_sha256"] for row in payload["scenarios"]
    ]
    # The digest the command computed from the protocol's own candidate, not one
    # an operator typed three times.
    assert report["spec_sha256"] == canonical_sha256(_protocol(seeded).candidates[0])
    # Two deltas against the baseline and no verdict, threshold or total anywhere.
    assert [row["multiplier"] for row in report["degradations"]] == ["1.5", "2"]


def test_the_orchestrators_baseline_is_the_run_an_operator_would_type_by_hand(
    seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    """S-4 at the level an operator experiences: the two runs are the *same* run.

    The orchestrator's level-1 simulation and a hand-run
    ``backtest run --mark-to-market --stress-multiplier 1`` over the same
    fourteen options must produce the same ``source_result_sha256``. Asserting
    merely that both succeeded would be worth nothing -- two different simulators
    both succeed, and comparing their outputs is how a disagreement hides.

    The two documents are nevertheless different files, because the bundle names
    the attempt and the specification and the orchestrator mints its own. Asserted
    so a "fix" that made the evidence addresses equal by dropping the identity
    could not pass this.
    """

    protocol_path = _register(tmp_path, seeded)
    payload = _scenarios(seeded, protocol_path)
    hand = _bundle_of(_run(seeded, marked=True, **{"--stress-multiplier": "1"}))

    baseline = payload["report"]["scenarios"][0]
    assert Decimal(baseline["multiplier"]) == Decimal(1)
    assert hand.result.cost_model.stress_multiplier == Decimal(1)
    assert baseline["source_result_sha256"] == hand.source_result_sha256
    assert baseline["evidence_sha256"] != canonical_sha256(hand)


def test_a_trial_the_protocol_never_declared_is_refused_before_anything_is_sealed(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    """``ConfigurationError`` and nothing written, for a ``--trial-id`` no file declares.

    ``TrialProtocol`` has no ``candidate_for``, so the candidate is reached by
    comprehension and an unknown id is the end of it. A bare ``next(...)`` would
    raise ``StopIteration``, which ``_execute``'s catch-all would answer as an
    internal fault with a correlation id -- telling an operator to file a bug
    about the remedy, which is to type the id the protocol declares. Exit 2 with
    an empty evidence root and one chain row is the whole of it.
    """

    protocol_path = _register(tmp_path, seeded)

    result = runner.invoke(
        cli.app, _scenario_args(seeded, protocol_path, trial_id="trial-never-declared")
    )

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert result.stdout == ""
    assert not list(research_env.rglob("*.json"))
    assert [record.event_type for record in _ledger(research_ledger_dsn).events()] == [
        LedgerEventType.PREREGISTERED
    ]


def test_rerunning_the_same_prefix_seals_nothing_and_reports_the_same_three(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    """Every timestamp an option rather than a clock read, and this is what they buy.

    Every event id the orchestrator appends is derived from content -- the
    attempt from the trial and the attempt id, the evidence seal from the digest
    -- and every timestamp in those bytes is an option. A second invocation with
    the same values therefore derives identical bytes and is recognised as a
    retry; a clock read anywhere in the loop would make each run's bytes differ
    and the retry would be refused as a conflict, every time, forever.

    Asserted on the chain as well as the payload: "the same three digests" is only
    half of idempotency, and the other half is that nothing was appended.
    """

    protocol_path = _register(tmp_path, seeded)
    first = _scenarios(seeded, protocol_path)
    before = len(_ledger(research_ledger_dsn).events())

    second = _scenarios(seeded, protocol_path)

    assert second["scenarios"] == first["scenarios"]
    assert second["report"] == first["report"]
    assert len(_ledger(research_ledger_dsn).events()) == before
    assert len(list(research_env.rglob("*.json"))) == 3


def test_the_orchestrator_adds_three_audit_attempts_and_closes_no_new_lottery(
    seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    """§5.6, counted: three attempts, one selection, one specification.

    The absolute counts are the orchestrator's own claim -- one candidate
    examined at three cost levels is three audit attempts and **one** selection,
    and the selection count is what the deflation denominator divides by. The
    delta afterwards is the same claim made the other way: three more attempts
    of the same candidate, appended through the ordinary ``start`` command, move
    the audit count and nothing else.

    This is the case that would catch an orchestrator minting a trial id or a
    specification digest per multiplier. Either would report 3/3/3 or 3/1/3, and
    both would be a candidate quietly examined three times over.
    """

    protocol_path = _register(tmp_path, seeded)
    assert _counters() == {
        "status": "ok",
        "audit_attempts": 0,
        "selection_lotteries": 0,
        "effective_specifications": 0,
    }

    _scenarios(seeded, protocol_path)

    assert _counters() == {
        "status": "ok",
        "audit_attempts": 3,
        "selection_lotteries": 1,
        "effective_specifications": 1,
    }

    spec_sha256 = canonical_sha256(_protocol(seeded).candidates[0])
    for attempt_id in ("grid-1.0-again", "grid-1.5-again", "grid-2.0-again"):
        started = runner.invoke(
            cli.app,
            [
                "research",
                "trial",
                "start",
                "--trial-id",
                TRIAL_ID,
                "--attempt-id",
                attempt_id,
                "--spec-sha256",
                spec_sha256,
                "--started-at",
                STARTED_AT,
            ],
        )
        assert started.exit_code == cli.ExitCode.OK, started.stderr

    assert _counters() == {
        "status": "ok",
        "audit_attempts": 6,
        "selection_lotteries": 1,
        "effective_specifications": 1,
    }


# --- the standalone read ------------------------------------------------------


def test_the_standalone_read_reports_the_declared_grid_and_all_three_scenarios(
    seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    """The same report, from a command that names only the trial.

    Asserted for *equality* with the orchestrator's own output rather than for
    its shape: both go through ``_scenario_report_for``, and a reader who saw two
    commands disagree about the same sealed evidence would have no way to say
    which one was wrong. The grid is read from the ``PREREGISTERED`` event, not
    from the protocol file -- the file is not even an option here, and the
    command is not given one.
    """

    protocol_path = _register(tmp_path, seeded)
    sealed = _scenarios(seeded, protocol_path)["report"]

    result = _scenario_report()

    assert result.exit_code == cli.ExitCode.OK, result.stderr
    assert json.loads(result.stdout) == {"status": "ok", **sealed}


def test_a_candidate_whose_bundle_was_deleted_is_an_integrity_failure_not_a_scenario_one(
    seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    """17 and not 19, and the difference is the remedy.

    ``store.read`` raises ``EvidenceIntegrityError`` on a digest with no file, and
    it does so *before* the report ever holds a set to find incomplete. A
    document that is not there is an integrity failure with one remedy -- restore
    it from backup -- while a set missing a declared level is a different one:
    run the level. Both are refusals, and a 19 that also covered a deleted file
    would send an operator to run a scenario they should be restoring.
    """

    protocol_path = _register(tmp_path, seeded)
    payload = _scenarios(seeded, protocol_path)
    removed = payload["scenarios"][2]["evidence_sha256"]

    next(research_env.rglob(f"{removed}.json")).unlink()
    result = _scenario_report()

    assert result.exit_code == cli.ExitCode.EVIDENCE_INTEGRITY
    assert "correlation_id" not in result.stderr
    # ``verify`` answers the same way, and for the same reason: it re-reads every
    # file the chain names, so a missing document stops it too. Nothing here
    # reports the *chain* as broken -- the rows are intact, and the 19 the report
    # would have raised is the code that is deliberately not reached.
    verified = runner.invoke(cli.app, ["research", "trial", "verify"])
    assert verified.exit_code == cli.ExitCode.EVIDENCE_INTEGRITY
    assert verified.stdout == ""


def test_a_complete_but_wrong_set_is_refused_at_the_scenario_code(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    """Every level present, one of them sealed twice, and the whole set refused.

    The 19 is for a set that is *present but wrong*, which is the failure a
    completeness check exists for and the one an integrity check cannot see: all
    three files are on disk, all of them hash to their own names, and the chain
    verifies. What is wrong is that a fourth attempt sat down at the level the
    1.0x scenario already occupies, with a different candidate's specification
    digest -- so which of the two the report should read is a question the
    report cannot answer without silently dropping one run's evidence.

    The refusal lands on completeness rather than on the identity check, because
    completeness runs first and a repeated level trips it. That ordering is
    deliberate -- the report is refused for the *first* reason that applies -- and
    the cause text below is what pins which reason fired, since both are 19.
    """

    protocol = _protocol(seeded)
    protocol_path = _register(tmp_path, seeded)
    _scenarios(seeded, protocol_path)
    _record(
        _foreign_baseline_bundle(seeded, canonical_sha256(protocol.candidates[1]), tmp_path),
        attempt_id="foreign-1.0",
    )

    result = _scenario_report()

    assert result.exit_code == cli.ExitCode.SCENARIO_EVIDENCE
    assert "correlation_id" not in result.stderr
    with pytest.raises(ScenarioEvidenceError) as refusal:
        cli._scenario_report_for(TRIAL_ID, _ledger(research_ledger_dsn), _store(research_env))
    assert "sealed more than once" in str(refusal.value.__cause__)


def test_a_grid_whose_runs_disagree_about_the_specification_is_refused_on_identity(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    """The identity check's clause, reached through the command rather than only in a unit test.

    The case above lands on *completeness*, because a repeated level trips it
    first and the report is refused for the first reason that applies. That leaves
    the identity check's own clause unexercised end to end, and it is reachable
    with a complete grid: all three levels present exactly once, every summary
    COMPLETE, the baseline and both stressed levels matching the protocol, and
    the window the declared one. Only the specification the runs were pinned to
    disagrees, and nothing above check 6 can see that.

    All three levels are sealed by hand rather than by the orchestrator, because
    the orchestrator cannot produce this shape: it derives one digest for all
    three, which is the whole point of it, so a fourth document at an occupied
    level is the only way to introduce a second one and that is a completeness
    defect instead. The specification digest is operator-declared and unvouched
    since Phase 8A -- the ledger records what it is told and counts the distinct
    values -- so this is what an operator produces by starting one attempt from
    the wrong candidate's digest, not a hand-built contrivance.
    """

    protocol = _protocol(seeded)
    _register(tmp_path, seeded)
    honest = canonical_sha256(protocol.candidates[0])
    drifted = canonical_sha256(protocol.candidates[1])
    for multiplier, spec_sha256 in (("1", honest), ("1.5", drifted), ("2", honest)):
        attempt_id = f"grid-{multiplier}"
        assert (
            _start(trial_id=TRIAL_ID, attempt_id=attempt_id, spec_sha256=spec_sha256).exit_code
            == cli.ExitCode.OK
        )
        _record(
            _write(
                tmp_path / f"{attempt_id}.json",
                _run(
                    seeded,
                    marked=True,
                    trial_id=TRIAL_ID,
                    attempt_id=attempt_id,
                    **{"--stress-multiplier": multiplier, "--spec-sha256": spec_sha256},
                ),
            ),
            attempt_id=attempt_id,
        )

    result = _scenario_report()

    assert result.exit_code == cli.ExitCode.SCENARIO_EVIDENCE
    with pytest.raises(ScenarioEvidenceError) as refusal:
        cli._scenario_report_for(TRIAL_ID, _ledger(research_ledger_dsn), _store(research_env))
    cause = str(refusal.value.__cause__)
    assert "spec_sha256" in cause
    assert "sealed more than once" not in cause


def test_a_candidate_the_chain_never_registered_is_refused_by_name(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    """The trap this pair of reads exists for, exercised from both sides.

    A ``PREREGISTERED`` row's ``trial_id`` column is null, because one event seals
    a whole candidate family, so ``events_for`` -- ``WHERE trial_id = %s`` --
    never returns the registration. A report built from that read alone would tell
    an operator their registered candidate was never registered, and send them to
    preregister a trial they had already preregistered. The chain's own
    ``declares_trial`` says the opposite, so the refusal here is a contradiction
    between two reads of one record rather than a fact about the candidate.

    The two assertions either side of the refusal are the halves of that: the
    rows ``events_for`` returns for a *registered* trial carry no registration at
    all, and the ledger nevertheless declares it. A report that read its protocol
    from ``events_for`` would therefore pass the healthy case above only by never
    getting as far as a protocol -- and here it would report a real candidate as
    unregistered, which is why the cause text is read rather than the raise.
    """

    _register(tmp_path, seeded)
    ledger = _ledger(research_ledger_dsn)
    assert ledger.declares_trial(TRIAL_ID) is True
    # The asymmetry the helper is built around, stated as a fact about the chain:
    # a trial's own rows hold three evidence seals and not one registration.
    assert LedgerEventType.PREREGISTERED not in {
        record.event_type for record in ledger.events_for(TRIAL_ID)
    }
    assert any(record.event_type is LedgerEventType.PREREGISTERED for record in ledger.replay())

    result = _scenario_report("trial-never-declared")

    assert result.exit_code == cli.ExitCode.SCENARIO_EVIDENCE
    assert result.stdout == ""
    with pytest.raises(ScenarioEvidenceError) as refusal:
        cli._scenario_report_for("trial-never-declared", ledger, _store(research_env))
    cause = str(refusal.value.__cause__)
    assert "trial-never-declared" in cause
    assert "no preregistered protocol" in cause
