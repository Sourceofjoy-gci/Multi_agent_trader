"""Phase 8C1 acceptance: the validated input, the walk-forward and CPCV splits, and the
``research trial splits`` read command, against real PostgreSQL where the chain matters.

8C1 adds no statistic, so what this file claims is about shape and honesty rather than
numbers:

1. **the command reads what the chain holds.** The folds and counts it prints are for
   the trial's own sealed 1.0x run, and every figure is checked against calendar
   arithmetic written out here, never against the code under test.
2. **a series too short for a walk-forward fold is undefined, not short.** The real
   run here spans 36 days and the policy needs 36 months.
3. **it states no verdict**, in its keys, its values and its help text.
4. **it refuses rather than defaults**: a run under 30 days, an unregistered trial and
   a registered one with nothing sealed each end in a typed refusal, and none writes.
5. **the README says what is true.**

Fixtures are the real ones, imported from the suites that own them; the only addition is
a 35-day bar store, because the shared one holds three days.
"""

# ruff: noqa: F811
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pydantic import SecretStr, ValidationError
from typer.testing import CliRunner

from tests.acceptance.test_phase8b2b import (
    _NO_VERDICT_HELP,
    _assert_refused,
    _envelope,
    _names_and_text,
)
from tests.conftest import DatabaseHarness
from tests.integration.marketdata.conftest import seed
from tests.integration.research.conftest import research_env  # noqa: F401
from tests.integration.research.test_backtest_evidence import ORIGIN, Fixture, _ramp
from tests.integration.research.test_compounding import _files, _rows
from tests.integration.research.test_scenarios import (
    TRIAL_ID,
    _protocol,
    _register,
    _scenarios,
)
from tests.integration.research.test_trial_cli import _ledger, _store
from tests.unit.ops.test_scenarios import _NO_VERDICT
from tests.unit.ops.test_scenarios import _protocol as _unit_protocol
from tests.unit.research.backtest.conftest import _contract
from tests.unit.research.validation.test_series import _trade, _with_trades
from trading_house import cli
from trading_house.core.errors import ScenarioEvidenceError, StatisticalInputError
from trading_house.database.connection import open_runtime_connection
from trading_house.marketdata.store import PostgresBarStore
from trading_house.ops.splits import WalkForwardReport, splits_report
from trading_house.research.canonical import canonical_sha256
from trading_house.research.evidence import DailyReturnPoint
from trading_house.research.trial_ledger import ReturnSeriesBasis

runner = CliRunner()

README = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")

BAR_DAYS = 35
"""Thirty-five days of M15 bars from 2026-09-21; the last bar is 2026-10-25 23:45."""

SERIES_DAYS = 36
"""The daily series is one day longer than the bars. The engine reads one bar past
``--end`` (inclusive open times against a half-open store range), so the final mark is
stamped 2026-10-26 and ``mark_to_market_bundle`` extends the series to it."""

WFA_UNDEFINED = (
    "the series spans 36 days and no fold of 24 training, 6 validation and 6 test months "
    "(36 in all) fits inside it"
)

# 36 days in six folds of six.
CPCV_FOLDS = [
    {"index": 0, "start": "2026-09-21", "end": "2026-09-26", "days": 6},
    {"index": 1, "start": "2026-09-27", "end": "2026-10-02", "days": 6},
    {"index": 2, "start": "2026-10-03", "end": "2026-10-08", "days": 6},
    {"index": 3, "start": "2026-10-09", "end": "2026-10-14", "days": 6},
    {"index": 4, "start": "2026-10-15", "end": "2026-10-20", "days": 6},
    {"index": 5, "start": "2026-10-21", "end": "2026-10-26", "days": 6},
]

# The split each fold of each path is taken from, worked out by hand from the lexicographic
# order of the fifteen pairs (see tests/unit/research/validation/test_splits.py).
PATH_SPLITS = [
    [0, 0, 1, 2, 3, 4],
    [1, 5, 5, 6, 7, 8],
    [2, 6, 9, 9, 10, 11],
    [3, 7, 10, 12, 12, 13],
    [4, 8, 11, 13, 14, 14],
]


@pytest.fixture(scope="module")
def long_seeded(
    database: DatabaseHarness, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Fixture]:
    """A real store holding 35 days of M15 bars, emptied afterwards."""

    bars = _ramp(BAR_DAYS)
    store = PostgresBarStore(lambda: open_runtime_connection(SecretStr(database.runtime_dsn)))
    seed(store, bars)
    contract = tmp_path_factory.mktemp("splits") / "contract.json"
    contract.write_text(_contract().model_dump_json(), encoding="utf-8")
    try:
        yield Fixture(
            contract=contract,
            first_bar=bars[0].event_time,
            last_bar=bars[-1].event_time,
            mid_session_bar=bars[33].event_time,
        )
    finally:
        with (
            psycopg.connect(database.test_superuser_dsn, autocommit=True) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute("TRUNCATE marketdata.bars, marketdata.ingest_runs")


def _splits(trial_id: str = TRIAL_ID) -> Any:
    return runner.invoke(cli.app, ["research", "trial", "splits", "--trial-id", trial_id])


def _flow(seeded: Fixture, tmp_path: Path) -> dict[str, Any]:
    """Register and run the grid; returns the ``scenarios`` payload."""

    return _scenarios(seeded, _register(tmp_path, seeded))


def _oracle_kept(
    trades: list[tuple[date, date]],
    folds: list[tuple[date, date]],
    test: tuple[int, ...],
    fold: int,
    purge: int,
) -> list[int]:
    """The test-side rule (own exit fold only) written out, for trades exiting in ``fold``.

    A straight transcription of the sentence in the design, with dates and loops, so the
    comparison is between two spellings and not two calls of one function.
    """

    kept = []
    for index, (entry, exit_) in enumerate(trades):
        if not folds[fold][0] <= exit_ <= folds[fold][1]:
            continue
        start = folds[fold][0]
        dropped = (
            purge >= 1 and fold >= 1 and fold - 1 not in test and entry <= start - timedelta(1)
        )
        if not dropped:
            kept.append(index)
    return kept


# --- 1. the command reads the chain's own sealed run ---------------------------


@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_splits_over_a_real_sealed_run_prints_the_hand_derived_folds_and_paths(
    long_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    grid = _flow(long_seeded, tmp_path)
    baseline = grid["report"]["scenarios"][0]
    assert baseline["multiplier"] == "1"
    rows, files = _rows(research_ledger_dsn), _files(research_env)

    result = _splits()

    assert result.exit_code == cli.ExitCode.OK, result.stderr
    payload = _envelope(result)
    assert payload["trial_id"] == TRIAL_ID
    assert payload["evidence_sha256"] == baseline["evidence_sha256"]
    assert payload["spec_sha256"] == canonical_sha256(_protocol(long_seeded).candidates[0])
    assert payload["series"] == {
        "first_day": "2026-09-21",
        "last_day": "2026-10-26",
        "days": SERIES_DAYS,
        "basis": "mark_to_market",
        "promotion_grade": True,
    }
    assert (payload["purge_days"], payload["embargo_days"]) == (1, 1)  # 16 hours, rounded up
    assert payload["wfa"] == {"folds": [], "undefined_reason": WFA_UNDEFINED}
    assert payload["cpcv"]["folds"] == CPCV_FOLDS
    assert len(payload["cpcv"]["splits"]) == 15
    assert payload["cpcv"]["splits"][0] == {"index": 0, "test_folds": [0, 1]}
    assert payload["cpcv"]["splits"][14] == {"index": 14, "test_folds": [4, 5]}
    paths = payload["cpcv"]["paths"]
    assert [[a["split_index"] for a in p["assignments"]] for p in paths] == PATH_SPLITS
    assert all([a["fold_index"] for a in p["assignments"]] == list(range(6)) for p in paths)
    # Read only: nothing was written, and a second read says the same thing.
    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows, files)
    assert _envelope(_splits()) == payload


@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_the_trade_counts_of_each_path_match_the_rule_written_out_per_trade(
    long_seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    _flow(long_seeded, tmp_path)
    payload = _envelope(_splits())
    bundle = _store(research_env).read(payload["evidence_sha256"])
    trades = [(t.entry_at.date(), t.exit_at.date()) for t in bundle.result.trades]
    folds = [
        (date.fromisoformat(f["start"]), date.fromisoformat(f["end"]))
        for f in payload["cpcv"]["folds"]
    ]
    pairs = [tuple(s["test_folds"]) for s in payload["cpcv"]["splits"]]
    assert trades, "a run that never traded proves nothing about its trade counts"
    assert payload["closed_trades"] == len(trades)

    for path in payload["cpcv"]["paths"]:
        expected = sum(
            len(_oracle_kept(trades, folds, pairs[a["split_index"]], a["fold_index"], purge=1))
            for a in path["assignments"]
        )
        assert path["trades_kept"] == expected
        assert path["trades_kept"] + path["trades_excluded"] == len(trades)


# --- 2. a 5-year series: walk-forward folds exist and are hand-derived ----------


def _five_year_points() -> tuple[DailyReturnPoint, ...]:
    first = date(2020, 1, 1)
    return tuple(
        DailyReturnPoint(day=first + timedelta(days=i), value=Decimal(0)) for i in range(1827)
    )


def _five_year_report() -> Any:
    protocol = _unit_protocol()
    bundle = _with_trades(
        _trade(*_instants((2020, 10, 31), (2020, 11, 2)), "1", "a"),  # f0 -> f1, exits in f1
        _trade(*_instants((2020, 12, 1), (2020, 12, 1)), "2", "b"),  # wholly inside f1
        _trade(*_instants((2020, 3, 1), (2020, 3, 1)), "4", "c"),  # wholly inside f0
    )
    bundle = bundle.model_copy(update={"daily_returns": _five_year_points()})
    return splits_report(trial_id="trial-1", protocol=protocol, sealed=("d" * 64, bundle))


def _instants(entry: tuple[int, int, int], exit_: tuple[int, int, int]) -> tuple[Any, Any]:
    return (
        datetime(*entry, 9, tzinfo=UTC),
        datetime(*exit_, 10, tzinfo=UTC),
    )


def test_a_five_year_series_reports_its_walk_forward_folds_and_no_reason() -> None:
    report = _five_year_report()

    assert report.series.days == 1827
    assert report.wfa.undefined_reason is None
    folds = report.wfa.folds
    # 24 + 6 + 6 months, stepping 6: five folds fit 1 Jan 2020 .. 31 Dec 2024. A 16 hour purge
    # is one day, so each training and validation window ends a day early.
    assert [f.test_end.isoformat() for f in folds] == [
        "2022-12-31", "2023-06-30", "2023-12-31", "2024-06-30", "2024-12-31",
    ]  # fmt: skip
    first, last = folds[0], folds[-1]
    assert (first.train_start, first.train_end) == (date(2020, 1, 1), date(2021, 12, 30))
    assert (first.validation_start, first.validation_end) == (date(2022, 1, 1), date(2022, 6, 29))
    assert (first.test_start, first.test_end) == (date(2022, 7, 1), date(2022, 12, 31))
    assert (last.train_end, last.validation_start) == (date(2023, 12, 30), date(2024, 1, 1))
    assert (last.validation_end, last.test_start) == (date(2024, 6, 29), date(2024, 7, 1))


def test_a_five_year_series_cuts_cpcv_folds_with_the_remainder_in_the_first_three() -> None:
    report = _five_year_report()

    # 1827 = 6 * 304 + 3.
    assert [f.days for f in report.cpcv.folds] == [305, 305, 305, 304, 304, 304]
    assert report.cpcv.folds[0].end == date(2020, 10, 31)  # day 305 of leap 2020
    assert report.cpcv.folds[1].start == date(2020, 11, 1)


def test_the_paths_of_a_five_year_series_keep_and_drop_the_hand_derived_trades() -> None:
    """Trades: a enters 31 Oct and exits 2 Nov (fold 1), b is inside fold 1, c inside fold 0.

    Fold 1 starts 1 Nov 2020. Only ``a`` can be dropped: it exits in fold 1 and entered on
    31 Oct, the day before. It is dropped exactly when fold 0 is not held out in the split
    that supplies fold 1; ``b`` entered inside fold 1 and ``c`` exits in fold 0, which has no
    predecessor, so neither is ever dropped. Fold 1's splits by path are (0,1), (1,2), (1,3),
    (1,4), (1,5): fold 0 is held out only on path 0. Hence path 0 keeps all three and
    paths 1-4 keep two and drop one.
    """

    report = _five_year_report()

    assert [(p.trades_kept, p.trades_excluded) for p in report.cpcv.paths] == [
        (3, 0), (2, 1), (2, 1), (2, 1), (2, 1),
    ]  # fmt: skip
    assert report.closed_trades == 3


# --- 3. no verdict ----------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_the_splits_output_carries_no_decision_vocabulary(
    long_seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    _flow(long_seeded, tmp_path)
    document = _envelope(_splits())

    names = _names_and_text(document)
    assert names, "a document with no keys or values would pass vacuously"
    keys = _keys(document)
    # ``promotion_grade`` is the spec's name (S-3) for a fact about the input's basis: true
    # only for a mark-to-market series. It states no decision about a candidate, and it is
    # the one key this sweep exempts, by name.
    assert "promotion_grade" in keys
    assert [
        key
        for key in keys - {"promotion_grade"}
        for word in (*_NO_VERDICT_HELP, "total")
        if word in key.lower()
    ] == []
    assert sorted(text for text in names for word in _NO_VERDICT if word in text.lower()) == []


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(k) for k in value} | set().union(*(_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(v) for v in value))
    return set()


def test_splits_takes_no_option_the_protocol_owns() -> None:
    result = runner.invoke(
        cli.app, ["research", "trial", "splits", "--trial-id", "t", "--protocol", "p.json"]
    )

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert "No such option" in result.stderr


# --- 4. refusals ------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_a_run_under_thirty_days_is_refused_and_nothing_is_written(
    long_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    ten_days = long_seeded._replace(last_bar=ORIGIN + timedelta(days=10) - timedelta(minutes=15))
    _flow(ten_days, tmp_path)
    rows, files = _rows(research_ledger_dsn), _files(research_env)

    result = _splits()

    _assert_refused(result, cli.ExitCode.STATISTICAL_INPUT)
    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows, files)
    with pytest.raises(StatisticalInputError) as refusal:
        cli._splits_report_for(TRIAL_ID, _ledger(research_ledger_dsn), _store(research_env))
    assert "holds 11 days; at least 30 are required" in str(refusal.value.__cause__)


@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_an_unregistered_trial_and_a_registered_one_with_nothing_sealed_are_refused(
    long_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    unregistered = _splits("nobody")
    _register(tmp_path, long_seeded)
    rows = _rows(research_ledger_dsn)
    unsealed = _splits(TRIAL_ID)

    _assert_refused(unregistered, cli.ExitCode.SCENARIO_EVIDENCE)
    _assert_refused(unsealed, cli.ExitCode.SCENARIO_EVIDENCE)
    assert _rows(research_ledger_dsn) == rows


# --- the report model --------------------------------------------------------------


def test_a_walk_forward_report_holds_folds_or_a_reason_and_exactly_one() -> None:
    folds = _five_year_report().wfa.folds

    assert WalkForwardReport(folds=folds, undefined_reason=None).folds == folds
    assert WalkForwardReport(folds=(), undefined_reason="too short").undefined_reason
    with pytest.raises(ValidationError):
        WalkForwardReport(folds=folds, undefined_reason="both")
    with pytest.raises(ValidationError):
        WalkForwardReport(folds=(), undefined_reason=None)


def test_purge_and_embargo_and_the_window_lengths_come_from_their_own_policy_fields() -> None:
    """16 hour purge (1 day), 49 hour embargo (3 days), windows 24/3/9 months, four CPCV folds."""

    protocol = _unit_protocol()
    policy = protocol.validation.model_copy(
        update={
            "purge_hours": 16,
            "embargo_hours": 49,
            "wfa_validation_months": 3,
            "wfa_test_months": 9,
            "cpcv_folds": 4,
        }
    )
    protocol = protocol.model_copy(update={"validation": policy})
    bundle = _with_trades().model_copy(update={"daily_returns": _five_year_points()})

    report = splits_report(trial_id="trial-1", protocol=protocol, sealed=("d" * 64, bundle))

    assert (report.purge_days, report.embargo_days) == (1, 3)
    first = report.wfa.folds[0]
    assert (first.train_end, first.validation_end) == (date(2021, 12, 30), date(2022, 3, 30))
    assert first.test_start == date(2022, 4, 1)
    assert len(report.cpcv.folds) == 4
    assert len(report.cpcv.splits) == 6
    assert len(report.cpcv.paths) == 3
    assert report.closed_trades == 0
    assert [(p.trades_kept, p.trades_excluded) for p in report.cpcv.paths] == [(0, 0)] * 3


def test_a_bundle_of_another_trial_is_refused_before_anything_is_cut() -> None:
    bundle = _with_trades().model_copy(update={"daily_returns": _five_year_points()})

    with pytest.raises(ScenarioEvidenceError):
        splits_report(trial_id="trial-2", protocol=_unit_protocol(), sealed=("d" * 64, bundle))


def test_more_cpcv_folds_than_days_is_refused_as_a_statistical_input() -> None:
    protocol = _unit_protocol()
    policy = protocol.validation.model_copy(update={"cpcv_folds": 40})
    bundle = _with_trades().model_copy(update={"daily_returns": _five_year_points()[:30]})

    with pytest.raises(StatisticalInputError) as error:
        splits_report(
            trial_id="trial-1",
            protocol=protocol.model_copy(update={"validation": policy}),
            sealed=("d" * 64, bundle),
        )

    assert "more folds than days" in str(error.value.__cause__)


def test_the_series_basis_is_reported_as_the_bundle_declares_it() -> None:
    bundle = _with_trades().model_copy(update={"daily_returns": _five_year_points()})
    report = splits_report(trial_id="trial-1", protocol=_unit_protocol(), sealed=("d" * 64, bundle))

    assert report.series.basis is ReturnSeriesBasis.REALIZED_CLOSED_TRADES
    assert report.series.promotion_grade is False


# --- 5. the README -----------------------------------------------------------------


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(heading: str) -> str:
    start = README.index(heading)
    end = README.find("\n## ", start + 1)
    return README[start : end if end != -1 else len(README)]


def test_the_readme_documents_the_command_in_the_table_and_the_section() -> None:
    section = _flat(_section("## Phase 8C1"))
    rows = [
        line
        for line in README.splitlines()
        if line.startswith("| `") and "trading-house research trial splits`" in line
    ]

    assert len(rows) == 1
    assert "research trial splits --trial-id T" in section
    assert README.index("## Phase 8B3") < README.index("## Phase 8C1")


def test_the_readme_states_what_8c1_is_and_is_not() -> None:
    section = _flat(_section("## Phase 8C1"))

    for sentence in (
        "It adds **no statistic, no verdict and no gate**",
        "NumPy is the one new dependency",
        "A series must hold at least `MIN_SERIES_DAYS` (30) days",
        "A fold that does not fit inside the series is **not created**",
        "Walk-forward with zero folds is reported as undefined, never as a short fold.",
        '`"wfa": {"folds": [], "undefined_reason": ...}`',
        "The live-data example will show zero walk-forward folds.",
        "A closed trade belongs to the fold containing its **exit** day.",
        "`entry_day <= e + embargo_days` and `exit_day >= s - purge_days`",
        "the test-side clause uses `purge_days` only as an on/off switch",
        "A path's return series is the whole series.",
        "16 hours is 1 day",
    ):
        assert sentence in section, sentence
    assert "| 20 | `sealed evidence is not a usable statistical input` |" in README
