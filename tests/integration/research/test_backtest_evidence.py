"""``backtest run --mark-to-market`` must produce evidence the ledger can seal.

The last mile of 8B1, and the one that can only be tested end to end: a replay
that emits a bundle, ``research trial record`` sealing that bundle unchanged, and
``verify`` reading the document back off disk. Everything between those three is
faked in the unit suites -- the bar reader, the store, the clock -- and a bundle
that only ever reaches a ``model_dump`` in a unit test proves nothing about the
store that has to accept the bytes.

The round trip here is a *real* one for the same reason ``test_trial_cli.py``'s
is: a registered protocol, a started attempt, the production content-addressed
store, and two databases. The only fake is the bar series, which is seeded
through the real store so the replay reads it the way it reads stored bars in
production.

``research_env`` is requested by every test here, including the ones that read
nothing from the evidence root: it is the fixture that points the command at the
two databases, and ``backtest run`` opens one of them on its own. It is also what
makes the identity-guard tests mean anything -- see
``test_a_bundle_missing_an_identity_option_is_refused_and_writes_nothing``.

The case that matters most is the Phase 7 one. Without the flag the payload is
Phase 7's artifact byte for byte, because ``research trial import-legacy``
depends on those three keys and on the ``margin_modelled`` beside them. That test
asserts the key set exactly *and* hands the emitted document to the importer that
reads it, so a renamed key or a digest taken over a different serialisation fails
as a refusal rather than as a shape nobody looked at again.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, NamedTuple

import psycopg
import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from tests.conftest import DatabaseHarness
from tests.integration.marketdata.conftest import seed

# The trial CLI's own helpers rather than a second copy of them: ``_protocol`` is
# the specification whose digest an operator passes to ``--mark-to-market``, and a
# copy of it would be a second declaration of what this file's bundle claims to
# belong to. The environment they all need -- the two DSNs and an emptied evidence
# root -- is this directory's ``research_env`` fixture, in ``conftest.py``.
from tests.integration.research.test_trial_cli import (
    _ledger,
    _protocol,
    _register,
    _start,
    _store,
)
from tests.unit.research.backtest.conftest import _contract
from trading_house import cli
from trading_house.core.errors import EvidenceIntegrityError
from trading_house.database.connection import open_runtime_connection
from trading_house.marketdata.models import Bar, BarQuality, Timeframe
from trading_house.marketdata.store import PostgresBarStore
from trading_house.research.backtest.result import BacktestResult
from trading_house.research.canonical import canonical_sha256
from trading_house.research.evidence import EvidenceBundle
from trading_house.research.trial_ledger import LedgerEventType

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("isolated_research_ledger"),
]

runner = CliRunner()

ORIGIN = datetime(2026, 9, 21, tzinfo=UTC)
BARS_PER_DAY = 96
DAYS = 3
POINT = Decimal("0.00001")
TRIAL_ID = "trial-1"
ATTEMPT_ID = "attempt-1"
# Declared, and identical on every run, because a declared clock is what makes
# the same bundle's bytes reproducible: both values ride in the bundle's
# provenance and therefore in its digest.
OCCURRED_AT = "2026-03-01T12:00:00"
REGISTERED_AT = "2026-03-01T13:00:00"


class Fixture(NamedTuple):
    contract: Path
    first_bar: datetime
    last_bar: datetime
    mid_session_bar: datetime


def _ramp(days: int) -> tuple[Bar, ...]:
    """``days`` of consecutive M15 bars, one continuous rising ramp.

    Whole days rather than the single London session the engine fixtures use,
    because the daily reduction is the claim under test here and a one-day window
    has exactly one day in it to be rectangular about. The price path runs
    through the overnight gaps rather than restarting each day, so this is one
    series and not three.
    """

    return tuple(
        Bar(
            instrument_id="fx.eurusd",
            timeframe=Timeframe.M15,
            event_time=ORIGIN + timedelta(minutes=15 * index),
            availability_time=ORIGIN + timedelta(minutes=15 * (index + 1)),
            open=Decimal("1.10000") + index * 2 * POINT,
            high=Decimal("1.10000") + index * 2 * POINT + POINT,
            low=Decimal("1.10000") + index * 2 * POINT - POINT,
            close=Decimal("1.10000") + index * 2 * POINT,
            tick_volume=100,
            spread=10,
            real_volume=0,
            quality=BarQuality.OK,
        )
        for index in range(days * BARS_PER_DAY)
    )


@pytest.fixture(scope="module")
def seeded(
    database: DatabaseHarness, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Fixture]:
    """A real store holding three days of M15 bars, emptied afterwards.

    Truncated in the ``finally`` for the reason
    ``test_backtest_determinism.py``'s is: the database is session-scoped and
    ``tests/integration/marketdata`` asserts on exactly what the store holds, so
    bars left behind here would fail tests with no connection to this one.
    """

    bars = _ramp(DAYS)
    store = PostgresBarStore(lambda: open_runtime_connection(SecretStr(database.runtime_dsn)))
    seed(store, bars)
    contract = tmp_path_factory.mktemp("backtest-evidence") / "contract.json"
    contract.write_text(_contract().model_dump_json(), encoding="utf-8")
    try:
        yield Fixture(
            contract=contract,
            first_bar=bars[0].event_time,
            last_bar=bars[-1].event_time,
            # The last bar of the first day's London morning. The strategy enters
            # at the open and its declared holding period runs to 16:00, so a
            # window stopping here ends with the position still open -- the case
            # the engine discards rather than closing at the range's edge, and the
            # case this file has to report rather than hide.
            mid_session_bar=bars[33].event_time,
        )
    finally:
        with (
            psycopg.connect(database.test_superuser_dsn, autocommit=True) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute("TRUNCATE marketdata.bars, marketdata.ingest_runs")


def _identity_options(trial_id: str = TRIAL_ID, attempt_id: str = ATTEMPT_ID) -> dict[str, str]:
    """The six options that travel with ``--mark-to-market``, and only with it.

    Declared once so the sweeps below name the same six the command takes; a
    second copy of the list is a second thing that can drift from it.
    """

    return {
        "--trial-id": trial_id,
        "--attempt-id": attempt_id,
        # The registered protocol's own digest, which is what an operator
        # copying the README types. Nothing in 8B1 compares it to a
        # preregistration -- ``trial record`` checks the bundle's trial and
        # attempt ids and no more -- so the bundle carries what the operator
        # declared, recorded rather than verified.
        "--spec-sha256": canonical_sha256(_protocol()),
        "--agent-run-id": "run-evidence",
        "--occurred-at": OCCURRED_AT,
        "--registered-at": REGISTERED_AT,
    }


def _args(
    seeded: Fixture,
    *,
    start: datetime,
    end: datetime,
    identity: bool = True,
    trial_id: str = TRIAL_ID,
    attempt_id: str = ATTEMPT_ID,
    **overrides: str,
) -> list[str]:
    options: dict[str, str] = {
        "--strategy": "session_momentum_eurusd",
        "--exit-policy": "none",
        "--start": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "--end": end.strftime("%Y-%m-%dT%H:%M:%S"),
        "--firm-equity": "100000",
        "--contract": str(seeded.contract),
        "--atr-period": "2",
        "--spread-window": "10",
        "--commission-per-lot-per-side": "3.50",
        "--slippage-points-per-side": "0",
        "--swap-long-points-per-day": "-0.80",
        "--swap-short-points-per-day": "0.30",
        "--triple-swap-weekday": "2",
        "--defective-bar-tolerance": "0",
    }
    if identity:
        options |= _identity_options(trial_id, attempt_id)
    options.update(overrides)
    return ["backtest", "run", *[value for pair in options.items() for value in pair]]


def _run(
    seeded: Fixture, *, marked: bool, end: datetime | None = None, **overrides: str
) -> dict[str, Any]:
    """One ``backtest run``, in the shape the operator asked for.

    The identity block travels with the flag and only with it, because the
    command refuses it in the other direction: a bare Phase 7 invocation has to
    be reachable without typing six options that would be refused.
    """

    argv = _args(
        seeded,
        start=seeded.first_bar,
        end=end if end is not None else seeded.last_bar,
        identity=marked,
        **overrides,
    )
    if marked:
        argv.insert(2, "--mark-to-market")
    result = runner.invoke(cli.app, argv)
    assert result.exit_code == cli.ExitCode.OK, result.stderr
    return json.loads(result.stdout)


def _bundle_of(payload: dict[str, Any]) -> EvidenceBundle:
    """The emitted bundle as a model, over the JSON path every reader takes.

    ``model_validate`` and not ``model_validate_json`` would be the interesting
    mistake here: every research contract is strict, so the decoded dict's string
    decimals and timestamps would be refused -- which is why the command reads a
    document through its JSON path too.
    """

    return EvidenceBundle.model_validate_json(json.dumps(payload["bundle"]))


def _write(path: Path, document: dict[str, Any]) -> Path:
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    return path


def _record(
    document: Path, *, trial_id: str = TRIAL_ID, attempt_id: str = ATTEMPT_ID
) -> dict[str, Any]:
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
            str(document),
        ],
    )
    assert result.exit_code == cli.ExitCode.OK, result.stderr
    return json.loads(result.stdout)


def test_mark_to_market_emits_a_marked_series_and_a_partial_cost_summary(
    seeded: Fixture, research_env: Path
) -> None:
    """What the flag buys, and the two things it refuses to invent.

    The series is ``MARK_TO_MARKET`` and rectangular: one point per calendar day
    across the whole window, no day omitted, which is what a daily reduction is
    for and what a month of missing days would silently break.

    ``costs`` is PARTIAL with both unknown components ``None``, and that is the
    point of the assertion rather than an incompleteness in it. Spread and
    slippage are charged inside the fill prices, so this run genuinely cannot
    separate them -- and writing a zero would turn an unmeasured term into a
    measured one. ``dataset_sha256`` is ``None`` for the same reason: 8B1 does
    not compute a digest of the bar store, and an unavailable hash is the honest
    record.
    """

    payload = _run(seeded, marked=True)

    assert sorted(payload) == ["bundle", "digest", "mark_to_market_flat", "status"]
    bundle = _bundle_of(payload)
    assert bundle.return_series_basis.value == "mark_to_market"
    # ``DAYS + 1``, not ``DAYS``: the final mark lands on the following UTC day
    # because the engine reads one bar past ``end``. Which day that is, and why
    # the series has to reach it, is the next test's whole subject.
    assert [point.day for point in bundle.daily_returns] == [
        seeded.first_bar.date() + timedelta(days=offset) for offset in range(DAYS + 1)
    ]
    # A day with no mark returns a literal zero, so a gap and a flat day are the
    # same number. What says the window was walked rather than sampled is that
    # the day carrying the 16:00 exit is not one: that equity is in the series.
    assert str(bundle.daily_returns[-2].value) != "0"
    assert bundle.costs.status.value == "partial"
    assert bundle.costs.spread_cost is None
    assert bundle.costs.slippage_cost is None
    # Commission and swap are measured, so they are not None. A PARTIAL summary
    # reporting zero for these would be indistinguishable from one that measured
    # nothing at all.
    assert bundle.costs.commission > 0
    assert bundle.provenance.dataset_sha256 is None
    assert payload["mark_to_market_flat"] is True


def test_the_bundle_seals_the_series_its_daily_returns_were_reduced_from(
    seeded: Fixture, research_env: Path
) -> None:
    """The assertion four review passes were missing: the series is *in* the bundle.

    ``daily_returns`` is derived from the per-bar series, so every test that only
    reads the daily series passes whether or not the series was ever sealed -- the
    reduction is present either way. That is precisely how the whole of 8B1's
    central artifact could be computed, fed into a function, and dropped without
    any of it going red: no field carried it out of the command, so it never
    reached the evidence store, and the design, the plan, the README and the
    acceptance file all described a sealed series no document contained.

    The count is against ``result.bars_seen`` rather than a literal, so a stored,
    subsampled or per-snapshot series fails it -- the same equality the engine's
    own ``BacktestOutcome`` validator makes, restated where the document is
    checked. The last two assertions are the ones tying the two series together:
    the daily walk's coverage is a fact about the sealed observations, and
    ``mark_to_market_flat`` is a fact about the same series, so a bundle whose
    reduction and whose input disagreed would be visible here.
    """

    payload = _run(seeded, marked=True)
    bundle = _bundle_of(payload)

    assert bundle.mark_to_market is not None, "the per-bar series must reach the evidence store"
    series = bundle.mark_to_market
    assert len(series.observations) == bundle.result.bars_seen
    assert series.firm_equity == bundle.result.firm_equity
    assert series.observations, (
        "an empty series is refused by the model, so this is belt and braces"
    )
    # The daily series is a function of this one, and the window reaches the day
    # the last mark falls on rather than the day ``result.end`` does.
    assert bundle.daily_returns[-1].day == series.observations[-1].marked_at.date()
    assert bundle.daily_returns[-1].day > bundle.result.end.date()
    # One report of flatness, two fields, and they must agree: the payload's
    # boolean is derived from the sealed series' own last observation.
    assert series.is_flat is payload["mark_to_market_flat"]
    assert series.observations[-1].open_positions == 0
    # And the identity at every point, in the document rather than in the engine.
    for point in series.observations:
        assert point.equity == (
            series.firm_equity + point.cumulative_realized_pnl + point.unrealized_pnl
        )


def _recomposed_close(bundle: EvidenceBundle) -> Fraction:
    """The equity the bundle's daily series compounds up to, in exact arithmetic.

    Recomputed from the reduced series rather than read off the sealed
    observations on purpose: that is the only way to ask whether the *reduction*
    still reconciles with the result beside it, which is a different question
    from whether the observations are present (the first test in this file) and
    the one the rounding in ``derive_daily_returns`` lives in. ``Fraction``
    because the *inputs* are the problem, not the walk: the returns are already
    rounded to Decimal's context by the time they are serialized, so ``Decimal``
    would compound that rounding into its own and the residual would depend on
    the context rather than on anything being asserted.
    """

    close = Fraction(bundle.result.firm_equity)
    for point in bundle.daily_returns:
        close *= 1 + Fraction(point.value)
    return close


def test_the_daily_series_reaches_the_day_the_final_bar_closes_on(
    seeded: Fixture, research_env: Path
) -> None:
    """The series must cover every observation, and reconcile with ``net_pnl``.

    ``start``/``end`` are inclusive bar *open* times and the store's range is
    half-open, so ``_replay_bars`` reads one bar past ``end``: this window ends
    at 23:45 and the last equity mark is stamped at the next UTC midnight. That
    bar is processed and its equity is inside ``net_pnl``, so a daily walk that
    stops at ``end.date()`` drops a mark the result already accounts for -- and
    for a window that ends on a bar realizing P&L it drops the whole of it.
    ``legacy_import.derive_realized_daily_returns`` extends its walk for the same
    reason and says so at length.

    The seal is what makes this observable at all, and it is worth naming: the
    bundle now carries the observations the walk was taken from, so a walk that
    silently stopped short is a disagreement between two fields of one document
    rather than a number nobody can re-derive. The coverage assertion is checked
    against the sealed series' own last mark, not against ``end.date()``, so it
    goes red exactly when the extension is dropped -- and it would not have been
    checkable at all before the series was sealed, which is why the two tests
    below are the pair.
    """

    bundle = _bundle_of(_run(seeded, marked=True))
    assert bundle.mark_to_market is not None

    # The off-by-one, stated rather than implied: the last mark is one bar
    # *past* ``result.end`` and on the following UTC day because of it.
    final_mark = bundle.result.end + timedelta(minutes=15)
    assert bundle.result.end == seeded.last_bar
    assert final_mark.date() > bundle.result.end.date()
    # ...and it is the sealed series' own last observation, so the daily walk is
    # asserted against the marks it reduced rather than against a second reading
    # of the result.
    assert bundle.mark_to_market.observations[-1].marked_at == final_mark

    assert bundle.daily_returns[-1].day == final_mark.date()
    # Flat, so the final mark is ``firm_equity + net_pnl`` with nothing
    # unrealized left in it, and the compounded series has to land there.
    assert bundle.result.trades
    expected = Fraction(bundle.result.firm_equity + bundle.result.net_pnl)
    # The sealed series says the same thing outright, which is what makes the
    # compounded walk a check on the *reduction* rather than a second reading of
    # one number the bundle already carries.
    assert Fraction(bundle.mark_to_market.observations[-1].equity) == expected
    # A cent rather than an equality, because the returns were rounded to
    # Decimal's context before they were serialized -- four orders of magnitude
    # below the smallest P&L this run books.
    assert abs(_recomposed_close(bundle) - expected) < Fraction(1, 100)


def test_the_payload_digest_is_the_bundle_address_not_the_result_digest(
    seeded: Fixture, research_env: Path
) -> None:
    """Two digests, deliberately unequal, and both of them real.

    ``digest`` is the content address ``research trial record`` will seal, so it
    is the canonical domain-separated digest of the whole bundle.
    ``source_result_sha256`` is the result's own declaration-ordered digest, which
    is what a Phase 7 artifact carried and what the legacy importer re-derives.
    They are different serialisations of the same result, so they differ; a reader
    who assumes otherwise will "fix" one into the other. Asserted here so the
    difference is a recorded property rather than a surprise.
    """

    payload = _run(seeded, marked=True)
    bundle = _bundle_of(payload)

    assert payload["digest"] == canonical_sha256(bundle)
    assert payload["digest"] != bundle.source_result_sha256
    assert bundle.provenance.source_artifact_sha256 == canonical_sha256(bundle.result)
    assert bundle.source_result_sha256 == bundle.result.digest()


def test_the_documented_operator_flow_seals_and_verifies(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    """The four commands the design writes down, run in that order, unmodified.

    ``backtest run --mark-to-market ... > run.json`` then
    ``research trial record --evidence run.json`` is the flow the README and
    design 8 document, and the redirect is the point: what lands in ``run.json``
    is the *whole* command payload -- ``{"status", "bundle", "digest",
    "mark_to_market_flat"}`` -- because that is what the CLI prints. The round
    trip below used to hand ``record`` ``payload["bundle"]`` instead, a document
    no operator ever produces, and every test in this file passed while the
    documented flow exited 2 with ``configuration invalid``. So the file written
    here is the file the command wrote, and this is the test that would have
    caught it.

    Nothing is rewritten in between, and the equality below is between the
    address the run reported and the address a reader gets back off disk rather
    than a digest the test recomputed. ``mark_to_market`` is read back out of the
    *stored* document as well, because a flow that ends at a green ``verify`` is
    not the claim: a bundle that verifies while missing the series is exactly the
    defect this suite is here to close.
    """

    _register(tmp_path)
    started = _start()
    assert started.exit_code == cli.ExitCode.OK, started.stderr

    payload = _run(seeded, marked=True)
    document = _write(tmp_path / "run.json", payload)
    assert json.loads(document.read_text(encoding="utf-8")) == payload

    recorded = _record(document)

    assert recorded["evidence_sha256"] == payload["digest"]
    assert [record.event_type for record in _ledger(research_ledger_dsn).events()] == [
        LedgerEventType.PREREGISTERED,
        LedgerEventType.EXECUTION_STARTED,
        LedgerEventType.RESULT_RECORDED,
        LedgerEventType.EVIDENCE_SEALED,
    ]
    sealed = _store(research_env).read(recorded["evidence_sha256"])
    assert sealed == _bundle_of(payload)
    assert sealed.mark_to_market is not None
    assert len(sealed.mark_to_market.observations) == sealed.result.bars_seen
    verified = runner.invoke(cli.app, ["research", "trial", "verify"])
    assert verified.exit_code == cli.ExitCode.OK, verified.stderr
    assert json.loads(verified.stdout) == {
        "status": "ok",
        "valid": True,
        "checked_events": 4,
        "reason": None,
    }


def test_a_bare_bundle_is_still_what_record_accepts(
    seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    """The second accepted shape, kept because ``record`` still takes it.

    ``--evidence`` reads a bare ``EvidenceBundle`` or the command payload that
    wraps one under ``"bundle"``, and nothing else. The flow test above covers
    the second; this covers the first, because it is the shape every other
    ``record`` caller in this file and in ``test_trial_cli.py`` uses, and a fix
    that taught the command to unwrap the wrapper would be free to stop accepting
    the document it was always given.
    """

    _register(tmp_path)
    started = _start()
    assert started.exit_code == cli.ExitCode.OK, started.stderr

    payload = _run(seeded, marked=True)
    recorded = _record(_write(tmp_path / "bundle.json", payload["bundle"]))

    assert recorded["evidence_sha256"] == payload["digest"]
    assert _store(research_env).read(recorded["evidence_sha256"]).mark_to_market is not None
    verified = runner.invoke(cli.app, ["research", "trial", "verify"])
    assert verified.exit_code == cli.ExitCode.OK, verified.stderr
    assert json.loads(verified.stdout)["valid"] is True


def test_a_document_that_is_neither_shape_is_refused_and_writes_nothing(
    seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    """The fail-closed half of the two shapes, on the likeliest wrong document.

    A real ``backtest run`` payload -- the same command without
    ``--mark-to-market``, so a real document an operator produced and a real
    mistake to make, with a real result inside it. It is refused at exit 2 and
    nothing is written, which is the same answer a mistyped path gets: an
    operator who named a document this command does not accept has made a
    mistake, not produced evidence that cannot be trusted.

    The point of the case is that the unwrap is narrow. A ``record`` that reached
    for a nested bundle, or that accepted anything with a ``result`` in it, would
    pass the documented flow and this document both; only the two named shapes
    do neither.
    """

    _register(tmp_path)
    started = _start()
    assert started.exit_code == cli.ExitCode.OK, started.stderr

    phase7 = _write(tmp_path / "phase7.json", _run(seeded, marked=False))
    result = runner.invoke(
        cli.app,
        [
            "research",
            "trial",
            "record",
            "--trial-id",
            TRIAL_ID,
            "--attempt-id",
            ATTEMPT_ID,
            "--evidence",
            str(phase7),
        ],
    )

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert not list(research_env.rglob("*.json"))
    verified = runner.invoke(cli.app, ["research", "trial", "verify"])
    assert verified.exit_code == cli.ExitCode.OK, verified.stderr
    assert json.loads(verified.stdout) == {
        "status": "ok",
        "valid": True,
        "checked_events": 2,
        "reason": None,
    }


def test_without_the_flag_the_payload_is_the_phase7_artifact_unchanged(
    seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    """The contract ``research/legacy_import.py`` reads, proven by letting it read it.

    Phase 7's artifact is ``{"status": "ok", "result": ..., "digest": ...,
    "margin_modelled": false}``, and the importer parses the three it needs and
    re-derives the digest from the result it carries. The key set is therefore
    asserted *exactly* here and on its own: the importer reads the keys it wants
    and no more, so a fourth key would import cleanly and "the payload is
    unchanged" is not something the round trip can establish on its own. What the
    round trip does establish -- and what the key set cannot -- is that a renamed
    key or a digest taken over a different serialisation comes back as a
    refusal.

    The digest is then compared across the two invocations, so the flag is proved
    to change the payload's *address* and not the result inside it: the same run
    reports one result digest in both shapes.
    """

    plain = _run(seeded, marked=False)
    marked = _run(seeded, marked=True)

    # The exact key set, stated as an equality on both the set and the order:
    # this is the only assertion here that a *fourth* key cannot slip past.
    assert sorted(plain) == ["digest", "margin_modelled", "result", "status"]
    assert list(plain) == sorted(plain)
    assert plain["status"] == "ok"
    assert plain["margin_modelled"] is False
    emitted = BacktestResult.model_validate_json(json.dumps(plain["result"]))
    assert plain["digest"] == emitted.digest()
    assert emitted.trades
    # The same run in two shapes: one result, one result digest, two documents.
    assert plain["digest"] == marked["bundle"]["source_result_sha256"]
    assert plain["result"] == marked["bundle"]["result"]

    artifact = _write(tmp_path / "phase7.json", plain)
    imported = runner.invoke(
        cli.app, ["research", "trial", "import-legacy", "--artifact", str(artifact)]
    )

    assert imported.exit_code == cli.ExitCode.OK, imported.stderr
    payload = json.loads(imported.stdout)
    assert payload["source_result_sha256"] == emitted.digest()
    assert payload["already_present"] is False


@pytest.mark.parametrize("omitted", sorted(_identity_options()))
def test_a_bundle_missing_an_identity_option_is_refused_and_writes_nothing(
    seeded: Fixture, research_env: Path, tmp_path: Path, omitted: str
) -> None:
    """A missing flag is a mistake, not untrustworthy evidence, and says so.

    ``ExitCode.CONFIGURATION`` (2) and not the equity code: an operator who left
    a flag off can fix it by typing it, while exit 18 reads as "this run's equity
    cannot be valued" and sends them looking in the wrong place.

    It runs under ``research_env`` because that is what makes the exit code mean
    anything. These tests set no DSN, so a *complete, valid* run also exits 2
    with empty stdout -- the unit sweep that used to sit here passed with the
    guard deleted for exactly that reason. Here a bare run of the same window
    succeeds (``test_without_the_flag_the_payload_is_the_phase7_artifact_unchanged``),
    so exit 2 is about the identity and nothing else.

    Four of the six still pass with the guard deleted, and that is worth saying
    rather than leaning on: a ``None`` reaches ``EvidenceBundle``, whose
    ``ValidationError`` ``_execute`` maps to exit 2 as well. The second line of
    defence is real, and it is why the *with-flag* direction was never the one
    that needed a test -- the two timestamp options are the pair the guard alone
    refuses, and they are the pair that goes red.

    ``research_env`` starts empty, and this test's own ``_register`` and
    ``_start`` are the only two events in the ledger, so "wrote nothing" is a
    statement about those two rather than a claim.
    """

    _register(tmp_path)
    started = _start()
    assert started.exit_code == cli.ExitCode.OK, started.stderr

    options = _args(seeded, start=seeded.first_bar, end=seeded.last_bar)
    index = options.index(omitted)
    del options[index : index + 2]
    options.insert(2, "--mark-to-market")

    result = runner.invoke(cli.app, options)

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert result.stdout == ""
    assert not list(research_env.rglob("*.json"))


@pytest.mark.parametrize("supplied", sorted(_identity_options()))
def test_one_identity_option_without_the_flag_is_refused_and_writes_nothing(
    seeded: Fixture, research_env: Path, supplied: str
) -> None:
    """The direction a one-sided guard misses, one option at a time.

    ``mark_to_market != all(...)`` catches a *complete* set without the flag and
    lets a partial one through: ``--trial-id`` typed and ``--mark-to-market``
    forgotten produces the Phase 7 artifact at exit 0, carrying no identity at
    all, and the operator is told nothing. Unlike the direction above there is no
    second line of defence here -- nothing downstream ever sees the option, so
    every one of these six is red with the guard deleted, and the full set below
    is red with it too. That is the whole reason this sweep lives.
    """

    options = _args(seeded, start=seeded.first_bar, end=seeded.last_bar, identity=False)
    options += [supplied, _identity_options()[supplied]]

    result = runner.invoke(cli.app, options)

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert result.stdout == ""
    assert not list(research_env.rglob("*.json"))


def test_every_identity_option_without_the_flag_is_refused_and_writes_nothing(
    seeded: Fixture, research_env: Path
) -> None:
    """All six without the flag: refused, and the payload is not produced.

    The whole set is the case the original guard was written for, and it is the
    one an operator most plausibly types by pasting a documented invocation.
    Without the guard it exits 0 and hands back the Phase 7 artifact, so the
    operator believes a bundle has been recorded when nothing was named.
    """

    options = _args(seeded, start=seeded.first_bar, end=seeded.last_bar, identity=False)
    for name, value in _identity_options().items():
        options += [name, value]

    result = runner.invoke(cli.app, options)

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert result.stdout == ""
    assert not list(research_env.rglob("*.json"))


def test_a_run_ending_with_an_open_position_is_reported_and_still_seals(
    seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    """A non-flat run exits 0, says it is non-flat, and is still evidence.

    The engine discards a position the bars ran out on rather than closing it at
    the range's edge, so this run has no trades and a final observation with one
    open position carrying unrealized PnL. A later gate refuses that, and not
    this command: the run happened and its marks are real, and a command that
    failed on it would hide the very thing the operator needs to see.
    ``mark_to_market_flat`` is the report.

    The zero-trade result is also what makes the cost assertions here rather than
    a copy of the first test's: with no trades measured, ``commission == 0`` and
    ``spread_cost is None`` sit side by side -- the two states a gate reading
    ``COMPLETE`` could not tell apart.
    """

    _register(tmp_path)
    started = _start(trial_id="trial-2", attempt_id="attempt-2")
    assert started.exit_code == cli.ExitCode.OK, started.stderr

    payload = _run(
        seeded,
        marked=True,
        end=seeded.mid_session_bar,
        trial_id="trial-2",
        attempt_id="attempt-2",
    )

    assert payload["mark_to_market_flat"] is False
    bundle = _bundle_of(payload)
    assert bundle.result.trades == ()
    assert bundle.costs.commission == 0
    assert bundle.costs.spread_cost is None
    # A day the series must still cover: the open position is a mark, not a gap.
    assert [point.day for point in bundle.daily_returns] == [seeded.first_bar.date()]

    recorded = _record(
        _write(tmp_path / "open.json", payload["bundle"]),
        trial_id="trial-2",
        attempt_id="attempt-2",
    )

    assert recorded["evidence_sha256"] == payload["digest"]
    assert _store(research_env).read(recorded["evidence_sha256"]).daily_returns
    verified = runner.invoke(cli.app, ["research", "trial", "verify"])
    assert verified.exit_code == cli.ExitCode.OK, verified.stderr
    assert json.loads(verified.stdout)["valid"] is True


def test_a_doctored_daily_return_is_refused_by_the_store(
    seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    """One number edited in a sealed document, and the typed refusal that follows.

    Nothing inside the bundle *derives* ``daily_returns[0]`` from the sealed
    observations -- the reduction happened in the engine and the store does not
    recompute it -- so a reader who trusts the reduction has to take it on the
    content address. That is what this case says, and it is a different claim
    from the series being present: sealing the observations makes a wrong
    reduction *checkable* by a reader or a later phase, and this test is about the
    layer below that, which is the address.

    The edited document is still well-formed JSON and still parses into a
    plausible bundle, so the refusal cannot be a parse accident: it is the
    digest, re-derived from the bytes, disagreeing with the address the ledger
    recorded.
    """

    _register(tmp_path)
    started = _start()
    assert started.exit_code == cli.ExitCode.OK, started.stderr
    payload = _run(seeded, marked=True)
    recorded = _record(_write(tmp_path / "bundle.json", payload["bundle"]))

    stored = next(research_env.rglob(f"{recorded['evidence_sha256']}.json"))
    edited = json.loads(stored.read_text(encoding="utf-8"))
    assert edited["daily_returns"][0]["value"] != "0.9"
    edited["daily_returns"][0]["value"] = "0.9"
    stored.write_text(json.dumps(edited, sort_keys=True), encoding="utf-8")
    # Still a bundle, as far as the model is concerned: the refusal has to come
    # from the address, not from a document that would not parse.
    assert EvidenceBundle.model_validate_json(json.dumps(edited)).daily_returns

    with pytest.raises(EvidenceIntegrityError):
        _store(research_env).read(recorded["evidence_sha256"])

    verified = runner.invoke(cli.app, ["research", "trial", "verify"])
    assert verified.exit_code == cli.ExitCode.EVIDENCE_INTEGRITY
