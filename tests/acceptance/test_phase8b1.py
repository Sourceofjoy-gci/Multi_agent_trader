"""Phase 8B1 acceptance: the mark-to-market series, and the four constants that
prove putting it on a sidecar was the right call.

8B1 put the equity series on a **new** type, ``BacktestOutcome``, beside
``BacktestResult`` rather than on ``BacktestResult`` itself. That decision is
load-bearing, and what this file mostly exists to check is that it held: four
pinned digests name artifacts that exist on no machine but the one that produced
them, so a field added to ``BacktestResult`` moves all four, and each is a
*record of a completed experiment* rather than a value anybody can recompute
today. A phase that mutates a result whose digests are published elsewhere has
to be told.

Only one of the four actually reads a ``BacktestResult`` in the making: the
known-answer **bundle** digest, whose bundle embeds a live result, so it moves
under any field added to the model. The other three pin results this repository
can no longer rebuild, so they stand as records and would be restated by hand if
a field were ever added. They are kept because the record is the point, not
because each one detects a change on its own.

That fourth constant did **not** move when the bundle gained
``mark_to_market``, and that is worth a sentence rather than a shrug: a plain
defaulted field writes ``"mark_to_market":null`` into every bundle's canonical
bytes, which moves the digest *and* makes ``EvidenceStore.read`` refuse every
v1 document already sealed in an operator's store, because it re-serializes what
it decoded. The field is excluded from the bytes when it is absent, so a bundle
carrying no series encodes exactly as before, and the test named for that cuts
the key out of real stored bytes to prove the store still reads the remainder at
its own address.

What is asserted, in the order a reader should meet it:

1. the three Phase 7 result digests are still exactly those literals, and still
   agree with the only independent record of them in this repository;
2. the known-answer v1 bundle digest is still exactly its literal, recomputed
   from the live model -- the one of the four that *can* be re-derived, and so
   the one that goes red when the encoding moves;
3. the three v1 legacy bundles still seal and verify through the production
   store, and a document sealed before the field existed still verifies at its
   own address, so the sidecar did not strand evidence sealed before it existed;
4. a real run marks every processed bar, the mark identity holds at every point
   of it, the count is the result's own ``bars_seen``, and the **series itself**
   is sealed in the bundle -- the daily reduction is a function of it, and a
   reduction whose input the store does not hold cannot be re-derived;
5. the basis and the series cannot disagree: a ``MARK_TO_MARKET`` bundle with no
   series is refused, and so is any other basis carrying one; and the sealed
   series must be the carried result's own — a firm equity, an observation
   count, or (on a flat run) a final realized total that disagrees with
   ``result`` is refused in ``BacktestOutcome``'s words, so the same disagreement
   is reported the same way whether it was caught at construction or at read
   time;
6. a flat run reconciles its final realized total to ``net_pnl``, and a run that
   ends holding a position is *reported* by ``backtest run`` rather than refused;
7. ``MARK_TO_MARKET`` -- the enum member 8A declared with no producer -- is now
   produced by that command;
8. without ``--mark-to-market`` the payload is the Phase 7 artifact, with no
   ``bundle`` key, over the same result.

The bar store is the only fake in the CLI cases, and it is the same
``FakeBarReader`` the unit CLI suite drives ``backtest run`` with: a DSN is
declared and never connected to, so this file runs where Docker does not.
Everything else is production -- the signed constitution, the risk engine, the
engine loop, the bundle builder, the content-addressed store, the legacy
importer. The container-backed round trip through ``research trial record`` is
``tests/integration/research/test_backtest_evidence.py``, which is where the
ledger and the store meet.

What is deliberately **not** asserted: that the bundle's ``spec_sha256`` is
cross-checked against the sealed ``PREREGISTERED`` event. Nothing cross-checks
it, by the design of this slice (design 9.1), and a test naming a refusal the
code does not raise is a promise rather than a check. So is anything about
promotion. 8B1 gates nothing, and a bundle that verifies is not a candidate
that passes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError
from typer.main import get_command
from typer.testing import CliRunner

from tests.property.test_trial_evidence import _CANONICAL_BUNDLE_SHA256, _bundle
from tests.unit.research.backtest.conftest import (
    FakeBarReader,
    _contract,
    _cost_model,
    _session_ramp,
)
from trading_house import cli
from trading_house.constitution.loader import LoadedConstitution, load_constitution
from trading_house.core.exits import ChandelierPolicy, ExitPolicy, FixedTargetPolicy, NoExitPolicy
from trading_house.core.schemas import Side
from trading_house.marketdata.models import Bar, Timeframe
from trading_house.ops.backtest import build_backtester, build_strategy
from trading_house.research.backtest.costs import CostModel
from trading_house.research.backtest.engine import BacktestRequest
from trading_house.research.backtest.fills import ExitKind
from trading_house.research.backtest.mark import BacktestOutcome
from trading_house.research.backtest.result import BacktestResult, SimulatedTrade
from trading_house.research.canonical import canonical_sha256
from trading_house.research.evidence import EvidenceBundle, EvidenceStore
from trading_house.research.legacy_import import import_phase7_artifact
from trading_house.research.trial_ledger import (
    CostAttributionStatus,
    HoldoutState,
    LedgerEvent,
    RegistrationState,
    ReturnSeriesBasis,
    TrialLedger,
)
from trading_house.strategies.impl.session_momentum import (
    SESSION_MOMENTUM_ID,
    SESSION_MOMENTUM_SPEC,
)

runner = CliRunner()

# --- the four pinned digests, as literals -------------------------------------
#
# Two kinds of constant, and the difference is the point.

PHASE7_RESULT_DIGESTS = {
    # Constants 1-3: the three completed Phase 7 arms, verbatim from
    # ``tests/acceptance/test_phase8a.py:111-115``, which took them from the
    # arms' own ``SESSION_MOMENTUM_SPEC.versioning`` string. NOT re-derived here
    # and not re-derivable by anyone: they name three runs against a bar store
    # that was deleted afterwards (umbrella 2.2), on the one machine that ran
    # them. A test can therefore only refuse to agree with them silently, which
    # is the whole of what it does -- and the reason a literal is the honest
    # encoding here rather than a weak one.
    "none": "a10b0fa43fc4ddb0185b3a376caab7c9369ec49e3e347d13171d31ab989f022b",
    "fixed_target": "fe5efadef7f5ea2448957e0bec507f2133755df676ade140b05a120cfb5fbc6b",
    "chandelier": "69867de09ef3993e5aabca78b225b21bb1ef82a4aa4fbcd63a9fd52dfe2e0c54",
}

V1_BUNDLE_SHA256 = "b07618429d5992d0fb038b6ad53b8401fa1aa9ad467efd06c20421a3557d8558"
# Constant 4: the known answer at ``tests/property/test_trial_evidence.py``,
# repeated as a literal so this guard does not depend on that file's copy
# staying put -- if either is edited, or the encoding moves, one of the two
# assertions below fails. Unlike the three above, this one IS recomputed, and
# that is the whole of what a known answer is for: ``canonical.py`` promises the
# encoding is stable across processes and Python versions, so a digest taken
# today must still verify when the evidence is re-read in a year, and a constant
# compared only against itself cannot show that.
#
# This value did NOT move when the bundle gained ``mark_to_market``, and the
# reason is the fourth design's compatibility claim rests on rather than a piece
# of luck: the field is excluded from the serialized bytes when it is absent, so
# a bundle that carries no series encodes exactly as it did before.
# ``test_a_document_sealed_before_the_series_existed_still_verifies`` is what
# proves that, by cutting the key out of real stored bytes and reading the result
# back at its own address; a plain ``= None`` would have moved this literal *and*
# made every v1 document in an operator's store unreadable.

# The three arms' own policies, so the v1 legacy bundles below differ the way the
# artifacts they stand for did. Three bundles differing only in their digest
# would be one bundle three times.
LEGACY_ARMS: dict[str, ExitPolicy] = {
    "none": NoExitPolicy(kind="none"),
    "fixed_target": FixedTargetPolicy(kind="fixed_target", r_multiple=Decimal("1.0")),
    "chandelier": ChandelierPolicy(
        kind="chandelier", atr_multiple=Decimal("3.0"), min_step_points=Decimal(10)
    ),
}

_IDENTITY: dict[str, str] = {
    "--trial-id": "trial-1",
    "--attempt-id": "attempt-1",
    # A declared digest, unvouched: 8B1 records what the operator claims and does
    # not compare it to a preregistration (design 9.1), which is why this is a
    # well-formed literal rather than one taken from a sealed protocol.
    "--spec-sha256": "1a2b3c4d5e6f70819293a4b5c6d7e8f90112233445566778899aabbccddeeff0",
    "--agent-run-id": "run-acceptance",
    "--occurred-at": "2026-03-01T12:30:00",
    "--registered-at": "2026-03-01T13:00:00",
}
# The one option that is not an identity, kept beside the six so the all-or-none
# set is read as what it is rather than as seven unrelated flags.
_FLAG = "--mark-to-market"

_HEX = frozenset("0123456789abcdef")
_FAKE_DSN = "postgresql://runtime:not-a-real-password@localhost/trading_house"
"""Never connected to. ``RuntimeSettings`` requires a DSN and, with the bar reader
supplied below, the command opens no connection at all -- so this is the
variable's presence and nothing else. Spelled out rather than borrowed from the
developer's own ``.env``, which is what would make this gate depend on a host."""

_IMPORT_CLOCK = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
_MID_MORNING_BAR = 33
"""A bar partway through the first London morning on a 65-bar M15 session. The
strategy enters inside that morning and its declared holding period runs to
16:00, so a
window ending here ends with the position still open: the case the engine
discards rather than closing at the range's edge, and the case a later gate
refuses."""


# --- a real run, assembled by the production factory ---------------------------


def _constitution() -> LoadedConstitution:
    """The checked-in constitution, loaded and signature-verified.

    Through the production loader rather than trusted, because the risk engine
    sized every position in the runs below and a stubbed constitution would make
    this a test of a run that never happened.
    """

    return load_constitution(
        Path("config/risk_constitution.yaml"),
        Path("config/risk_constitution.yaml.sig"),
        Path("config/risk_constitution.public.pem"),
    )


def _outcome(bars: tuple[Bar, ...], *, last_bar: int | None = None) -> BacktestOutcome:
    """One real replay: ``build_backtester`` is the factory the CLI itself calls.

    So the shared ``ReplayClock``, the real ``RiskEngine`` on the real signed
    constitution, and the margin port that never binds are all production
    objects. The bar reader is the single substitution, because a real one is
    PostgreSQL and this gate runs where Docker does not.

    One arm, ``none``. The other two are Phase 7's, and a mark series is not
    arm-specific: what is under test is where the marks are taken, which is the
    engine's loop and not the exit policy under it.
    """

    end = bars[-1].event_time if last_bar is None else bars[last_bar].event_time
    return build_backtester(
        bars=FakeBarReader(bars),
        contract=_contract(),
        constitution=_constitution(),
    ).run(
        BacktestRequest(
            strategy=build_strategy(SESSION_MOMENTUM_ID, exit_policy=NoExitPolicy(kind="none")),
            instrument_id="fx.eurusd",
            timeframe=Timeframe.M15,
            start=bars[0].event_time,
            end=end,
            firm_equity=Decimal("100000"),
            cost_model=_cost_model(),
            atr_period=2,
            spread_window=10,
        )
    )


def _run_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, marked: bool, last_bar: int | None = None
) -> dict[str, Any]:
    """``backtest run`` in the shape an operator types it, over in-memory bars.

    Every cost stated and ``--contract`` a real file, because that is the
    documented shape and a boundary that only works with a default is a boundary
    nobody has used.
    """

    bars = _session_ramp()
    contract = tmp_path / "contract.json"
    contract.write_text(_contract().model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("TRADING_HOUSE_DATABASE_DSN", _FAKE_DSN)
    monkeypatch.setattr(cli, "_bar_store", lambda: FakeBarReader(bars))
    end = bars[-1].event_time if last_bar is None else bars[last_bar].event_time
    options: dict[str, str] = {
        "--strategy": SESSION_MOMENTUM_ID,
        "--exit-policy": "none",
        "--start": bars[0].event_time.strftime("%Y-%m-%dT%H:%M:%S"),
        "--end": end.strftime("%Y-%m-%dT%H:%M:%S"),
        "--firm-equity": "100000",
        "--contract": str(contract),
        "--atr-period": "2",
        "--spread-window": "10",
        "--commission-per-lot-per-side": "3.50",
        "--slippage-points-per-side": "0",
        "--swap-long-points-per-day": "-0.80",
        "--swap-short-points-per-day": "0.30",
        "--triple-swap-weekday": "2",
        "--defective-bar-tolerance": "0",
    }
    if marked:
        options |= _IDENTITY
    argv = ["backtest", "run", *[value for pair in options.items() for value in pair]]
    if marked:
        argv.insert(2, _FLAG)
    result = runner.invoke(cli.app, argv)
    assert result.exit_code == cli.ExitCode.OK, result.stderr
    payload: dict[str, Any] = json.loads(result.stdout)
    return payload


def _bundle_of(payload: dict[str, Any]) -> EvidenceBundle:
    """The emitted bundle as a model, over the JSON path every reader takes.

    Every research contract is strict, so the document's string decimals and
    timestamps are refused on ``model_validate`` -- which is why the command, and
    every reader of its output, goes through the JSON path instead.
    """

    return EvidenceBundle.model_validate_json(json.dumps(payload["bundle"]))


# --- 1/2. the pinned digests --------------------------------------------------


def test_the_three_phase7_digests_are_still_exactly_these_literals() -> None:
    """Constants 1-3, against the one other place in this repository that knows them.

    The runs are gone; the record of them is not. ``versioning`` was written by
    Phase 7 and has not been edited since, so agreement between two files edited
    at different times by different phases is evidence -- where agreement with a
    recomputation would only be arithmetic, and there is nothing here to
    recompute from.

    And a digest pinned to an arm the registry still records as losing money is a
    record of a completed experiment. Pinned to an open question it would be a
    promise, and the two are not the same kind of claim.
    """

    assert set(PHASE7_RESULT_DIGESTS) == set(LEGACY_ARMS)
    assert len(set(PHASE7_RESULT_DIGESTS.values())) == 3
    for arm, digest in PHASE7_RESULT_DIGESTS.items():
        assert len(digest) == 64
        assert _HEX.issuperset(digest)
        assert f"{arm}_digest={digest}" in SESSION_MOMENTUM_SPEC.versioning

    assert "lost money after costs in every arm" in SESSION_MOMENTUM_SPEC.invalidation
    assert SESSION_MOMENTUM_SPEC.trial_count == 3


def test_the_known_answer_bundle_digest_is_still_exactly_this_literal() -> None:
    """Constant 4, the only one of the four a reader can re-derive.

    Two assertions, because the constant now lives in two files: the first fails
    if one copy is edited without the other, the second fails if the canonical
    encoding of a v1 bundle moves at all. A field added to ``BacktestResult`` --
    which is exactly what a series on the result would have been -- changes those
    bytes, and this is where a change like that would be noticed rather than
    shipped. ``mark_to_market`` did not, because it is excluded from the bytes
    when absent; that this test is still green is the regression guard for the
    exclusion, and the v1 document test below is its consequence.
    """

    assert _CANONICAL_BUNDLE_SHA256 == V1_BUNDLE_SHA256
    assert canonical_sha256(_bundle()) == V1_BUNDLE_SHA256


# --- 3. the v1 legacy bundles -------------------------------------------------


@dataclass
class _RecordedLedger:
    """The two methods the importer calls, and nothing else.

    ``cast(TrialLedger, ...)`` at the single call site below -- the same narrow
    fiction the legacy importer's own unit tests use, and stated here rather than
    inherited. The ledger is not what this gate is about; the store is.
    """

    events: list[LedgerEvent] = field(default_factory=list)

    def append(self, event: LedgerEvent) -> None:
        self.events.append(event)

    def events_for(self, trial_id: str) -> tuple[LedgerEvent, ...]:
        return tuple(event for event in self.events if event.trial_id == trial_id)


def _legacy_result(arm: str, policy: ExitPolicy) -> BacktestResult:
    """One Phase 7 result, in its arm's own shape.

    A synthetic stand-in for an artifact that exists on no machine, and labelled
    as one. What is under test is that the *v1 shape* still seals and verifies --
    not that these are the bytes from 2026, which nothing can check.
    """

    entry = _IMPORT_CLOCK
    trade = SimulatedTrade(
        proposal_id=f"p-{arm}",
        side=Side.BUY,
        lots=Decimal("0.10"),
        entry_price=Decimal("1.10000"),
        entry_at=entry,
        exit_price=Decimal("1.10100"),
        exit_at=entry + timedelta(minutes=12),
        exit_kind=ExitKind.TIME,
        gross_pnl=Decimal("100"),
        commission=Decimal("0"),
        swap=Decimal("0"),
        net_pnl=Decimal("100"),
    )
    return BacktestResult(
        run_id=f"run-{arm}",
        strategy_id="session_momentum_eurusd",
        strategy_version="1",
        exit_policy=policy,
        constitution_sha256="a" * 64,
        contract_sha256="b" * 64,
        instrument_id="fx.eurusd",
        timeframe=Timeframe.M15,
        start=_IMPORT_CLOCK,
        end=_IMPORT_CLOCK + timedelta(hours=1),
        firm_equity=Decimal("100000"),
        cost_model=CostModel(
            commission_per_lot_per_side=Decimal("0"),
            slippage_points_per_side=Decimal("0.4"),
            swap_long_points_per_day=Decimal("-7.7"),
            swap_short_points_per_day=Decimal("2"),
            triple_swap_weekday=2,
        ),
        atr_period=14,
        spread_window=20,
        defective_bar_tolerance=Fraction(0),
        trades=(trade,),
        rejections=(),
        bars_seen=60,
        snapshots_skipped=0,
        net_pnl=Decimal("100"),
    )


def _phase7_artifact(path: Path, result: BacktestResult) -> Path:
    """The document ``backtest run`` wrote in Phase 7, key set included.

    Four keys, because ``research/legacy_import.py`` reads three of them and
    re-derives the fourth from the result the document carries. So a renamed key
    fails here as a refusal rather than passing as a shape nobody read again.
    """

    path.write_text(
        json.dumps(
            {
                "status": "ok",
                "result": result.model_dump(mode="json"),
                "digest": result.digest(),
                "margin_modelled": False,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def _sealed_legacy_bundle(
    tmp_path: Path, arm: str = "none"
) -> tuple[EvidenceStore, EvidenceBundle]:
    """One arm's v1 bundle, through the real importer and the real store.

    The round trip is what a ``REALIZED_CLOSED_TRADES`` document with no series
    actually is; building one by hand would be a second thing that can drift from
    the importer. The store comes back with it because the only way to place a
    document *today's* model cannot write is into the root it is already using.
    """

    store = EvidenceStore(tmp_path / "evidence")
    entry = import_phase7_artifact(
        _phase7_artifact(tmp_path / f"{arm}.json", _legacy_result(arm, LEGACY_ARMS[arm])),
        ledger=cast(TrialLedger, _RecordedLedger()),
        evidence=store,
        now=_IMPORT_CLOCK,
    )
    return store, store.read(entry.evidence_sha256)


def test_the_three_v1_legacy_bundles_still_seal_and_verify(tmp_path: Path) -> None:
    """Evidence sealed before the sidecar existed is still readable after it.

    Through the real importer and the real content-addressed store, one per arm.
    What matters is not that these three particular documents verify -- they are
    built here, not preserved from 2026 -- but that the v1 shape is untouched by
    the series: schema version 1, a ``REALIZED_CLOSED_TRADES`` basis, no
    observations anywhere, and a digest that still agrees with the bytes. A field
    on ``BacktestResult`` moves every one of these addresses, and a bundle an
    operator sealed last year would stop verifying with nothing in the code
    saying why.

    The absences are asserted beside the digests because they are the same
    claim: ``null`` where nothing was measured, and never a zero standing in for
    an unmeasured term. ``mark_to_market is None`` is one of them, and it is the
    one a reader of the series would otherwise have to infer: the importer has no
    marks to seal, and a Phase 7 result cannot have any reconstructed for it now
    that its bar store is gone (design 9).
    """

    store = EvidenceStore(tmp_path / "evidence")
    ledger = _RecordedLedger()

    sealed = {
        arm: import_phase7_artifact(
            _phase7_artifact(tmp_path / f"{arm}.json", _legacy_result(arm, policy)),
            ledger=cast(TrialLedger, ledger),
            evidence=store,
            now=_IMPORT_CLOCK,
        )
        for arm, policy in LEGACY_ARMS.items()
    }

    assert len({entry.evidence_sha256 for entry in sealed.values()}) == 3
    assert len(list(store.root.rglob("*.json"))) == 3
    assert [event.event_type.name for event in ledger.events] == ["LEGACY_IMPORTED"] * 3
    for arm, entry in sealed.items():
        store.verify(entry.evidence_sha256)
        bundle = store.read(entry.evidence_sha256)
        assert bundle.result_schema_version == 1
        assert bundle.return_series_basis is ReturnSeriesBasis.REALIZED_CLOSED_TRADES
        assert bundle.mark_to_market is None
        assert bundle.costs.status is CostAttributionStatus.PARTIAL
        assert bundle.costs.spread_cost is None
        assert bundle.costs.slippage_cost is None
        assert bundle.provenance.registration_state is RegistrationState.LEGACY_UNPREGISTERED
        assert bundle.provenance.holdout_state is HoldoutState.CONTAMINATED
        assert bundle.provenance.dataset_sha256 is None
        assert bundle.source_result_sha256 == bundle.result.digest()
        assert bundle.result.exit_policy == LEGACY_ARMS[arm]


LEGACY_NONE_SHA256 = "a2178cc72fcb6693197577656962d9eb91fa184e812ea89a80c065f26848a921"
"""The address of the ``none`` arm's v1 bundle, pinned.

The one bundle address in this repository that stands for a *stored v1
document* rather than for a value recomputed from a live model, so it is the only
one that would notice a change to how a bundle with no series is encoded. The
three Phase 7 artifacts it stands for are synthetic and exist on no machine; what
is pinned is their shape's encoding, which is the part a schema change moves.
"""


def test_a_document_sealed_before_the_series_existed_still_verifies(tmp_path: Path) -> None:
    """The backward-compatibility claim, made on the bytes rather than assumed.

    The three bundles above cannot prove this, and the gap is the whole reason
    the new field is defaulted *and* excluded when absent: those are built
    *now*, through an importer that cannot omit a field it does not have, so they
    show the v1 shape still constructs. What they cannot show is that a document
    written by the previous build still verifies -- and the three Phase 7
    artifacts this stands for exist on no machine, so nothing can regenerate one
    to find out.

    Both halves of a plain ``= None`` would have broken it, and the second is the
    one nobody predicts. A required field under ``extra="forbid"`` refuses the
    document outright. A merely *defaulted* one is worse: ``EvidenceStore.read``
    re-serializes what it decoded and refuses any document whose bytes are not
    today's canonical encoding, so a field that always wrote itself out would add
    its own ``null`` to these bytes, fail the read, and turn ``research trial
    verify`` into a failure on a chain it sealed itself.

    So the pinned address is the assertion, and the absence of the keys in the
    stored bytes is what makes it the pre-extension document rather than a
    description of one. **Both** fields are asserted, and that is the whole
    subject of the test: ``mark_to_market`` here, and 8B2a's
    ``cost_attribution`` beside it, are one decision taken twice for one reason,
    and the second decision is a regression guard only if the *same* stored bytes
    are checked for it. A copy of this test in 8B2a's own file would buy the same
    protection less well -- deleting ``cost_attribution`` outright would leave the
    copy passing trivially on ``reparsed.cost_attribution is None``, while these
    two lines would still see the key in the bytes.
    """

    store, bundle = _sealed_legacy_bundle(tmp_path, "none")
    document = (store.root / LEGACY_NONE_SHA256[:2] / f"{LEGACY_NONE_SHA256}.json").read_bytes()

    # No key at all: this is what the previous build wrote, byte for byte, so the
    # extension is invisible to it rather than tolerated by it. Both keys, because
    # both fields are defaulted-and-excluded and neither is allowed into a v1
    # document's bytes.
    assert b"mark_to_market" not in document
    assert b"cost_attribution" not in document
    assert canonical_sha256(bundle) == LEGACY_NONE_SHA256

    store.verify(LEGACY_NONE_SHA256)
    reparsed = store.read(LEGACY_NONE_SHA256)
    assert reparsed == bundle
    assert reparsed.mark_to_market is None
    assert reparsed.cost_attribution is None
    assert reparsed.daily_returns == bundle.daily_returns


# --- 4/5. the series a real run produced --------------------------------------


def test_every_processed_bar_is_marked_and_every_mark_reconciles() -> None:
    """The two things an operator reads the series for.

    One observation per *processed* bar -- not per snapshot, and not per bar the
    strategy proposed on. A run that traded once and did nothing for two hundred
    bars still marks all of them, which is the whole difference between a
    valuation path and a trade log. And at every one of them
    ``equity == firm_equity + cumulative_realized_pnl + unrealized_pnl``: the
    mark is a composition of the two totals, not a third number carried beside
    them that could disagree with either.

    ``bars_seen`` is a count the result already published, so the equality is
    between two things an operator can compare without this file.
    """

    outcome = _outcome(_session_ramp())
    result = outcome.result
    series = outcome.equity

    assert result.trades, "a run with no trades proves nothing about a realized total"
    assert len(series.observations) == result.bars_seen
    # Not one per snapshot, and not one per trade either. This window asked the
    # strategy about fewer bars than it processed, and the skipped ones are still
    # marked -- so the count above is a statement about bars, and an engine that
    # marked where it thought was shorter than this run.
    assert result.snapshots_skipped > 0
    assert result.bars_seen - result.snapshots_skipped < result.bars_seen
    for point in series.observations:
        reconciled = series.firm_equity + point.cumulative_realized_pnl + point.unrealized_pnl
        assert point.equity == reconciled
        # Nothing open means nothing unrealized. Checked per point because this
        # is what makes the flat reconciliation below reduce to a single term,
        # and a flag that could disagree with the count would be duplicated state
        # -- the thing ``result.py`` refuses to carry.
        assert (point.unrealized_pnl == 0) == (point.open_positions == 0)

    # Flat, so the last mark carries the whole of ``net_pnl`` and nothing else.
    assert series.is_flat is True
    assert series.observations[-1].cumulative_realized_pnl == result.net_pnl
    assert series.observations[-1].unrealized_pnl == 0


def test_a_run_ending_holding_a_position_is_reported_and_not_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A run with an open position is evidence with a known defect, so it is named.

    Both halves of the same window, engine and command, because the claim is
    ``is_flat`` and its report: the final observation is one open position
    carrying unrealized P&L, ``mark_to_market_flat`` is false, the bundle is
    still emitted, and the exit code is 0. A command that refused the run would
    hide the defect rather than name it, and an operator would meet it for the
    first time at a later gate -- or not at all.

    Reported, never promoted. The day it is marked on is still in the series: a
    position that is open is a mark, not a gap, and a rectangular daily series
    that dropped the day would read as a flat one.
    """

    outcome = _outcome(_session_ramp(), last_bar=_MID_MORNING_BAR)
    payload = _run_command(monkeypatch, tmp_path, marked=True, last_bar=_MID_MORNING_BAR)

    final = outcome.equity.observations[-1]
    assert outcome.equity.is_flat is False
    assert outcome.result.trades == ()
    assert final.open_positions == 1
    assert final.unrealized_pnl != 0
    assert len(outcome.equity.observations) == outcome.result.bars_seen

    assert payload["mark_to_market_flat"] is False
    bundle = _bundle_of(payload)
    assert bundle.result.trades == ()
    assert len(bundle.daily_returns) == 1
    # No trades means nothing was charged, so every component is a real zero --
    # and 8B2a is what makes that a measurement rather than a claim: the summary
    # is COMPLETE and the sealed attribution behind it is an empty tuple, so a
    # gate reading COMPLETE can see that there was nothing to attribute. Under
    # 8B1 this run was PARTIAL with two ``None``s, and the distinction between a
    # measured zero and an unmeasured term was carried by the ``None`` itself.
    assert bundle.costs.status is CostAttributionStatus.COMPLETE
    assert bundle.costs.commission == 0
    assert bundle.costs.spread_cost == 0
    assert bundle.costs.slippage_cost == 0
    assert bundle.cost_attribution is not None
    assert bundle.cost_attribution.trades == ()


# --- 6/7. the command's two payloads -----------------------------------------


def test_the_command_now_produces_the_mark_to_market_basis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The 8A gap, closed: an enum member no command emitted now has a producer.

    8A shipped ``ReturnSeriesBasis.MARK_TO_MARKET`` with nothing that set it, so
    the value was a claim about a future phase rather than a record of anything.
    Asserted against the payload the command actually prints, and against the
    enum rather than the wire string, so a rename of the *member* cannot pass as
    the same basis. A rename of the serialised value alone would pass: the enum
    member is what is compared, so swapping the string on the wire while keeping
    the member would go unnoticed here.

    The series is asserted here, not only in the daily reduction, and that is the
    assertion four review passes were missing. ``daily_returns`` is *derived*
    from the per-bar series, so a test that only reads it passes whether or not
    the series was ever sealed: the reduction is present either way. What was
    absent was every check that the reduction's own input reached the evidence
    store at all, which is why the design, the plan, the README and the acceptance
    file could all describe a sealed series that no document carried.

    Two digests over one result, deliberately unequal: the payload's ``digest`` is
    the bundle's content address and ``source_result_sha256`` is the result's own
    declaration-ordered digest. A change to the result's serialisation moves
    both, and the four pinned constants above with them.
    """

    payload = _run_command(monkeypatch, tmp_path, marked=True)

    assert sorted(payload) == ["bundle", "digest", "mark_to_market_flat", "status"]
    assert payload["status"] == "ok"
    bundle = _bundle_of(payload)
    assert bundle.return_series_basis is ReturnSeriesBasis.MARK_TO_MARKET
    assert payload["digest"] == canonical_sha256(bundle)
    assert payload["digest"] != bundle.source_result_sha256
    assert bundle.source_result_sha256 == bundle.result.digest()
    # 8B1 computes no digest of the bar store, so it writes none. A fabricated
    # one would vouch for data nobody hashed.
    assert bundle.provenance.dataset_sha256 is None
    assert bundle.provenance.registration_state is RegistrationState.PROSPECTIVE

    # The series, sealed. ``bars_seen`` is the count the result already published,
    # so this is an equality between two things an operator can compare without
    # this file, and a stored or subsampled series cannot pass it.
    assert bundle.mark_to_market is not None
    assert len(bundle.mark_to_market.observations) == bundle.result.bars_seen
    assert bundle.mark_to_market.firm_equity == bundle.result.firm_equity
    # The daily series is a function of the sealed one: the window reaches the day
    # the last observation falls on, which is the coverage the next test in the
    # integration suite makes the load-bearing claim about.
    assert bundle.daily_returns[-1].day == bundle.mark_to_market.observations[-1].marked_at.date()
    assert bundle.mark_to_market.is_flat is payload["mark_to_market_flat"]


def test_the_basis_and_the_sealed_series_cannot_disagree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both directions of the coupling, and one real series on each side of them.

    ``return_series_basis`` names which series a bundle is reporting, so a bundle
    whose basis and series disagree is claiming two different returns for one run
    with nothing to tell a reader which one a downstream number used. Both
    directions are refused rather than reconciled, and the series used here is a
    real engine's -- so what is under test is the coupling, not the arithmetic of
    a series that happens to satisfy its own validators.

    Documents rather than constructors: the CLI's own JSON is what a reader gets,
    and going through the JSON path is also what proves the refusal is the
    validator's and not a strict-mode artefact of building the model directly.
    """

    marked = _bundle_of(_run_command(monkeypatch, tmp_path, marked=True))
    without_series = json.loads(marked.model_dump_json())
    del without_series["mark_to_market"]
    assert marked.mark_to_market is not None, (
        "the half above is meaningless if the run carried none"
    )

    realized = _sealed_legacy_bundle(tmp_path)[1]
    with_series = json.loads(realized.model_dump_json())
    with_series["mark_to_market"] = json.loads(_outcome(_session_ramp()).equity.model_dump_json())

    # A MARK_TO_MARKET basis with nothing to mark.
    with pytest.raises(ValidationError, match="must carry the series it reduced"):
        EvidenceBundle.model_validate_json(json.dumps(without_series))
    # A REALIZED_CLOSED_TRADES basis carrying one anyway: the two would be two
    # different answers to "what did this run return", side by side.
    with pytest.raises(ValidationError, match="only a mark-to-market bundle may carry"):
        EvidenceBundle.model_validate_json(json.dumps(with_series))

    # And each refusal is one edit from a document that validates, so the coupling
    # rule is the only thing between these two and a seal rather than an
    # accident of the fixtures.
    assert EvidenceBundle.model_validate_json(marked.model_dump_json()) == marked
    assert EvidenceBundle.model_validate_json(realized.model_dump_json()) == realized


def test_a_sealed_series_must_share_the_result_it_reduces_one_firm_equity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A self-consistent series from the wrong firm equity is still the wrong run.

    The series is sealed so a reader can audit the reduction, and an audit of
    somebody else's equity path audits nothing. Rebasing the series *and* every
    observation by the same amount keeps each point reconciling against the firm
    equity beside it, so ``EquitySeries`` accepts the result and the bundle's
    rule is the only thing left that can refuse it. The words are
    ``BacktestOutcome``'s own, so the disagreement reads the same whether it was
    caught where the series was produced or where it is read back.
    """

    marked = _bundle_of(_run_command(monkeypatch, tmp_path, marked=True))
    assert marked.mark_to_market is not None, "the case below is vacuous without a series"

    rebased = json.loads(marked.model_dump_json())
    rebased["mark_to_market"]["firm_equity"] = str(
        Decimal(rebased["mark_to_market"]["firm_equity"]) + 1
    )
    for point in rebased["mark_to_market"]["observations"]:
        point["equity"] = str(Decimal(point["equity"]) + 1)

    with pytest.raises(ValidationError, match="the series and the result must share one firm"):
        EvidenceBundle.model_validate_json(json.dumps(rebased))


def test_a_sealed_series_must_hold_one_observation_per_processed_bar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A series that stops early is a series of a shorter run, not of this one.

    The reduction beside it still reconciles -- ``daily_returns`` is not what
    counts bars, ``result.bars_seen`` is, and the two are checked here rather
    than inferred. Truncation is the shape a subsampling or partial-write bug
    takes, and it is the reason the count is compared against the result's own
    count rather than against a constant.
    """

    marked = _bundle_of(_run_command(monkeypatch, tmp_path, marked=True))
    assert marked.mark_to_market is not None, "the case below is vacuous without a series"
    assert len(marked.mark_to_market.observations) > 1, (
        "a one-observation series could not disagree with a count above one"
    )

    truncated = json.loads(marked.model_dump_json())
    truncated["mark_to_market"]["observations"].pop()

    with pytest.raises(ValidationError, match="one observation per processed bar"):
        EvidenceBundle.model_validate_json(json.dumps(truncated))


def test_a_flat_sealed_series_must_reduce_to_the_results_own_net_pnl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A flat series that stops short of the result's own total is a different run.

    The third of the three equalities ``BacktestOutcome`` asserts, and the one
    that binds the series to the trades rather than to the result's shape: the
    first two accept a series from the right firm equity with the right number of
    bars, and a realized total that belongs to some other run passes both. The
    mark is re-based with the same amount, so every point still reconciles and
    ``EquitySeries`` accepts it -- the bundle's rule is the only thing left.

    Conditional on ``is_flat`` exactly as ``mark.py`` gates it, and the case
    below the assertion is gated on is the reason it can be: the non-flat run
    recorded one bar earlier carries a legitimate final total that excludes the
    mark its open position never realized, and it still validates.
    """

    marked = _bundle_of(_run_command(monkeypatch, tmp_path, marked=True))
    assert marked.mark_to_market is not None, "the case below is vacuous without a series"
    assert marked.mark_to_market.is_flat is True, "the assertion under test does not apply"

    understated = json.loads(marked.model_dump_json())
    final = understated["mark_to_market"]["observations"][-1]
    final["cumulative_realized_pnl"] = str(Decimal(final["cumulative_realized_pnl"]) - 1)
    final["equity"] = str(Decimal(final["equity"]) - 1)

    with pytest.raises(ValidationError, match="final realized total must equal the result"):
        EvidenceBundle.model_validate_json(json.dumps(understated))

    # The gate is the conditional branch, not a blanket refusal: the same run
    # ended one bar early ends holding a position, and that document is still
    # valid evidence of a defect a later gate refuses.
    open_position = _run_command(monkeypatch, tmp_path, marked=True, last_bar=_MID_MORNING_BAR)
    bundle = _bundle_of(open_position)
    assert bundle.mark_to_market is not None
    assert bundle.mark_to_market.is_flat is False
    assert EvidenceBundle.model_validate_json(json.dumps(open_position["bundle"])) == bundle


def test_without_the_flag_the_payload_is_the_phase7_artifact_and_names_no_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pre-8B1 contract, asserted exactly, over the same result either way.

    ``import-legacy`` reads ``status``, ``result`` and ``digest`` from this
    document, so the key set is pinned on its own: the importer ignores a key it
    does not need, which would let a fifth one arrive unnoticed. The digest is
    then compared across the two invocations, so the flag is proved to change the
    *document's* address without changing the result inside it.
    """

    plain = _run_command(monkeypatch, tmp_path, marked=False)
    marked = _run_command(monkeypatch, tmp_path, marked=True)

    assert sorted(plain) == ["digest", "margin_modelled", "result", "status"]
    assert "bundle" not in plain
    assert plain["status"] == "ok"
    assert plain["margin_modelled"] is False
    assert plain["digest"] == marked["bundle"]["source_result_sha256"]
    assert plain["result"] == marked["bundle"]["result"]


def test_the_command_declares_the_flag_and_all_six_identity_options() -> None:
    """The surface 8B1 added, read off the Click command rather than the help text.

    ``--help`` proves an option is *documented*; this proves it is *declared*. It
    is not the only check here that would notice a rename -- the three CLI
    invocations below fail on one too. It is not a claim that these seven are
    the command's only additions: nothing here compares against an older
    parameter list, and it would pass with an eighth identity option added beside
    them.

    The refusals are the integration suite's: an exit-2 assertion with no database
    behind it cannot tell a refused operator mistake from a missing database.
    """

    command = get_command(cli.app).commands["backtest"].commands["run"]
    declared = {
        option for parameter in command.params for option in parameter.opts if option[:2] == "--"
    }

    assert {_FLAG, *_IDENTITY} <= declared
