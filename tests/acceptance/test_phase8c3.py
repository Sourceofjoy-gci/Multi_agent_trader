"""Phase 8C3 acceptance: ``research trial validate`` over real sealed bundles.

The command assembles every 8C measurement into one document. This file claims, against
real PostgreSQL and a real evidence store:

1. **it reads what the chain holds, and the figures agree with an independent reading.**
   The baseline drawdown, the compounding drawdown and the stressed expectancies are
   recomputed here, in plain loops over the sealed documents, from nothing the command
   computed; the per-path trade counts equal the ``splits`` command's.
2. **every measurement is finite or says why, and names its evidence**, and the digests it
   names are the chain's own.
3. **it is read only and byte-deterministic**: row and file counts do not move, and a second
   run prints the same bytes.
4. **it refuses as ``splits`` does**: an unregistered trial, a registered one with nothing
   sealed, and a trial with two sealed 1.0x baselines, each with nothing written.
5. **no decision vocabulary**, in keys, values and help; and **the README says what is true**.

Fixtures are the 8C1 acceptance's own (a real 35-day bar store and the real orchestrator).
"""

# ruff: noqa: F811
from __future__ import annotations

import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.acceptance.test_phase8b2b import (
    _NO_VERDICT_HELP,
    _assert_refused,
    _envelope,
    _names_and_text,
)
from tests.acceptance.test_phase8c1 import (  # noqa: F401
    ORIGIN,
    SERIES_DAYS,
    _flow,
    _splits,
    long_seeded,
)
from tests.integration.research.conftest import research_env  # noqa: F401
from tests.integration.research.test_backtest_evidence import Fixture, _record, _run, _write
from tests.integration.research.test_compounding import _compounding, _files, _rows
from tests.integration.research.test_scenarios import (
    OTHER_TRIAL_ID,
    TRIAL_ID,
    _foreign_baseline_bundle,
    _protocol,
    _register,
    _scenarios,
)
from tests.integration.research.test_trial_cli import _ledger, _start, _store
from tests.unit.ops.test_scenarios import _NO_VERDICT
from trading_house import cli
from trading_house.core.errors import ScenarioEvidenceError
from trading_house.ops.ledger import seal_bundle
from trading_house.ops.validate import read_validation_inputs, statistical_evidence
from trading_house.research.canonical import canonical_sha256
from trading_house.research.evidence import EvidenceBundle

runner = CliRunner()

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("isolated_research_ledger")]


def _validate(trial_id: str = TRIAL_ID) -> Any:
    return runner.invoke(cli.app, ["research", "trial", "validate", "--trial-id", trial_id])


def _measurements(value: object) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if {"value", "undefined_reason", "evidence_sha256"} <= set(value):
            found.append(value)
        for item in value.values():
            found.extend(_measurements(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_measurements(item))
    return found


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(k) for k in value} | set().union(*(_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(v) for v in value))
    return set()


def _marked_drawdown(equities: list[Decimal]) -> float:
    """The largest fall from the running peak, in a plain loop over Decimals."""

    peak, worst = equities[0], Decimal(0)
    for equity in equities:
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak)
    return float(worst)


def _equities(bundle: Any) -> list[Decimal]:
    series = bundle.mark_to_market
    return [series.firm_equity, *(point.equity for point in series.observations)]


def _mean_net(bundle: Any) -> float:
    trades = bundle.result.trades
    return float(sum((t.net_pnl for t in trades), Decimal(0)) / len(trades))


# --- 1. the command reads the chain's own sealed runs ---------------------------------


def test_validate_agrees_with_an_independent_reading_of_the_sealed_documents(
    long_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    protocol_path = _register(tmp_path, long_seeded)
    grid = _scenarios(long_seeded, protocol_path)["report"]["scenarios"]
    digest_of = {s["multiplier"]: s["evidence_sha256"] for s in grid}
    compounding = _compounding(long_seeded, protocol_path)
    assert compounding.exit_code == cli.ExitCode.OK, compounding.stderr
    compounding_digest = json.loads(compounding.stdout)["evidence_sha256"]
    store = _store(research_env)
    base = store.read(digest_of["1"])
    comp = store.read(compounding_digest)
    rows, files = _rows(research_ledger_dsn), _files(research_env)

    result = _validate()

    assert result.exit_code == cli.ExitCode.OK, result.stderr
    payload = _envelope(result)
    assert payload["trial_id"] == TRIAL_ID
    assert payload["spec_sha256"] == canonical_sha256(_protocol(long_seeded).candidates[0])
    assert payload["attempt_id"] == "grid-1"
    assert payload["basis_is_mark_to_market"] is True
    assert (payload["first_day"], payload["last_day"], payload["series_days"]) == (
        "2026-09-21",
        "2026-10-26",
        SERIES_DAYS,
    )
    assert payload["evidence"]["baseline"] == digest_of["1"]
    assert payload["evidence"]["compounding"] == compounding_digest
    assert payload["evidence"]["pbo_candidates"] == [digest_of["1"]]
    assert payload["evidence"]["chain_head"] == _ledger(research_ledger_dsn).events()[-1].event_hash
    assert payload["capacity"]["status"] == "unavailable"

    # the drawdowns are recomputed from the sealed equity series with a plain loop
    assert base.mark_to_market is not None
    assert comp.mark_to_market is not None
    assert payload["max_drawdown_baseline"]["value"] == pytest.approx(
        _marked_drawdown(_equities(base)), abs=1e-12
    )
    assert payload["max_drawdown_baseline"]["evidence_sha256"] == [digest_of["1"]]
    assert payload["max_drawdown_compounding"]["value"] == pytest.approx(
        _marked_drawdown(_equities(comp)), abs=1e-12
    )
    assert payload["max_drawdown_compounding"]["evidence_sha256"] == [compounding_digest]
    assert len(payload["max_drawdown_cpcv_paths"]) == 5

    # the stressed expectancies are the means of the stressed bundles' own trades
    by_level = {s["multiplier"]: s for s in payload["scenario_expectancy"]}
    assert sorted(by_level) == ["1.5", "2"]
    for level in ("1.5", "2"):
        stressed = store.read(digest_of[level])
        assert stressed.result.trades, "a run that never traded proves nothing about expectancy"
        assert by_level[level]["evidence_sha256"] == digest_of[level]
        assert by_level[level]["expectancy"]["evidence_sha256"] == [digest_of[level]]
        assert by_level[level]["expectancy"]["value"] == pytest.approx(
            _mean_net(stressed), abs=1e-9
        )

    # CPCV: the per-path kept counts are the splits command's, and the p5 is always computed
    splits = _envelope(_splits())
    assert [p["trades_kept"] for p in payload["cpcv_p5"]["paths"]] == [
        p["trades_kept"] for p in splits["cpcv"]["paths"]
    ]
    assert payload["cpcv_p5"]["paths_differ"] is splits["cpcv"]["paths_differ"]
    assert payload["cpcv_p5"]["p5"]["value"] is not None
    if not payload["cpcv_p5"]["paths_differ"]:
        # identical paths: the 5th percentile is the aggregate expectancy of the baseline's trades
        assert payload["cpcv_p5"]["p5"]["value"] == pytest.approx(_mean_net(base), abs=1e-9)
    assert payload["cpcv_p5"]["quantile"] == 0.05
    assert payload["cpcv_p5"]["closed_trades"] == len(base.result.trades)
    assert payload["coverage"]["closed_trades"] == len(base.result.trades)
    assert payload["cpcv_p5"]["all_trades_kept"] is not payload["cpcv_p5"]["paths_differ"]
    assert payload["coverage"]["all_trades_kept"] is payload["cpcv_p5"]["all_trades_kept"]
    assert payload["dsr"]["cross_section_count"] == 1  # one candidate, so no cross-section
    assert payload["dsr"]["cross_section"] is None
    assert payload["coverage"]["declared_labels"] == ["london", "new_york"]

    # the registered strategy's horizon is read without a run: 32,400 s is one day
    assert payload["dsr"]["horizon_days"] == 1
    assert payload["dsr"]["trials"] == 1
    assert payload["dsr"]["dsr"]["value"] == payload["psr"]["value"]  # N = 1 is PSR
    assert payload["monte_carlo"]["horizon_days"] == SERIES_DAYS
    assert payload["monte_carlo"]["replicates"] == payload["bootstrap"]["replicates"] == 10000
    assert payload["monte_carlo"]["policy_version"] == "8c-mc-1"
    assert payload["bootstrap"]["policy_version"] == "8c-sb-1"
    assert payload["monte_carlo"]["seed"] != payload["bootstrap"]["seed"]
    assert payload["pbo"]["pbo"]["value"] is None
    assert payload["pbo"]["excluded"] == [OTHER_TRIAL_ID]
    assert payload["wfa_folds"]["value"] is None
    assert (
        "no fold of 24 training, 6 validation and 6 test months"
        in (payload["wfa_folds"]["undefined_reason"])
    )

    # every measurement is finite or says why, and names evidence the chain holds
    chain_digests = set(digest_of.values()) | {compounding_digest}
    found = _measurements(payload)
    assert len(found) >= 20
    for item in found:
        assert (item["value"] is None) != (item["undefined_reason"] is None)
        if item["value"] is not None:
            assert math.isfinite(item["value"])
        assert item["evidence_sha256"]
        assert set(item["evidence_sha256"]) <= chain_digests

    # read only
    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows, files)


def test_the_output_is_byte_deterministic_and_the_command_writes_nothing(
    long_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    _flow(long_seeded, tmp_path)
    rows, files = _rows(research_ledger_dsn), _files(research_env)

    first, second = _validate(), _validate()

    assert first.exit_code == second.exit_code == cli.ExitCode.OK
    assert first.stdout == second.stdout
    assert len(first.stdout) > 1000
    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows, files)


def test_without_a_compounding_run_that_drawdown_is_undefined_and_the_rest_is_not(
    long_seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    _flow(long_seeded, tmp_path)

    payload = _envelope(_validate())

    assert payload["evidence"]["compounding"] is None
    assert payload["max_drawdown_compounding"]["value"] is None
    assert payload["max_drawdown_compounding"]["undefined_reason"] == "no compounding run is sealed"
    assert payload["max_drawdown_baseline"]["value"] is not None


def test_two_sealed_candidates_are_both_ranked_and_the_counters_follow(
    long_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    protocol_path = _register(tmp_path, long_seeded)
    _scenarios(long_seeded, protocol_path)
    _scenarios(long_seeded, protocol_path, trial_id=OTHER_TRIAL_ID, attempt_prefix="other")

    payload = _envelope(_validate())

    assert len(payload["evidence"]["pbo_candidates"]) == 2
    assert payload["pbo"]["candidates"] == [TRIAL_ID, OTHER_TRIAL_ID]
    assert payload["pbo"]["excluded"] == []
    assert payload["counters"]["selection_lotteries"] == 2
    assert payload["dsr"]["trials"] == 2
    assert 0.0 < payload["dsr"]["expected_max_z"] < 1.0
    assert set(payload["pbo"]["pbo"]["evidence_sha256"]) == set(
        payload["evidence"]["pbo_candidates"]
    )


# --- 4. refusals --------------------------------------------------------------------


def test_an_unregistered_trial_and_a_registered_one_with_nothing_sealed_are_refused(
    long_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    unregistered = _validate("nobody")
    _register(tmp_path, long_seeded)
    rows, files = _rows(research_ledger_dsn), _files(research_env)
    unsealed = _validate(TRIAL_ID)

    _assert_refused(unregistered, cli.ExitCode.SCENARIO_EVIDENCE)
    _assert_refused(unsealed, cli.ExitCode.SCENARIO_EVIDENCE)
    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows, files)


def test_two_sealed_baselines_are_refused_rather_than_one_being_chosen(
    long_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    protocol = _protocol(long_seeded)
    _flow(long_seeded, tmp_path)
    _record(
        _foreign_baseline_bundle(long_seeded, canonical_sha256(protocol.candidates[0]), tmp_path),
        attempt_id="foreign-1.0",
    )
    rows, files = _rows(research_ledger_dsn), _files(research_env)

    result = _validate()

    _assert_refused(result, cli.ExitCode.SCENARIO_EVIDENCE)
    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows, files)


def test_a_run_under_thirty_days_is_refused_as_a_statistical_input(
    long_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    from datetime import timedelta

    ten_days = long_seeded._replace(last_bar=ORIGIN + timedelta(days=10) - timedelta(minutes=15))
    _flow(ten_days, tmp_path)
    rows, files = _rows(research_ledger_dsn), _files(research_env)

    result = _validate()

    _assert_refused(result, cli.ExitCode.STATISTICAL_INPUT)
    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows, files)


def test_a_baseline_that_is_not_the_protocols_candidate_is_refused_and_nothing_is_written(
    long_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    """Sealed with another candidate's specification digest: ``validate`` runs the same
    faithfulness checks the sibling reports run, refuses with the same exit code, writes
    nothing, and says which clause."""

    protocol = _protocol(long_seeded)
    _register(tmp_path, long_seeded)
    honest = canonical_sha256(protocol.candidates[0])
    drifted = canonical_sha256(protocol.candidates[1])
    assert _start(trial_id=TRIAL_ID, attempt_id="grid-1", spec_sha256=honest).exit_code == 0
    _record(
        _write(
            tmp_path / "drifted.json",
            _run(
                long_seeded,
                marked=True,
                trial_id=TRIAL_ID,
                attempt_id="grid-1",
                **{"--stress-multiplier": "1", "--spec-sha256": drifted},
            ),
        ),
        attempt_id="grid-1",
    )
    rows, files = _rows(research_ledger_dsn), _files(research_env)

    result = _validate()

    _assert_refused(result, cli.ExitCode.SCENARIO_EVIDENCE)
    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows, files)
    with pytest.raises(ScenarioEvidenceError) as refusal:
        statistical_evidence(
            read_validation_inputs(TRIAL_ID, _ledger(research_ledger_dsn), _store(research_env))
        )
    assert "declares spec" in str(refusal.value.__cause__)


def test_a_bundle_run_on_an_opened_holdout_changes_nothing_for_any_read(
    long_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    """A second 1.0x bundle, sealed with its holdout OPENED, sits in the trial. Every read
    that selects bundles ignores it: ``scenario-report``, ``splits``, ``compounding-report``
    and ``validate`` print what they printed before, and none refuses it as a duplicate."""

    protocol = _protocol(long_seeded)
    protocol_path = _register(tmp_path, long_seeded)
    _scenarios(long_seeded, protocol_path)
    assert _compounding(long_seeded, protocol_path).exit_code == cli.ExitCode.OK
    reads = (
        ["scenario-report", "--trial-id", TRIAL_ID],
        ["splits", "--trial-id", TRIAL_ID],
        ["compounding-report", "--trial-id", TRIAL_ID],
        ["validate", "--trial-id", TRIAL_ID],
    )
    before = [runner.invoke(cli.app, ["research", "trial", *argv]) for argv in reads]
    assert [r.exit_code for r in before] == [0, 0, 0, 0]

    document = _foreign_baseline_bundle(
        long_seeded, canonical_sha256(protocol.candidates[0]), tmp_path
    )
    payload = json.loads(document.read_text(encoding="utf-8"))
    payload["bundle"]["provenance"]["holdout_state"] = "opened"
    document.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    # ``record`` now refuses an opened bundle (8D2 review), so it is sealed through the lower-level
    # ``seal_bundle`` that ``open-holdout`` itself uses: a named test path, not a command.
    seal_bundle(
        EvidenceBundle.model_validate_json(json.dumps(payload["bundle"])),
        ledger=_ledger(research_ledger_dsn),
        store=_store(research_env),
    )
    assert len(_store(research_env).read(_ledger_digests(research_ledger_dsn)[-1]).daily_returns)

    after = [runner.invoke(cli.app, ["research", "trial", *argv]) for argv in reads]

    assert [r.exit_code for r in after] == [0, 0, 0, 0]
    assert [r.stdout for r in after[:3]] == [r.stdout for r in before[:3]]

    def without_head(result: Any) -> dict[str, Any]:
        # the one thing that must move: the chain has one more row, so its head is new
        document = json.loads(result.stdout)
        assert document["evidence"].pop("chain_head") != ""
        document["dsr"].pop("chain_head_sha256")
        return document

    assert without_head(after[3]) == without_head(before[3])
    assert (
        json.loads(after[3].stdout)["evidence"]["chain_head"]
        != json.loads(before[3].stdout)["evidence"]["chain_head"]
    )


def _ledger_digests(dsn: str) -> list[str]:
    """The evidence digests the chain names, in chain order."""

    return [
        record.event_json["payload"]["evidence_sha256"]
        for record in _ledger(dsn).events()
        if record.event_type.value == "evidence_sealed"
    ]


def test_the_counters_and_the_head_come_from_one_read_of_the_chain(
    long_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    ledger = _ledger(research_ledger_dsn)
    assert ledger.snapshot() == ((), None)
    with pytest.raises(ScenarioEvidenceError) as empty:
        read_validation_inputs(TRIAL_ID, ledger, _store(research_env))
    assert "the chain holds no event" in str(empty.value.__cause__)
    _flow(long_seeded, tmp_path)

    events, head = ledger.snapshot()

    assert head == ledger.events()[-1].event_hash
    assert events == ledger.replay()
    assert len(events) == len(ledger.events())
    assert (
        statistical_evidence(
            read_validation_inputs(TRIAL_ID, ledger, _store(research_env))
        ).evidence.chain_head
        == head
    )


def test_validate_takes_no_option_the_protocol_owns() -> None:
    result = runner.invoke(
        cli.app, ["research", "trial", "validate", "--trial-id", "t", "--protocol", "p.json"]
    )

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert "No such option" in result.stderr


# --- 5. no decision vocabulary ------------------------------------------------------------


def test_the_output_carries_no_decision_vocabulary(
    long_seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    _flow(long_seeded, tmp_path)
    document = _envelope(_validate())

    names = _names_and_text(document)
    keys = _keys(document)
    assert names, "a document with no keys or values would pass vacuously"
    assert "basis_is_mark_to_market" in keys
    assert "promotion_grade" not in keys
    assert [
        key for key in keys for word in (*_NO_VERDICT_HELP, "total") if word in key.lower()
    ] == []
    assert sorted(text for text in names for word in _NO_VERDICT if word in text.lower()) == []
    assert not any("threshold" in text.lower() for text in names)


# --- the README and the spec --------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")
SPEC = (
    ROOT / "docs" / "superpowers" / "specs" / "2026-10-01-phase-8c-statistical-validation-design.md"
).read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(heading: str) -> str:
    start = README.index(heading)
    end = README.find("\n## ", start + 1)
    return README[start : end if end != -1 else len(README)]


def test_the_readme_documents_the_command_in_the_table_and_the_section() -> None:
    rows = [
        line
        for line in README.splitlines()
        if line.startswith("| `") and "trading-house research trial validate`" in line
    ]

    assert len(rows) == 1
    assert README.index("## Phase 8C2") < README.index("## Phase 8C3")
    assert "research trial validate --trial-id T" in _flat(_section("## Phase 8C3"))


def test_the_readme_states_what_8c3_measures_and_what_it_resolved() -> None:
    section = _flat(_section("## Phase 8C3"))

    for sentence in (
        "It adds **no gate, no verdict and no cutoff decision**",
        "`MAX_DRAWDOWN = 0.10`",
        '`MC_POLICY_VERSION = "8c-mc-1"`',
        "open-position marks included and never closed trades",
        "has an undefined drawdown with the reason, never one computed from trades",
        "`>= MAX_DRAWDOWN - DRAWDOWN_TOLERANCE`",
        "`DRAWDOWN_TOLERANCE = 1e-12`",
        "the future gate passes only when the drawdown is `< MAX_DRAWDOWN - DRAWDOWN_TOLERANCE`",
        "an exact 10% fall from a flat start measures 0.09999999999999998",
        "whose basis is not mark-to-market (`REALIZED_CLOSED_TRADES`), has an undefined drawdown",
        "whose FINAL equity is strictly below 1.0",
        "`ceil(CPCV_P5_QUANTILE * n_paths)`",
        "Coverage takes the path with the FEWEST kept trades",
        "That sample is the CPCV-kept sample, not a holdout.",
        "`closed_trades` (the closed trades of the baseline run) and `all_trades_kept`",
        "**not** a locked out-of-sample one",
        "**The CPCV 5th-percentile measurement is always computed (amended 2026-10-01).**",
        "`paths_differ` is carried beside it as a plain diagnostic",
        "**Gate 5 therefore reads the net expectancy of the full sealed research-window "
        "sample when the paths are identical, and 8D must say so in its reason.**",
        "that sample is in-sample on the research window, not out-of-sample evidence",
        "`cross_section_count` says how many entered",
        "through the one shared helper `refuse_unfaithful`",
        "taken from ONE read of the chain",
        "A bundle sealed after its holdout was OPENED or CONSUMED is a different experiment",
        "**CPCV path drawdowns are identical by construction**",
        "**Regimes are recognised, not vouched.**",
        "**A PBO candidate sealed over other days refuses the whole command** (exit 20)",
        "the reason says which of the two it was",
        "The registered session momentum strategy declares 32,400 seconds, one day, so its "
        "DSR is defined",
        "one day is never assumed",
        "an undefined measurement is data, not an error",
    ):
        assert sentence in section, sentence


def test_the_8c1_text_that_said_the_measurement_would_be_undefined_is_replaced() -> None:
    eight_c1 = _flat(_section("## Phase 8C1"))

    assert "a measurement over the paths has nothing to say" not in eight_c1
    assert (
        "the CPCV 5th-percentile measurement is still computed (amended 2026-10-01 by 8C3"
        in eight_c1
    )
    assert "`paths_differ`" in eight_c1


def test_the_spec_section_seven_carries_the_amended_p5_and_horizon_sentences() -> None:
    spec = _flat(SPEC)

    assert "must be `undefined` when `paths_differ` is false" not in spec.replace(
        "reversing the 8C1 note that the CPCV p5 measurement must be `undefined` when "
        "`paths_differ` is false:*",
        "",
    )
    for sentence in (
        "*Amended 2026-10-01 (8C3), reversing the 8C1 note",
        "The measurement is always computed and `paths_differ` is carried beside it as a "
        "plain diagnostic.",
        "8D must say so in its reason for gate 5.",
        "A horizon that cannot be read is unknown and makes DSR undefined",
    ):
        assert sentence in spec, sentence
