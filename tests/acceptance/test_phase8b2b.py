"""Phase 8B2b acceptance: a candidate's declared cost grid, the six checks its
sealed evidence must pass, and a report that states no verdict.

8B2a sealed a per-trade cost split beside every run. 8B2b is the layer that reads
three of them over: umbrella 6.4 requires a preregistered candidate to be rerun at
1.0x, 1.5x and 2.0x costs, and this phase runs the grid, seals each level as
ordinary evidence, and reports across the three. It adds **no new sealed
artifact** -- the three bundles are the evidence, and a report is a read of them
plus the registration that declared the grid.

What this file is for is therefore not "the report works" but the three claims
the rest of the framework rests on:

1. **the grid comes from the chain.** ``scenario-report`` recovers the protocol
   out of the ``PREREGISTERED`` event rather than from a file an operator names,
   so what is checked is what was sealed before any result existed. The
   disagreement between the two reads is not a hypothetical: an operator who
   registers a protocol, edits their copy, and runs the orchestrator gets nine
   writes and *then* a refusal, and
   ``test_a_protocol_file_edited_after_registration_is_refused_after_its_writes``
   is the case that makes that cost visible rather than asserted in a docstring.
2. **the six checks fail closed, each naming what it found and what it
   expected.** Every refusing case here is a *real* sealed bundle in a *real*
   evidence root, reached through ``research trial record``, over a real chain.
   All six refuse with the same exit code, so the private cause is read for each
   -- a refusal that cannot be told apart from the other five proves that
   something refuses, not that *this* check does.
3. **the report states no verdict.** Not only in its key set, which is pinned
   here over a real report, but in the operator-facing ``--help`` text of both
   commands, which is where a threshold would actually be sold.

The fixtures are the real ones, imported rather than re-created: the ``seeded``
three-day bar store and the ``_args``/``_run``/``_record``/``_write`` helpers
from ``test_backtest_evidence``, the protocol, the registration and the two
command drivers from ``test_scenarios``, the environment from that directory's
``conftest``, and the tamper builders from ``tests/unit/ops/test_scenarios``.
The last one is the important sharing: a tampered document that the unit suite
already proved is *model-accepted* is what makes a refusal here the report's
rather than pydantic's, and a second tamper builder in this file would be a
second thing that could drift from that proof.

Six of the seven cases in the large parametrisation below are the same grid --
three real replays, one per declared level, each started and sealed by a real
command -- with **one** document changed. The seventh is that grid untampered,
and it sits in the same table on purpose: six refusals are only evidence about
six checks if the grid they were built from is a grid the report accepts.

What is deliberately **not** asserted here: anything about promotion, and any
claim that a candidate survives its grid. 8B2b judges nothing. A grid that
passes all six checks is a grid that is what it says it is, which is a different
claim and the only one this framework makes before 8D does.
"""

# ruff: noqa: F811
# Every database test here requests the ``seeded`` and ``research_env`` fixtures
# that the research integration suite defines, and pytest resolves a fixture by
# the parameter name -- so the request is spelled the same way as the import that
# brings it into this module, and pyflakes reads that as a redefinition. F811 is
# switched off for this file rather than repeated on every test signature;
# nothing here shadows a module-level name by any other route.
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.acceptance.test_phase8b1 import PHASE7_RESULT_DIGESTS, V1_BUNDLE_SHA256
from tests.integration.research.conftest import research_env  # noqa: F401
from tests.integration.research.test_backtest_evidence import (
    Fixture,
    _bundle_of,
    _record,
    _run,
    _write,
    seeded,  # noqa: F401
)
from tests.integration.research.test_scenarios import (
    ATTEMPT_IDS,
    ATTEMPT_PREFIX,
    MULTIPLIERS,
    STARTED_AT,
    TRIAL_ID,
    _counters,
    _protocol,
    _register,
    _scenario_args,
    _scenario_report,
    _scenarios,
)
from tests.integration.research.test_trial_cli import _ledger, _start, _store
from tests.property.test_trial_evidence import _CANONICAL_BUNDLE_SHA256, _bundle
from tests.unit.ops.test_scenarios import (
    _NO_VERDICT,
    _carrying,
    _costs,
    _ending_at,
    _moving,
    _rebuild,
)
from trading_house import cli
from trading_house.core.errors import ScenarioEvidenceError
from trading_house.ops.scenarios import ScenarioReport, declared_grid, registered_protocol
from trading_house.research.canonical import canonical_sha256
from trading_house.research.evidence import EvidenceBundle
from trading_house.research.trial_ledger import CostAttributionStatus, LedgerEventType
from trading_house.strategies.impl.session_momentum import SESSION_MOMENTUM_SPEC

runner = CliRunner()

DRIFTED_END = datetime(2025, 6, 1, tzinfo=UTC)
"""A ``result.end`` the protocol cannot have declared -- it precedes the run's own
start, and ``BacktestResult`` holds no start-before-end rule of its own. The
window check's case is the one an operator produces by mistyping ``--end``, and a
window that is merely *different* would be caught by the same rule, so the
absurd value is used to make the substitution obvious in the refusal text."""


# --- the report's own vocabulary ----------------------------------------------

_NO_VERDICT_HELP = (
    "surviv",
    "threshold",
    "promot",
    "recommend",
    "approve",
    "reject",
    "verdict",
    "pass",
    "fail",
    "accept",
    "eligib",
)
"""The vocabulary of a decision about a candidate, as stems, matched anywhere in the help.

``promot`` is the spelling that matters: ``promote`` is *not* a substring of
``promotion``, so a gate written on the bare word would pass a help screen that
announced its own promotion gate. The rest are stems for the same reason -- one
inflection slipping past a gate is the whole failure mode.

``judge`` is absent, for the reason ``tests/unit/ops/test_scenarios.py`` gives for
the same omission: both commands say in their own docstrings that they do not
judge, and a gate that fired on the sentence promising restraint would be a gate
to disable rather than to satisfy.

``pass`` and ``fail`` are in the list, and an earlier version left them out on the
argument that they name any unsuccessful thing rather than a decision about a
candidate. That argument does not survive the gate's actual scope: it reads two
help screens, and *neither* contains either word. The one place in this CLI that
does — ``health``'s summary promising "a typed failure" — is not a screen this
gate reads, so the exclusion bought nothing and cost the two likeliest phrasings
of the thing the binding constraint forbids: "the candidate passes the declared
grid". If a future sentence genuinely needs one, the remedy is the reword this
slice already applied to ``verdict``.

Lowercased before matching, and that is not a detail. A capital at the start of a
sentence is where a capital goes, and a docstring's first line is the summary Typer
renders -- so ``Survives the grid; 8D decides.`` would otherwise pass, and it is
the single easiest edit that could make this gate a lie.

**This is not the same list as the report gate's, on purpose.** That one reads
``_NO_VERDICT`` from ``tests/unit/ops/test_scenarios.py`` and carries one word this
one does not — ``total`` — because it matches *field names*, where a field called
``total_cost`` would launder the four signed terms into one, whereas ``total`` is a
word an operator uses freely about their own P&L. The two lists agree on the rest,
including ``verdict``: a field by that name and a sentence by that name are the
same claim in different clothes, and only the prose one has to be worded around.
"""


def _verdict_claims(help_text: str) -> list[str]:
    """Every stem of decision vocabulary found anywhere in a help screen.

    There is no denial rule and no exception. An earlier draft allowed a stem in
    any sentence that also contained ``no`` or ``not``, which is what let
    ``There is no verdict here: a candidate that survives the grid is promoted``
    through -- the denial and the claim in one sentence, licensing each other.

    That exception existed only because ``scenario-report``'s docstring said "There
    is no survival verdict here and no threshold ... 8D's promotion gate, with its
    thresholds fixed in advance", which uses three of these words to promise
    restraint. The docstring now says the same thing in words that are not the
    decision's own, so the exception is not needed and the rule is total: this
    help text may not contain a word of decision vocabulary at all, however it is
    spelled, cased, or surrounded by a promise not to decide.

    The rationale for the docstring's wording lives here rather than in the
    docstring for the same reason the rule is total: a paragraph in the help
    screen *explaining* the gate is a paragraph that can trip it, so the first
    draft of that explanation failed its own gate and was cut down to this one.
    """

    text = " ".join(help_text.split()).lower()
    return [word for word in _NO_VERDICT_HELP if word in text]


def _names_and_text(value: object) -> set[str]:
    """Every mapping key **and every string value** anywhere in a dumped report.

    The unit suite's ``_all_keys`` walks keys; this adds the values, because a
    ``"total cost"`` could be carried as a label on a row rather than as a field
    name, and a gate that only read field names would not see it. A sweep over
    the top level would miss both -- a ``total_cost`` belongs on
    ``ScenarioTotals``, two levels down.
    """

    if isinstance(value, dict):
        return {str(key) for key in value} | set().union(
            *(_names_and_text(item) for item in value.values())
        )
    if isinstance(value, list):
        return set().union(*(_names_and_text(item) for item in value))
    return {value} if isinstance(value, str) else set()


# --- a grid, sealed the way the chain seals one -------------------------------


def _spec_sha256(seeded: Fixture) -> str:
    """The registration's own candidate digest, computed rather than typed.

    The same expression ``research trial scenarios`` runs, so a hand-sealed grid
    here is pinned to the same specification an orchestrated one would be -- which
    is what makes the six refusals below about the tampered document and nothing
    else.
    """

    return canonical_sha256(_protocol(seeded).candidates[0])


def _seal_grid(
    seeded: Fixture,
    tmp_path: Path,
    *,
    levels: tuple[str, ...] = MULTIPLIERS,
    tamper: tuple[str, Callable[[dict[str, Any]], None]] | None = None,
) -> None:
    """One candidate's grid, one real replay and one real seal per level.

    Every level is a ``backtest run --mark-to-market`` the CLI itself produced,
    a ``research trial start`` and a ``research trial record``; the attempt ids
    are the ones the orchestrator mints, so nothing downstream can tell a
    hand-sealed grid from an orchestrated one -- which is the point, since the
    report is supposed to read the evidence and not the route it took.

    ``tamper`` is ``(level, change)``. The change is applied to the emitted
    bundle's own JSON and the result re-validated through
    ``EvidenceBundle.model_validate_json``, which is the path every reader takes
    and the one the unit suite proved leaves the document *accepted*: a tamper
    that could not be sealed would prove pydantic's rules, not the report's.
    Re-sealing a tampered document is a real write to a real evidence root at a
    new address, and the chain names that new address -- so a refusal below is
    about a set the chain genuinely holds.
    """

    for level in levels:
        attempt_id = f"{ATTEMPT_PREFIX}-{level}"
        spec_sha256 = _spec_sha256(seeded)
        started = _start(
            trial_id=TRIAL_ID, attempt_id=attempt_id, spec_sha256=spec_sha256, started_at=STARTED_AT
        )
        assert started.exit_code == cli.ExitCode.OK, started.stderr
        payload = _run(
            seeded,
            marked=True,
            trial_id=TRIAL_ID,
            attempt_id=attempt_id,
            **{"--stress-multiplier": level, "--spec-sha256": spec_sha256},
        )
        bundle: EvidenceBundle = _bundle_of(payload)
        assert bundle.result.trades, "a grid over a run that never traded proves nothing"
        if tamper is not None and tamper[0] == level:
            bundle = _rebuild(bundle, tamper[1])
        _record(
            _write(tmp_path / f"{attempt_id}.json", json.loads(bundle.model_dump_json())),
            attempt_id=attempt_id,
        )


def _refusal_cause(research_ledger_dsn: str, research_env: Path) -> str:
    """The private cause of the refusal the command just reported.

    All six checks exit 19 and the public message is the same opaque sentence for
    every one of them, so the cause is the only thing that distinguishes "the
    completeness check fired" from "something else refused first". It stays on
    the error's private cause by design: a report that narrates its inputs back
    to whoever asked for one is exactly what ``ScenarioEvidenceError`` refuses to
    be, so a test is the right place to read it and an operator is not.
    """

    with pytest.raises(ScenarioEvidenceError) as refusal:
        cli._scenario_report_for(TRIAL_ID, _ledger(research_ledger_dsn), _store(research_env))
    cause = refusal.value.__cause__
    assert cause is not None, "a refusal with no cause says nothing about which check fired"
    return str(cause)


def _without_the_split(payload: dict[str, Any]) -> None:
    """A level whose summary is ``PARTIAL`` and which carries no per-trade split.

    Both halves move together because ``EvidenceBundle`` already couples them: a
    ``PARTIAL`` summary beside a split is refused at construction, so a tamper
    that changed one without the other would be a ``ValidationError`` and would
    prove the wrong layer. This is the shape a Phase 7 document has, and it is
    the one the report cannot read -- spread and slippage are ``None`` on it, and
    reading a ``None`` as a zero would turn an unmeasured term into a flattering
    one.
    """

    _carrying(CostAttributionStatus.PARTIAL)(payload)


def _envelope(result: Any) -> dict[str, Any]:
    """A command's payload with the ``status`` envelope taken off.

    Every command here prints ``{"status": "ok", ...}``, and ``ScenarioReport``
    is ``extra="forbid"``, so the envelope is removed rather than tolerated --
    and its removal asserts the envelope was there. The model is rebuilt from
    JSON rather than from the decoded dict because every research contract is
    ``strict``: a decoded dict's string decimals and timestamps are refused,
    which is the same reason the command reads a sealed document through its JSON
    path.
    """

    payload = json.loads(result.stdout)
    assert payload.pop("status") == "ok", payload
    return payload


def _assert_refused(result: Any, exit_code: int) -> None:
    """One shape every refusal in this file shares, asserted once.

    The exit code is the contract, the empty stdout is the contract, and the
    absent correlation id is the contract: an unmapped error answers an operator
    with ``unexpected failure`` and a correlation id, which would mean the
    refusal escaped as something it is not.
    """

    assert result.exit_code == exit_code, result.stderr
    assert result.stdout == ""
    assert "correlation_id" not in result.stderr


# --- 1. a real candidate passes the six checks and reports its declared grid ---


@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_a_real_candidate_passes_all_six_checks_and_reports_its_declared_grid(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    """The orchestrator, the chain and the report, agreeing on one grid.

    The grid is read out of the chain rather than out of the file the operator
    typed, and the two are compared: ``registered_protocol`` over
    ``ledger.replay()`` must return exactly the protocol that was registered, and
    ``declared_grid`` over it must be ``1, 1.5, 2``. That is the whole of the
    first design rule, and it is asserted on the *chain's* copy so a report that
    quietly fell back to the file would fail here rather than agree by accident.

    A grid that comes out of the report at all is a grid that passed all six
    checks -- ``scenario_report`` refuses before building a model, and the
    command exits non-zero if it raises. Three distinct documents at three
    distinct result digests, each named by the address it was sealed at, is what
    makes "three sealed scenarios" a statement about files on disk rather than
    three rows in a payload.

    The counters are read here as a *delta* on this run, which is the shape the
    deflation argument needs: three audit attempts moved, and the other two
    denominators not. What makes those two numbers 1 rather than 3 is a property
    of the chain's rows rather than of the counter, and
    ``test_the_grid_is_one_selection_lottery_whatever_it_costs_to_run`` is where
    that is asserted.
    """

    protocol_path = _register(tmp_path, seeded)
    before = _counters()
    payload = _scenarios(seeded, protocol_path)
    after = _counters()

    sealed_protocol = registered_protocol(_ledger(research_ledger_dsn).replay(), TRIAL_ID)
    assert sealed_protocol == _protocol(seeded)
    grid = declared_grid(sealed_protocol)
    assert grid == (Decimal("1"), Decimal("1.5"), Decimal("2"))

    report = payload["report"]
    assert report["declared_multipliers"] == [str(level) for level in grid]
    assert [row["multiplier"] for row in report["scenarios"]] == [str(level) for level in grid]
    assert [row["attempt_id"] for row in report["scenarios"]] == ATTEMPT_IDS
    # One delta per stressed level against the baseline, and no delta for the
    # baseline itself: a degradation measured against nothing would be a number
    # about a level rather than a comparison of two runs.
    assert [row["multiplier"] for row in report["degradations"]] == ["1.5", "2"]
    # One specification digest for all three, computed from the protocol's own
    # candidate. That matters because the third denominator is a ``len(set())``
    # over what an operator declared and nothing compares those values to a
    # registration, so a grid that minted one digest per level would be counted
    # as three specifications. The report's own copy is read off the sealed
    # documents rather than off the protocol: the ledger's ``spec_sha256`` column
    # is the operator's declared value, unvouched since 8A for everything but a
    # start, so a report that read it from the registration would be asserting a
    # cross-check the chain does not make.
    assert report["spec_sha256"] == _spec_sha256(seeded)
    sealed = [_store(research_env).read(row["evidence_sha256"]) for row in report["scenarios"]]
    assert {bundle.spec_sha256 for bundle in sealed} == {report["spec_sha256"]}

    digests = [row["evidence_sha256"] for row in report["scenarios"]]
    assert len(set(digests)) == 3
    assert len({row["source_result_sha256"] for row in report["scenarios"]}) == 3
    # The store holds exactly those three files: a report that named an address
    # nothing is sealed at would pass every assertion above.
    assert sorted(path.name for path in research_env.rglob("*.json")) == sorted(
        f"{digest}.json" for digest in digests
    )
    assert [record.event_type for record in _ledger(research_ledger_dsn).events()] == [
        LedgerEventType.PREREGISTERED
    ] + [
        event
        for _ in MULTIPLIERS
        for event in (
            LedgerEventType.EXECUTION_STARTED,
            LedgerEventType.RESULT_RECORDED,
            LedgerEventType.EVIDENCE_SEALED,
        )
    ]

    assert before["audit_attempts"] == 0
    assert after["audit_attempts"] == before["audit_attempts"] + 3
    assert after["selection_lotteries"] == before["selection_lotteries"] + 1
    assert after["effective_specifications"] == before["effective_specifications"] + 1


# --- 2. the report's numbers are the sealed bundles' own numbers ----------------


@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_the_report_re_prints_what_two_sealed_bundles_already_say(
    seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    """A reader must be able to re-add the report and land on the sealed numbers.

    Every figure in a scenario row is a field the bundle sealed and its own
    validators checked, or a digest the chain already holds -- nothing is
    recomputed here, because a report that re-derived them would be a second
    implementation of the engine and a disagreement between the two would be
    unresolvable. So the assertion is against the documents *read back off disk*
    by their own addresses, and not against the payload the command printed,
    which would only prove the payload is consistent with itself.

    The sign convention is the reason the four cost terms are never summed, and
    it is asserted rather than asserted-about: ``swap`` is a charge when it is
    negative and a credit when it is positive, and it enters ``net`` with a
    ``+``, so a "total cost" that added the four together would bury a credit
    inside a plausible-looking number. The relation holds term by term on
    whatever this run produced.

    And the stress is paid where the stress is meant to land: the spread rises and
    the market's move does not. A stressed scenario whose ``market_pnl`` had
    drifted would be reporting a different market rather than a harsher one, and
    the attribution is the only place that would show it.
    """

    protocol_path = _register(tmp_path, seeded)
    _scenarios(seeded, protocol_path)
    result = _scenario_report()

    assert result.exit_code == cli.ExitCode.OK, result.stderr
    report = ScenarioReport.model_validate_json(json.dumps(_envelope(result)))
    store = _store(research_env)
    sealed = {row.multiplier: store.read(row.evidence_sha256) for row in report.scenarios}

    for row in report.scenarios:
        bundle = sealed[row.multiplier]
        assert row.source_result_sha256 == bundle.source_result_sha256
        assert row.attempt_id == bundle.attempt_id
        assert row.trades == len(bundle.result.trades)
        assert row.net_pnl == bundle.result.net_pnl
        # The four terms are the summary's own, and the report never reduces them
        # to one figure. The reader's version of the check the bundle makes.
        assert row.spread_cost == bundle.costs.spread_cost
        assert row.slippage_cost == bundle.costs.slippage_cost
        assert row.commission == bundle.costs.commission
        assert row.swap == bundle.costs.swap
        assert (
            row.market_pnl - row.spread_cost - row.slippage_cost - row.commission + row.swap
        ) == row.net_pnl

    baseline = report.scenarios[0]
    for row, delta in zip(report.scenarios[1:], report.degradations, strict=True):
        assert delta.multiplier == row.multiplier
        assert delta.net_pnl_delta == baseline.net_pnl - row.net_pnl
        assert delta.market_pnl_delta == row.market_pnl - baseline.market_pnl
        assert delta.spread_cost_delta == row.spread_cost - baseline.spread_cost
        assert delta.slippage_cost_delta == row.slippage_cost - baseline.slippage_cost
        assert delta.commission_delta == row.commission - baseline.commission
        assert delta.swap_delta == row.swap - baseline.swap
        assert row.spread_cost > baseline.spread_cost
        assert row.market_pnl == baseline.market_pnl

    # Non-vacuous: the market move is the one figure that must NOT move, and a
    # fixture that traded nothing would make the equality above true by having
    # nothing to compare. Stressed spreads raise the cost; they do not reach back
    # into the signal, and a difference here would mean the cost leaked into the
    # trade decision -- the one thing a cost grid must not do.
    assert baseline.market_pnl != 0
    assert baseline.trades > 0

    # ``swap`` is reported verbatim from the summary -- signed, never passed
    # through ``abs`` and never folded into a total -- and this fixture's runs
    # cross no rollover, so it is a *measured* zero rather than an absent term.
    # The signed case (a credit reduced under stress rather than enlarged) needs a
    # window that holds a position past midnight, and is asserted where such a
    # window is built: ``tests/unit/ops/test_scenarios.py``. What is asserted here
    # is the identity above, which is the form that makes ``swap`` the odd term
    # out: a charge is negative and enters ``net`` with a ``+``, so a "total cost"
    # adding the four together would bury a credit inside a plausible number.


# --- 3. the report names no total and no verdict -------------------------------


@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_the_report_carries_no_key_or_value_naming_a_total_or_a_verdict(
    seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    """The premise, asserted on a real report rather than on a field list.

    A report is a read, and the only thing that would make it a gate is a
    *field* that judges -- a ``verdict``, a ``threshold``, a ``promote_to``, a
    ``total_cost``. The vocabulary comes from the unit suite, which argues why
    those words cannot appear in prose promising restraint, and this adds the
    values: a row could carry the claim as a label rather than as a key.

    What is deliberately not asserted is anything about the *numbers*. A
    ``net_pnl`` of +350.48 at 1.0x and -20.22 at 1.5x is a fact about two runs,
    and whether it refutes a candidate is the promotion gate's question, with
    thresholds fixed in advance. Refusing to publish a degradation would be the
    same defect as publishing a verdict: the operator would learn the outcome
    from nothing rather than from the evidence.
    """

    protocol_path = _register(tmp_path, seeded)
    _scenarios(seeded, protocol_path)
    result = _scenario_report()

    assert result.exit_code == cli.ExitCode.OK, result.stderr
    report = ScenarioReport.model_validate_json(json.dumps(_envelope(result)))
    document = report.model_dump(mode="json")

    assert _names_and_text(document), "a report with no keys or values would pass vacuously"
    offending = sorted(
        text for text in _names_and_text(document) for word in _NO_VERDICT if word in text.lower()
    )
    assert offending == []
    # The declared grid is named *before* the scenarios in the field order, so a
    # reader meets the preregistration before the outcome. That is a rendering
    # property and nothing computes it, so this holds the prefix rather than
    # claiming an ordering any code depends on -- and it is the one property of
    # this model worth a test that needs no verdict vocabulary near it.
    assert list(ScenarioReport.model_fields)[:3] == [
        "trial_id",
        "spec_sha256",
        "declared_multipliers",
    ]


# --- 4. each of the six checks refuses a real sealed grid ----------------------


@pytest.mark.parametrize(
    ("levels", "tamper", "expected"),
    [
        pytest.param(MULTIPLIERS, None, None, id="control-the-hand-sealed-grid-is-reportable"),
        pytest.param(("1", "1.5"), None, "missing", id="1-completeness"),
        pytest.param(MULTIPLIERS, ("1", _without_the_split), "per-trade split", id="2-attribution"),
        pytest.param(
            MULTIPLIERS,
            ("1", _costs({"commission_per_lot_per_side": Decimal("4.50")})),
            "the 1.0 scenario declares",
            id="3-baseline-fidelity",
        ),
        pytest.param(
            MULTIPLIERS,
            ("1.5", _costs({"slippage_points_per_side": Decimal("0.5")})),
            "at that multiplier is",
            id="4-scenario-fidelity",
        ),
        pytest.param(
            MULTIPLIERS,
            ("1", _ending_at(DRIFTED_END)),
            "the protocol declares",
            id="5-window-fidelity",
        ),
        pytest.param(
            MULTIPLIERS, ("1.5", _moving("trades", None)), "proposal_ids", id="6-identity"
        ),
    ],
)
@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_each_of_the_six_checks_refuses_a_grid_of_real_sealed_bundles(
    seeded: Fixture,
    research_env: Path,
    research_ledger_dsn: str,
    tmp_path: Path,
    levels: tuple[str, ...],
    tamper: tuple[str, Callable[[dict[str, Any]], None]] | None,
    expected: str | None,
) -> None:
    """Six refusals, six causes, and one grid they were all built from.

    The control case is the first row of the table and is the reason the other
    six mean anything: the same three real replays, the same ``start`` and
    ``record`` calls, with no document changed, come out of ``scenario-report``
    clean. Without it, six refusals off a grid nothing ever accepted would be
    indistinguishable from six refusals that each named their own check.

    Each refusing case is a *real sealed bundle in a real evidence root*. The
    tampered document is written by ``research trial record``, which puts it on
    disk at its own address and appends the two rows that name it, so the report
    is refusing a set the chain genuinely holds rather than a set a test handed
    it. Every tamper is one the model accepts whole -- the unit suite proved
    that -- so a refusal here is the report's and not pydantic's.

    The completeness case is a grid that was *never finished* rather than one with
    something removed afterwards, and the identity case moves a trade's
    ``proposal_id`` rather than a digest, because the sequence is the clause the
    spread stress implies: scaling the spread moves every price and must not move
    which trades happened. The digest clause is reached end to end by
    ``tests/integration/research/test_scenarios.py``, whose identity case is the
    only way to build a complete grid with two different digests in it -- the
    orchestrator derives one digest for all three levels, so a second document at
    an occupied level is a completeness defect instead.

    The chain still verifies in every case, and that is asserted rather than
    assumed: these refusals are about the *evidence* not matching the
    *registration*, which is a different exit code from a document that is
    missing or altered, and the two have different remedies.
    """

    _register(tmp_path, seeded)
    _seal_grid(seeded, tmp_path, levels=levels, tamper=tamper)
    result = _scenario_report()

    if expected is None:
        assert result.exit_code == cli.ExitCode.OK, result.stderr
        report = ScenarioReport.model_validate_json(json.dumps(_envelope(result)))
        assert [str(row.multiplier) for row in report.scenarios] == list(levels)
        assert runner.invoke(cli.app, ["research", "trial", "verify"]).exit_code == cli.ExitCode.OK
        return

    _assert_refused(result, cli.ExitCode.SCENARIO_EVIDENCE)
    cause = _refusal_cause(research_ledger_dsn, research_env)
    assert expected in cause, cause
    assert "correlation_id" not in cause, "the private cause stays a log reader's"

    verified = runner.invoke(cli.app, ["research", "trial", "verify"])
    assert verified.exit_code == cli.ExitCode.OK, verified.stderr
    assert json.loads(verified.stdout)["valid"] is True


# --- 5. the orchestrator's baseline is the run an operator would type ----------


@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_at_acceptance_the_orchestrators_baseline_is_the_run_an_operator_would_type(
    seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    """Two simulators, one run, compared on the result rather than on success.

    ``research trial scenarios`` and ``backtest run`` share one ``simulate`` and
    one ``seal_bundle``, which is the refactor that makes this comparison
    possible at all -- and comparing them is the only way to know the sharing did
    not change what either one does. Asserting merely that both succeeded would
    be worth nothing: two different simulators both succeed.

    The equality is on ``source_result_sha256``, the result's own
    declaration-ordered digest, so it is the *experiment* that matches and not
    the envelope. The two documents are still different files, because the
    bundle names the attempt and the orchestrator mints its own; a "fix" that
    made the evidence addresses equal by dropping the identity would fail the
    last assertion here.
    """

    protocol_path = _register(tmp_path, seeded)
    payload = _scenarios(seeded, protocol_path)
    hand = _bundle_of(_run(seeded, marked=True, **{"--stress-multiplier": "1"}))

    baseline = payload["report"]["scenarios"][0]
    assert Decimal(baseline["multiplier"]) == Decimal(1)
    assert hand.result.cost_model.stress_multiplier == Decimal(1)
    assert baseline["source_result_sha256"] == hand.source_result_sha256
    assert baseline["evidence_sha256"] != canonical_sha256(hand)
    assert _store(research_env).read(baseline["evidence_sha256"]).result == hand.result


# --- 6. the counters -----------------------------------------------------------


@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_the_grid_is_one_selection_lottery_whatever_it_costs_to_run(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    """The denominator, counted from the chain rather than asserted in prose.

    One candidate examined at three cost levels is **three audit attempts and
    one selection lottery**, and the second number is the one the Deflated Sharpe
    divides by: the grid is a sensitivity probe of one specification, not three
    specifications tried, and inflating the count would deflate a ratio for
    having done its homework.

    The count is read from ``research trial count``, which counts from the chain,
    so the assertion is about the record rather than about anything the
    orchestrator believes. The two other properties that would break it -- a trial
    id or a specification digest minted per multiplier -- are asserted on the
    chain rows themselves, because a counter that read 1 here could still have
    been arrived at by three candidates each examined once.
    """

    protocol_path = _register(tmp_path, seeded)
    payload = _scenarios(seeded, protocol_path)

    counters = _counters()
    assert counters == {
        "status": "ok",
        "audit_attempts": 3,
        "selection_lotteries": 1,
        "effective_specifications": 1,
    }
    started = [
        record
        for record in _ledger(research_ledger_dsn).events_for(TRIAL_ID)
        if record.event_type is LedgerEventType.EXECUTION_STARTED
    ]
    assert [record.attempt_id for record in started] == ATTEMPT_IDS
    assert len({record.trial_id for record in started}) == 1
    assert {record.spec_sha256 for record in started} == {_spec_sha256(seeded)}
    assert [row["attempt_id"] for row in payload["scenarios"]] == ATTEMPT_IDS


# --- 7. a registration cannot be amended, and the refusal is late --------------


@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_a_protocol_file_edited_after_registration_is_refused_before_any_write(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    """The edit 8B2b could only refuse late is refused before anything is written (8B3, C-10).

    ``research trial scenarios`` reads the grid and the money terms from the file
    it is given, and ``scenario-report`` re-derives its own from the sealed
    registration. Until 8B3 an operator who edited their copy got three sealed
    documents and nine rows before the report refused. The command now compares
    the file's canonical digest with the registered protocol's first, so the chain
    holds only the registration and the evidence root is empty.

    The last block is unchanged from the late-refusal version of this case: an
    operator's copy is a file, and the chain still holds, byte for byte, the
    protocol that was registered.
    """

    protocol_path = _register(tmp_path, seeded)
    registered = _protocol(seeded)
    # The edit is to a money term, not to the multipliers: ``CostSpec`` pins the
    # stressed levels to exactly {1.5, 2}, so a grid's *levels* cannot be edited
    # even in a file. What can differ is the baseline they are multiples of.
    document = json.loads(protocol_path.read_text(encoding="utf-8"))
    document["costs"]["baseline"]["commission_per_lot_per_side"] = "4.50"
    protocol_path.write_text(json.dumps(document), encoding="utf-8")

    result = runner.invoke(cli.app, _scenario_args(seeded, protocol_path))

    _assert_refused(result, cli.ExitCode.SCENARIO_EVIDENCE)
    assert len(_ledger(research_ledger_dsn).events()) == 1
    assert not list(research_env.rglob("*.json"))
    assert declared_grid(registered) == tuple(Decimal(level) for level in MULTIPLIERS)
    assert registered_protocol(_ledger(research_ledger_dsn).replay(), TRIAL_ID) == registered
    assert registered.costs.baseline.commission_per_lot_per_side == Decimal("3.50")


# --- 8. the pinned digests -----------------------------------------------------


def test_the_four_pinned_digests_are_still_exactly_these_literals() -> None:
    """The constants 8B2b's own subject could most plausibly have moved.

    ``test_phase8b1.py`` asserts these and does not need saying again -- except
    that 8B2b is a phase about *costs*, and the one of the four that is
    recomputed from a live model is the bundle digest. A cost term reaching a
    bundle is precisely the change that would move it, and moving it would make
    every v1 document already sealed in an operator's store unreadable, because
    ``EvidenceStore.read`` re-serialises what it decoded.

    The three Phase 7 digests are the other kind of constant: they name runs
    against a bar store that was deleted afterwards, on the one machine that ran
    them, so a test can only refuse to disagree with them silently. They are kept
    for the same reason 8B1 kept them -- a digest pinned to an arm the registry
    records as losing money is a record of a completed experiment, and one pinned
    to an open question would be a promise.
    """

    assert set(PHASE7_RESULT_DIGESTS) == {"none", "fixed_target", "chandelier"}
    for arm, digest in PHASE7_RESULT_DIGESTS.items():
        assert len(digest) == 64
        assert f"{arm}_digest={digest}" in SESSION_MOMENTUM_SPEC.versioning
    assert SESSION_MOMENTUM_SPEC.trial_count == 3

    # The one of the four a reader can re-derive, and therefore the one that goes
    # red when the encoding moves rather than merely when a literal is edited.
    known_answer = _bundle()
    assert known_answer.result_schema_version == 1
    assert _CANONICAL_BUNDLE_SHA256 == V1_BUNDLE_SHA256
    assert canonical_sha256(known_answer) == V1_BUNDLE_SHA256


# --- 9. the operator-facing text ----------------------------------------------


DECISION_COMMANDS = frozenset({"decide", "report", "holdout", "open-holdout"})
"""The only trial commands whose help may speak in decisions, and why.

Phase 8D1 is the first slice that judges a candidate, and the three commands that exist to
say what was judged cannot describe themselves without the vocabulary: ``decide`` records
a decision, ``report`` prints the report of the last one, and ``holdout`` prints the state
a decision's gate two reads. Phase 8D2 adds ``open-holdout``: its help must name the decision
that gates it and the holdout state it creates, which is the same vocabulary. This is a NAMED
exception, not a loosening: every other trial
command stays under the total ban below, and ``test_every_trial_command_is_banned_a_named_
decision_command_or_a_ledger_one`` fails when a command is in neither list,
so a new command cannot slip out from under the rule by being forgotten."""

TOTAL_BAN_COMMANDS = (
    ["scenarios"],
    ["scenario-report"],
    # 8B3: the three commands it adds are held to the same total rule.
    ["compounding"],
    ["compounding-report"],
    ["capacity"],
    # 8C1: the read command that prints the folds and splits.
    ["splits"],
    # 8C3: the read command that assembles every statistical measurement.
    ["validate"],
)


def test_every_trial_command_is_banned_a_named_decision_command_or_a_ledger_one() -> None:
    """The two lists together are exactly the trial commands that report or run evidence.

    The commands outside both are the ledger's own (register, start, record, import-legacy,
    show, count, verify), whose help predates the rule and says nothing about candidates.
    """

    registered = {command.name for command in cli.trial_app.registered_commands}
    banned = {command[0] for command in TOTAL_BAN_COMMANDS}
    ledger_commands = {"register", "start", "record", "import-legacy", "show", "count", "verify"}

    assert banned.isdisjoint(DECISION_COMMANDS)
    assert registered == banned | DECISION_COMMANDS | ledger_commands


@pytest.mark.parametrize("command", TOTAL_BAN_COMMANDS)
def test_no_report_commands_help_text_claims_a_verdict(command: list[str]) -> None:
    """The gate the framework's premise actually needs.

    No slice fixes a threshold after seeing results. A test that greps the
    operator-facing text for the vocabulary of a decision is the only thing that
    keeps 8B2b from quietly becoming one, because nothing else here would notice:
    a report that grew a ``passes`` field would be caught by the key test, and a
    command whose *documentation* started saying "a candidate survives the grid at
    1.5x if…" would be caught by nothing.

    The rule is total and has no exception: no word of decision vocabulary may
    appear in the help text at all, however it is spelled or cased. The rationale,
    the list, and the ``scenario-report`` docstring wording in ``cli.py`` that makes
    a rule this strict possible are all above; the guard on the guard is
    ``test_the_verdict_gate_fires_on_a_claim_however_it_is_worded`` below.
    """

    result = runner.invoke(cli.app, ["research", "trial", *command, "--help"])

    assert result.exit_code == 0, result.stderr
    assert _verdict_claims(result.stdout) == []


@pytest.mark.parametrize(
    "claim",
    [
        "The candidate survives the declared grid.",
        "A promotion gate applies at a 2.0x threshold.",
        "Survives the grid; 8D decides.",  # a capital, where a capital goes
        "We recommend this one.",
        "The candidate is approved for paper trading.",
        "This candidate is rejected at 1.5x.",
        "Prints a verdict for each level.",  # the word the docstring used to spend
        "The candidate passes the declared grid.",  # the likeliest phrasing of all
        "This candidate fails at 1.5x.",
        "The candidate is acceptable at 1.0x.",
        "Two levels of eligibility remain.",
        # The loophole this gate used to have: a denial and a claim in one
        # sentence used to license each other.
        "There is no verdict here: a candidate that survives the grid is promoted.",
        "This is not the same as backtest run, and a candidate that survives the grid is promoted.",
    ],
)
def test_the_verdict_gate_fires_on_a_claim_however_it_is_worded(claim: str) -> None:
    """The detector has to fire, or "no verdict words" is a statement about a regex.

    A gate with no demonstration of its own sensitivity is a gate nobody knows is
    armed, and a gate demonstrated only on its two most obvious stems would stay
    green with the other four deleted from it -- which are exactly the ones a
    later slice would reach for. So this is parametrised over **every** stem
    rather than a convenient sample, plus the two shapes that defeated the
    previous version of it: a leading capital, which the matcher lowercases for,
    and a denial wrapped around a claim, which it no longer excuses.

    Each case fails if one specific thing is removed: ``.lower()`` drops takes
    three of them down, restoring the denial exception takes the last two, and
    deleting any one stem from ``_NO_VERDICT_HELP`` takes down the case naming it.
    """

    assert _verdict_claims(claim) != []
