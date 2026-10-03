"""Phase 8E acceptance: the computed dataset digest, against real PostgreSQL.

What is proved, with the real bar store, the real ledger and the real evidence store:

1. ``research dataset digest`` prints the digest a real run seals, and an independent
   restatement of the digest rule agrees with both;
2. a protocol declaring that digest runs, and every bundle it seals carries it;
3. a protocol declaring any other hash (a placeholder, a typo) is refused before any write by
   ``scenarios``, ``compounding`` and ``open-holdout``: ledger rows and evidence files unchanged;
4. a store that changes between the pre-flight and the run is caught after the run, and nothing is
   sealed from it;
5. ``decide`` clears "dataset-content hash is unavailable" for a baseline carrying the declared
   digest, keeps it for one carrying none (a run sealed before 8E), and refuses one carrying
   another digest as a promotion refusal, with ``validate`` refusing the same bundle as a scenario
   evidence failure;
6. a legacy import still says ``unavailable``;
7. the command's help states no verdict, in a named set of its own, and the README says what is
   now true and what the digest does not prove.
"""

# ruff: noqa: F811
from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.acceptance.test_phase8b2b import (
    _assert_refused,
    _envelope,
    _seal_grid,
)
from tests.acceptance.test_phase8c1 import long_seeded  # noqa: F401
from tests.acceptance.test_phase8d1 import _legacy_trial
from tests.acceptance.test_phase8d2 import (
    HOLDOUT_HASH,
    _decide,
    _holdout,
    _locked_protocol,
    _open,
    _state,
    _synthetic,
    opening_seeded,  # noqa: F401
)
from tests.acceptance.test_phase8d2 import _register as _register_protocol
from tests.dataset_digest import declared_digest
from tests.integration.research.conftest import research_env  # noqa: F401
from tests.integration.research.test_backtest_evidence import Fixture, _run, seeded  # noqa: F401
from tests.integration.research.test_compounding import _compounding
from tests.integration.research.test_scenarios import (
    TRIAL_ID,
    _protocol,
    _register,
    _scenario_args,
    _scenarios,
)
from tests.integration.research.test_trial_cli import _ledger, _store
from tests.unit.research.backtest.conftest import FakeBarReader
from tests.unit.research.backtest.test_dataset import _reference
from trading_house import cli
from trading_house.core.errors import PromotionRefusedError
from trading_house.ops.decide import decide_trial
from trading_house.ops.scenarios import sealed_bundles
from trading_house.research.promotion import Decision
from trading_house.research.trial_ledger import TrialProtocol

runner = CliRunner()

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("isolated_research_ledger")]

PLACEHOLDER = "a" * 64
OTHER = "9" * 64
REASON = "dataset-content hash is unavailable"
README = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")


def _digest_args(
    seeded: Fixture, *, start: Any = None, end: Any = None, instrument: str = "fx.eurusd"
) -> list[str]:
    return [
        "research",
        "dataset",
        "digest",
        "--instrument",
        instrument,
        "--timeframe",
        "M15",
        "--start",
        (seeded.first_bar if start is None else start).strftime("%Y-%m-%dT%H:%M:%S"),
        "--end",
        (seeded.last_bar if end is None else end).strftime("%Y-%m-%dT%H:%M:%S"),
    ]


def _digest(seeded: Fixture, **kwargs: Any) -> Any:
    return runner.invoke(cli.app, _digest_args(seeded, **kwargs))


def _declaring(protocol: TrialProtocol, digest: str) -> TrialProtocol:
    return protocol.model_copy(
        update={"data": protocol.data.model_copy(update={"dataset_sha256": digest})}
    )


# --- 1. the command ------------------------------------------------------------------------


def test_the_command_prints_the_digest_a_real_run_seals_and_an_independent_one_agrees(
    seeded: Fixture, research_env: Path
) -> None:
    printed = _envelope(_digest(seeded))
    sealed = _run(seeded, marked=True)["bundle"]["provenance"]["dataset_sha256"]

    assert printed["dataset_sha256"] == sealed == _reference(list(seeded.bars))
    assert printed["bar_count"] == len(seeded.bars) == 288
    assert printed["first_event_time"] == seeded.first_bar.isoformat()
    assert printed["last_event_time"] == seeded.last_bar.isoformat()
    assert (printed["instrument_id"], printed["timeframe"]) == ("fx.eurusd", "M15")


def test_the_digest_follows_the_window_it_is_asked_about(
    seeded: Fixture, research_env: Path
) -> None:
    end = seeded.bars[40].event_time
    narrow = _envelope(_digest(seeded, end=end))
    sealed = _run(seeded, marked=True, end=end)["bundle"]["provenance"]["dataset_sha256"]

    assert narrow["dataset_sha256"] == sealed == _reference(list(seeded.bars[:41]))
    assert narrow["bar_count"] == 41
    assert narrow["dataset_sha256"] != _envelope(_digest(seeded))["dataset_sha256"]


def test_a_window_the_store_does_not_hold_or_that_holds_no_bar_is_refused(
    seeded: Fixture, research_env: Path
) -> None:
    after = seeded.last_bar.replace(year=seeded.last_bar.year + 1)

    _assert_refused(_digest(seeded, end=after), cli.ExitCode.COVERAGE)
    _assert_refused(_digest(seeded, instrument="fx.gbpusd"), cli.ExitCode.COVERAGE)
    # inside the store, but backwards: no bar is read
    _assert_refused(
        _digest(seeded, start=seeded.last_bar, end=seeded.first_bar),
        cli.ExitCode.STATISTICAL_INPUT,
    )


# --- 2. a protocol declaring the printed digest runs ---------------------------------------


def test_a_protocol_declaring_the_printed_digest_runs_and_every_bundle_seals_it(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    printed = _envelope(_digest(seeded))["dataset_sha256"]
    protocol = _declaring(_protocol(seeded), printed)
    path = _register_protocol(tmp_path, protocol)

    _scenarios(seeded, path)
    assert _compounding(seeded, path).exit_code == cli.ExitCode.OK

    sealed = sealed_bundles(
        _ledger(research_ledger_dsn).events_for(TRIAL_ID), _store(research_env).read
    )
    assert len(sealed) == 4  # the three levels of the grid and the compounding rerun
    assert {bundle.provenance.dataset_sha256 for _, bundle in sealed} == {printed}


# --- 3. any other declared hash is refused before any write --------------------------------


@pytest.mark.parametrize("wrong", [PLACEHOLDER, OTHER])
def test_scenarios_refuses_a_declared_hash_the_store_does_not_hash_to_before_any_write(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path, wrong: str
) -> None:
    path = _register_protocol(tmp_path, _declaring(_protocol(seeded), wrong))
    before = _state(research_ledger_dsn, research_env)

    _assert_refused(
        runner.invoke(cli.app, _scenario_args(seeded, path)), cli.ExitCode.SCENARIO_EVIDENCE
    )

    assert _state(research_ledger_dsn, research_env) == before


def test_compounding_refuses_a_placeholder_declared_hash_before_any_write(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    path = _register_protocol(tmp_path, _declaring(_protocol(seeded), PLACEHOLDER))
    # the 1.0x baseline a compounding rerun needs, sealed by hand: ``record`` does not hash bars
    _seal_grid(seeded, tmp_path, levels=("1",))
    before = _state(research_ledger_dsn, research_env)

    _assert_refused(_compounding(seeded, path), cli.ExitCode.SCENARIO_EVIDENCE)

    assert _state(research_ledger_dsn, research_env) == before


def test_open_holdout_refuses_a_holdout_hash_the_store_does_not_hash_to_before_any_write(
    opening_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    locked = _locked_protocol(opening_seeded)
    placeholder = locked.model_copy(
        update={"holdout": locked.holdout.model_copy(update={"dataset_sha256": PLACEHOLDER})}
    )
    assert placeholder.holdout.dataset_sha256 != HOLDOUT_HASH
    path = _register_protocol(tmp_path, placeholder)
    _scenarios(opening_seeded, path)
    assert _compounding(opening_seeded, path).exit_code == cli.ExitCode.OK
    _synthetic(research_ledger_dsn, research_env, Decision.RESEARCH_PASSED)
    before = _state(research_ledger_dsn, research_env)

    _assert_refused(_open(opening_seeded, path), cli.ExitCode.SCENARIO_EVIDENCE)

    assert _state(research_ledger_dsn, research_env) == before
    assert _holdout() == ("locked", 0)


# --- 4. the store changes between the pre-flight and the run -------------------------------


def test_a_store_that_changes_after_the_preflight_is_refused_and_nothing_is_sealed(
    seeded: Fixture,
    research_env: Path,
    research_ledger_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _register(tmp_path, seeded)
    altered = tuple(
        bar.model_copy(update={"spread": bar.spread + 1}) if index == 50 else bar
        for index, bar in enumerate(seeded.bars)
    )
    assert declared_digest(altered) != declared_digest(seeded.bars)
    handed: list[FakeBarReader] = []

    def store() -> FakeBarReader:
        # the pre-flight reads the bars the protocol declared; every read after it, the run's,
        # reads a store somebody has since corrected
        handed.append(FakeBarReader(seeded.bars if not handed else altered))
        return handed[-1]

    monkeypatch.setattr(cli, "_bar_store", store)
    rows, files = _state(research_ledger_dsn, research_env)

    _assert_refused(
        runner.invoke(cli.app, _scenario_args(seeded, path)), cli.ExitCode.SCENARIO_EVIDENCE
    )

    # the pre-flight passed (no refusal before an append), the first level appended its start row
    # and nothing was sealed: one row more, no file more
    assert _state(research_ledger_dsn, research_env) == (rows + 1, files)
    assert len(handed) == 2


def test_a_retry_with_every_level_sealed_reads_no_bars_and_writes_nothing(
    seeded: Fixture,
    research_env: Path,
    research_ledger_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _register(tmp_path, seeded)
    first = _scenarios(seeded, path)
    before = _state(research_ledger_dsn, research_env)

    def no_store() -> FakeBarReader:
        raise AssertionError("a retry that has nothing to run must not read the bar store")

    monkeypatch.setattr(cli, "_bar_store", no_store)
    again = _scenarios(seeded, path)

    assert [row["evidence_sha256"] for row in again["scenarios"]] == [
        row["evidence_sha256"] for row in first["scenarios"]
    ]
    assert _state(research_ledger_dsn, research_env) == before


# --- 5. decide, validate and the three states of a baseline's digest -----------------------


def _no_digest(payload: dict[str, Any]) -> None:
    payload["provenance"]["dataset_sha256"] = None


def _other_digest(payload: dict[str, Any]) -> None:
    payload["provenance"]["dataset_sha256"] = OTHER


def _baseline_only(long_seeded: Fixture, tmp_path: Path, change: Any = None) -> None:
    _register(tmp_path, long_seeded)
    _seal_grid(
        long_seeded,
        tmp_path,
        levels=("1",),
        tamper=None if change is None else ("1", change),
    )


def test_a_baseline_carrying_the_declared_digest_clears_the_dataset_reason(
    long_seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    _baseline_only(long_seeded, tmp_path)

    payload = _envelope(_decide())

    assert REASON not in payload["blocking_reasons"]
    assert payload["decision"] == "REJECTED"  # the holdout is not defined, capacity unavailable


def test_a_baseline_sealed_before_8e_keeps_the_dataset_reason(
    long_seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    _baseline_only(long_seeded, tmp_path, _no_digest)

    payload = _envelope(_decide())

    assert REASON in payload["blocking_reasons"]


def test_a_baseline_sealed_on_another_dataset_is_refused_by_decide_and_by_validate(
    long_seeded: Fixture,
    research_env: Path,
    research_ledger_dsn: str,
    tmp_path: Path,
) -> None:
    _baseline_only(long_seeded, tmp_path, _other_digest)
    before = _state(research_ledger_dsn, research_env)

    # decide: a promotion refusal (21), nothing written -- and not exit 19, which the statistical
    # read's faithfulness check would raise first if decide did not look at the digest before it
    _assert_refused(_decide(), cli.ExitCode.PROMOTION_REFUSED)
    # validate reads the same bundle through the faithfulness check: a scenario evidence refusal
    _assert_refused(
        runner.invoke(cli.app, ["research", "trial", "validate", "--trial-id", TRIAL_ID]),
        cli.ExitCode.SCENARIO_EVIDENCE,
    )

    assert _state(research_ledger_dsn, research_env) == before
    with pytest.raises(PromotionRefusedError) as refusal:
        decide_trial(
            TRIAL_ID,
            ledger=_ledger(research_ledger_dsn),
            store=_store(research_env),
            occurred_at=datetime(2026, 10, 1, tzinfo=UTC),
        )
    assert OTHER in str(refusal.value.__cause__)
    assert _protocol(long_seeded).data.dataset_sha256 in str(refusal.value.__cause__)


# --- 6. legacy stays unavailable -----------------------------------------------------------


def test_a_legacy_import_still_carries_no_dataset_hash_and_decide_still_names_the_reason(
    research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    trial_id = _legacy_trial(tmp_path)

    payload = _envelope(_decide(trial_id))

    assert REASON in payload["blocking_reasons"]
    legacy = [
        _store(research_env).read(record.event_json["payload"]["evidence_sha256"])
        for record in _ledger(research_ledger_dsn).events()
        if record.event_json["payload"].get("event_type") == "legacy_imported"
    ]
    assert legacy
    assert {bundle.provenance.dataset_sha256 for bundle in legacy} == {None}


# --- 7. the named help list, and the README ------------------------------------------------


def test_the_dataset_commands_help_offers_no_write_and_says_it_is_read_only() -> None:
    """The verdict ban and the completeness check live in the no-verdict gate
    (``TOTAL_BAN_RESEARCH_COMMANDS`` in the 8B2b acceptance), where the reason is stated."""

    result = runner.invoke(cli.app, ["research", "dataset", "digest", "--help"])

    assert result.exit_code == 0, result.stderr
    text = " ".join(result.stdout.split()).lower()
    assert "read only" in text
    assert re.search(r"--(force|overwrite|out|sign|approve)", text) is None


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(heading: str) -> str:
    start = README.index(heading)
    end = README.find("\n## ", start + 1)
    return README[start : end if end != -1 else len(README)]


def test_the_readme_has_the_8e_section_and_the_commands_row() -> None:
    rows = [
        line
        for line in README.splitlines()
        if line.startswith("| `") and "trading-house research dataset digest`" in line
    ]
    assert len(rows) == 1
    assert README.index("## Phase 8D2") < README.index("## Phase 8E")
    assert README.index("## Phase 8E") < README.index(
        "## Phase 8 - what exists, what blocks promotion, what a human must supply"
    )


def test_the_readme_states_the_digest_where_it_is_checked_and_what_it_does_not_prove() -> None:
    section = _flat(_section("## Phase 8E"))

    for sentence in (
        '`sha256(b"trading-house:dataset:v1" + canonical JSON array of the bars)`',
        "twelve fields of each bar",
        "One function, `replay_window_bars`, is the read",
        "defective bars included",
        "a bar at `end` is in and the bar after it is out",
        "`scenarios`, `compounding` and `open-holdout` hash the stored bars of the window",
        "before their first append",
        "After each run the digest the engine computed over the bars it replayed must equal the "
        "one the pre-flight took",
        "`provenance.dataset_sha256` is the digest the engine computed, never a value a caller "
        "supplies",
        "left out of every serialisation of `BacktestOutcome`",
        "A baseline carrying the declared digest clears the blocking reason",
        "one carrying none keeps it",
        "now true only for legacy imports and for runs sealed before 8E",
        "A protocol that declares a placeholder hash cannot run.",
        "It does not prove where the bars came from, or that they are real market data.",
        "A store that is later corrected, or re-ingested, hashes differently, by design.",
        "`research trial record` seals the bundle it is given.",
        "`scenario-report` and `splits` are descriptive reads with their own checks and do not "
        "examine the dataset digest",
    ):
        assert sentence in section, sentence


def test_the_readme_no_longer_says_the_dataset_hash_is_never_computed() -> None:
    text = _flat(README)

    for stale in (
        "The holdout dataset hash is DECLARED, not computed.",
        "Nothing in this repository can hash a bar store",
        "check is not implemented and is not done",
        "Every `backtest run` seals no dataset hash",
        "8B1 computes no digest of the bar store, and an unavailable hash is the honest record",
        "(never computed)",
        "is never computed from any data",
    ):
        assert stale not in text, stale
    for corrected in (
        "That is now true only of legacy imports and of runs sealed before 8E",
        "so a run sealed before Phase 8E is `null` too",
        "Since Phase 8E the dataset reason stands only when the baseline carries no dataset digest",
        "computed when it is opened (Phase 8E)",
    ):
        assert corrected in text, corrected
