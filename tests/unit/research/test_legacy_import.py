"""The Phase 7 legacy importer.

A Phase 7 artifact is a backtest result that was produced before the trial ledger
existed, so importing it has to say what it can and cannot support: the closed
trades really do yield a realized daily series, the spread and slippage really
were folded into the fill prices, and no dataset hash was ever recorded. The tests
below pin each of those, because every one of them is a place where a shorter
importer would invent a fact rather than record its absence.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from trading_house.core.errors import EvidenceIntegrityError
from trading_house.core.exits import NoExitPolicy
from trading_house.core.schemas import Side
from trading_house.marketdata.models import Timeframe
from trading_house.research.backtest.costs import CostModel
from trading_house.research.backtest.fills import ExitKind
from trading_house.research.backtest.result import BacktestResult, SimulatedTrade
from trading_house.research.canonical import canonical_sha256
from trading_house.research.evidence import EvidenceStore
from trading_house.research.legacy_import import (
    LegacyImportResult,
    derive_realized_daily_returns,
    import_phase7_artifact,
)
from trading_house.research.trial_ledger import (
    CostAttributionStatus,
    HoldoutState,
    LedgerEvent,
    LedgerEventType,
    LedgerRecord,
    LegacyImportedPayload,
    RegistrationState,
    ReturnSeriesBasis,
    ScopeKind,
    TrialCounters,
    TrialLedger,
    TrialSpec,
    trial_counters,
)

_NOW = datetime(2024, 1, 1, tzinfo=UTC)


# Copied from tests/unit/research/backtest/test_result.py so the importer's tests
# are read against a result the backtester's own tests already accept; only the
# module clock moves, to 2024-01-01 so the two trades below land on two UTC days.
def _cost_model(**overrides: object) -> CostModel:
    defaults: dict[str, object] = {
        "commission_per_lot_per_side": Decimal("3.50"),
        "slippage_points_per_side": Decimal("1"),
        "swap_long_points_per_day": Decimal("-1"),
        "swap_short_points_per_day": Decimal("-1"),
        "triple_swap_weekday": 2,
    }
    return CostModel(**{**defaults, **overrides})  # type: ignore[arg-type]


def _trade(**overrides: object) -> SimulatedTrade:
    gross_pnl = overrides.pop("gross_pnl", Decimal("100"))
    commission = overrides.pop("commission", Decimal("7"))
    swap = overrides.pop("swap", Decimal("-3"))
    net_pnl = overrides.pop("net_pnl", gross_pnl - commission + swap)  # type: ignore[operator]
    defaults: dict[str, object] = {
        "proposal_id": "p-1",
        "side": Side.BUY,
        "lots": Decimal("0.10"),
        "entry_price": Decimal("1.10000"),
        "entry_at": _NOW,
        "exit_price": Decimal("1.10100"),
        "exit_at": _NOW + timedelta(minutes=12),
        "exit_kind": ExitKind.TIME,
        "gross_pnl": gross_pnl,
        "commission": commission,
        "swap": swap,
        "net_pnl": net_pnl,
    }
    return SimulatedTrade(**{**defaults, **overrides})  # type: ignore[arg-type]


def _result(**overrides: object) -> BacktestResult:
    trades = overrides.pop("trades", (_trade(),))
    net_pnl = overrides.pop(
        "net_pnl",
        sum((t.net_pnl for t in trades), Decimal(0)),  # type: ignore[union-attr]
    )
    defaults: dict[str, object] = {
        "run_id": "run-1",
        "strategy_id": "strat-1",
        "strategy_version": "v1",
        "exit_policy": NoExitPolicy(kind="none"),
        "constitution_sha256": "a" * 64,
        "contract_sha256": "b" * 64,
        "instrument_id": "fx.eurusd",
        "timeframe": Timeframe.M1,
        "start": _NOW,
        "end": _NOW + timedelta(hours=1),
        "firm_equity": Decimal("100000"),
        "cost_model": _cost_model(),
        "atr_period": 14,
        "spread_window": 20,
        "defective_bar_tolerance": Fraction(0),
        "trades": trades,
        "rejections": (),
        "bars_seen": 60,
        "snapshots_skipped": 0,
        "net_pnl": net_pnl,
    }
    return BacktestResult(**{**defaults, **overrides})  # type: ignore[arg-type]


def _result_with_two_trades() -> BacktestResult:
    first = _trade(
        entry_at=_NOW,
        exit_at=datetime(2024, 1, 2, 12, 0, tzinfo=UTC),
        gross_pnl=Decimal("1000"),
        commission=Decimal("0"),
        swap=Decimal("0"),
        net_pnl=Decimal("1000"),
    )
    second = _trade(
        entry_at=datetime(2024, 1, 2, 12, 0, tzinfo=UTC),
        exit_at=datetime(2024, 1, 3, 12, 0, tzinfo=UTC),
        gross_pnl=Decimal("0"),
        commission=Decimal("0"),
        swap=Decimal("0"),
        net_pnl=Decimal("0"),
    )
    return _result(
        start=_NOW,
        end=datetime(2024, 1, 3, 23, 59, tzinfo=UTC),
        trades=(first, second),
        net_pnl=Decimal("1000"),
    )


def _result_with_costs() -> BacktestResult:
    """A legacy result whose trades carry non-zero commission and opposite-signed
    swap.

    The brief's two-day fixture zeroes both terms so its daily returns are exact
    decimals, which means a cost summary hardcoded to zero would pass every
    assertion made against it. These two trades carry non-zero charges of their
    own -- 7 and 11 of commission, -3 and +5 of swap -- so the bundle's totals
    pin a signed sum: 18 of commission, 2 of swap. A zero, a first-trade-only
    total, and an unsigned accumulation of the swap (which would read 8) are
    three different wrong answers and all three fail.
    """

    first = _trade(
        entry_at=_NOW,
        exit_at=datetime(2024, 1, 2, 12, 0, tzinfo=UTC),
        gross_pnl=Decimal("1000"),
        commission=Decimal("7"),
        swap=Decimal("-3"),
        net_pnl=Decimal("990"),
    )
    second = _trade(
        entry_at=datetime(2024, 1, 2, 12, 0, tzinfo=UTC),
        exit_at=datetime(2024, 1, 3, 12, 0, tzinfo=UTC),
        gross_pnl=Decimal("500"),
        commission=Decimal("11"),
        swap=Decimal("5"),
        net_pnl=Decimal("494"),
    )
    return _result(
        start=_NOW,
        end=datetime(2024, 1, 3, 23, 59, tzinfo=UTC),
        trades=(first, second),
        net_pnl=Decimal("1484"),
    )


def _write_outer_artifact(path: Path, result: BacktestResult) -> Path:
    artifact = path / "phase7.json"
    artifact.write_text(
        json.dumps(
            {
                "status": "ok",
                "result": json.loads(result.model_dump_json()),
                "digest": result.digest(),
                "margin_modelled": False,
            },
            # Indented, because a preserved artifact is a file an operator kept
            # and read. It also means the file's bytes are not the bytes
            # ``json.dumps(json.loads(file))`` would produce, so the recorded
            # ``source_artifact_sha256`` has to be a digest of the file rather
            # than of a re-encoding that happens to look the same.
            indent=2,
        ),
        encoding="utf-8",
    )
    return artifact


class FakeLedger:
    def __init__(self) -> None:
        self.events: list[LedgerEvent] = []

    @property
    def append_count(self) -> int:
        return len(self.events)

    def append(self, event: LedgerEvent) -> None:
        self.events.append(event)

    def events_for(self, trial_id: str) -> tuple[LedgerEvent, ...]:
        return tuple(event for event in self.events if event.trial_id == trial_id)


def _fake_ledger() -> FakeLedger:
    return FakeLedger()


def _record(event: LedgerEvent) -> LedgerRecord:
    """The row the ledger's ``events_for`` actually returns: the same
    event with the chain's columns around it and the payload still JSON text.

    ``sequence`` and ``recorded_at`` are the two columns the *database* assigns,
    and they are enumerated here on purpose rather than left implicit. Design
    §5.3 excludes them from the request comparison that makes a retry
    idempotent, so the importer must never read them: it matches on ``event_id``
    and reports the payload's own ``evidence_sha256``. Their values are
    therefore arbitrary below, and ``sequence=1`` says exactly that.
    """

    return LedgerRecord(
        sequence=1,
        event_id=event.event_id,
        scope_kind=event.scope_kind,
        scope_id=event.scope_id,
        trial_id=event.trial_id,
        attempt_id=event.attempt_id,
        event_type=event.event_type,
        spec_sha256=event.spec_sha256,
        event_json=event.model_dump(mode="json"),
        payload_sha256=canonical_sha256(event.payload),
        previous_hash="0" * 64,
        event_hash="b" * 64,
        legacy=event.legacy,
        legacy_reason=event.legacy_reason,
        recorded_at=_NOW,
    )


class FakeRecordLedger:
    """``FakeLedger`` answering in rows, which is the production shape.

    The importer's idempotency check reads a recorded event through this, so
    testing only the ``LedgerEvent`` shape would leave the branch that runs
    against PostgreSQL unexercised.
    """

    def __init__(self, inner: FakeLedger) -> None:
        self._inner = inner

    def append(self, event: LedgerEvent) -> None:
        self._inner.append(event)

    def events_for(self, trial_id: str) -> tuple[LedgerRecord, ...]:
        return tuple(_record(event) for event in self._inner.events_for(trial_id))


def _import(artifact: Path, ledger: FakeLedger, tmp_path: Path) -> LegacyImportResult:
    return import_phase7_artifact(
        artifact,
        ledger=cast(TrialLedger, ledger),
        evidence=EvidenceStore(tmp_path / "evidence"),
        now=_NOW,
    )


def test_realized_returns_include_every_utc_calendar_day() -> None:
    points = derive_realized_daily_returns(_result_with_two_trades())

    assert [point.day.isoformat() for point in points] == [
        "2024-01-01",
        "2024-01-02",
        "2024-01-03",
    ]
    assert points[1].value == Decimal("0.01")
    assert points[2].value == Decimal("0")


def test_realized_returns_are_refused_when_equity_is_not_positive() -> None:
    """A return over a non-positive base is not a number, and emitting one would
    let a wiped-out account report a spectacular daily series."""

    with pytest.raises(EvidenceIntegrityError):
        derive_realized_daily_returns(
            _result_with_two_trades().model_copy(update={"firm_equity": Decimal("0")})
        )


def test_a_trade_exiting_after_the_run_window_gets_its_own_day(tmp_path: Path) -> None:
    """The engine reads one bar past ``request.end`` to warm the last snapshot,
    so a position opened on the final bar closes *after* ``result.end``. That
    exit is real P&L, and the series must carry it on its own day rather than
    refusing the artifact: the sum of every point has to equal ``result.net_pnl``,
    which counted it."""

    final_bar = _trade(
        entry_at=datetime(2024, 1, 3, 12, 0, tzinfo=UTC),
        exit_at=datetime(2024, 1, 4, 9, 0, tzinfo=UTC),
        gross_pnl=Decimal("500"),
        commission=Decimal("0"),
        swap=Decimal("0"),
        net_pnl=Decimal("500"),
    )
    earlier = _trade(
        entry_at=_NOW,
        exit_at=datetime(2024, 1, 2, 12, 0, tzinfo=UTC),
        gross_pnl=Decimal("1000"),
        commission=Decimal("0"),
        swap=Decimal("0"),
        net_pnl=Decimal("1000"),
    )
    result = _result(
        start=_NOW,
        end=datetime(2024, 1, 3, 23, 59, tzinfo=UTC),
        trades=(earlier, final_bar),
        net_pnl=Decimal("1500"),
    )

    points = derive_realized_daily_returns(result)

    assert [point.day.isoformat() for point in points] == [
        "2024-01-01",
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
    ]
    assert points[1].value == Decimal("0.01")
    assert points[2].value == Decimal("0")
    assert points[3].value == Decimal("500") / Decimal("101000")
    # The day-4 base is the equity the earlier days produced, so writing the
    # divisor as firm_equity + the day-2 P&L states the reconciliation exactly:
    # 1000 lands on day 2, 500 lands on day 4, and the two are result.net_pnl.
    # (Compounding the returns instead would divide 500/101000 at the default
    # 28-digit context and not land back on 1.015, so the check would assert
    # nothing.)
    assert points[3].value == Decimal("500") / (result.firm_equity + Decimal("1000"))
    assert result.net_pnl == Decimal("1500")

    # And it imports, rather than being refused.
    artifact = _write_outer_artifact(tmp_path, result)
    store = EvidenceStore(tmp_path / "evidence")
    outcome = _import(artifact, _fake_ledger(), tmp_path)
    assert store.read(outcome.evidence_sha256).daily_returns == points


def test_a_trade_exiting_before_the_run_window_is_refused() -> None:
    """The one out-of-window exit that is not a legitimate engine outcome: a
    position cannot open on a bar the run never read, and its day precedes the
    first day the walk visits, so folding it in would attribute P&L to a day that
    is not in the series at all."""

    pre_start = _trade(
        entry_at=datetime(2023, 12, 31, 12, 0, tzinfo=UTC),
        exit_at=datetime(2023, 12, 31, 18, 0, tzinfo=UTC),
        gross_pnl=Decimal("5000"),
        commission=Decimal("0"),
        swap=Decimal("0"),
        net_pnl=Decimal("5000"),
    )
    result = _result(
        start=_NOW,
        end=datetime(2024, 1, 3, 23, 59, tzinfo=UTC),
        trades=(pre_start,),
        net_pnl=Decimal("5000"),
    )

    with pytest.raises(EvidenceIntegrityError):
        derive_realized_daily_returns(result)


def test_an_artifact_whose_trade_precedes_its_window_is_not_imported(tmp_path: Path) -> None:
    """The refusal inherits to the import: the bundle cannot be built, so nothing
    is written to the evidence store and nothing is appended to the ledger."""

    pre_start = _trade(
        entry_at=datetime(2023, 12, 31, 12, 0, tzinfo=UTC),
        exit_at=datetime(2023, 12, 31, 18, 0, tzinfo=UTC),
        gross_pnl=Decimal("5000"),
        commission=Decimal("0"),
        swap=Decimal("0"),
        net_pnl=Decimal("5000"),
    )
    result = _result(
        start=_NOW,
        end=datetime(2024, 1, 3, 23, 59, tzinfo=UTC),
        trades=(pre_start,),
        net_pnl=Decimal("5000"),
    )
    artifact = _write_outer_artifact(tmp_path, result)
    ledger = _fake_ledger()

    with pytest.raises(EvidenceIntegrityError):
        _import(artifact, ledger, tmp_path)

    assert ledger.append_count == 0
    assert not (tmp_path / "evidence").exists()


def test_import_verifies_the_original_result_digest(tmp_path: Path) -> None:
    artifact = _write_outer_artifact(tmp_path, _result_with_two_trades())
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    payload["digest"] = "0" * 64
    artifact.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvidenceIntegrityError):
        import_phase7_artifact(
            artifact,
            ledger=cast(TrialLedger, _fake_ledger()),
            evidence=EvidenceStore(tmp_path / "evidence"),
            now=_NOW,
        )


def test_import_refuses_a_result_that_disagrees_with_its_digest(tmp_path: Path) -> None:
    """The other direction: the digest field is well formed and the *content* was
    edited, which only re-deriving the digest from the carried result catches."""

    artifact = _write_outer_artifact(tmp_path, _result_with_two_trades())
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    payload["result"]["firm_equity"] = 999999
    artifact.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvidenceIntegrityError):
        import_phase7_artifact(
            artifact,
            ledger=cast(TrialLedger, _fake_ledger()),
            evidence=EvidenceStore(tmp_path / "evidence"),
            now=_NOW,
        )


def test_a_failed_phase7_artifact_is_refused(tmp_path: Path) -> None:
    """A document the CLI marked ``status: error`` is refused even when it also
    carries a parseable result.

    The bare ``{"status": "error"}`` shape a failed run really writes is already
    refused, because it has no result to read. The result-carrying variant is
    the one that needs the status: without it a partial run that emitted a
    result and then failed would import as a clean legacy artifact.
    """

    result = _result_with_two_trades()
    (tmp_path / "phase7.json").write_text(
        json.dumps(
            {
                "status": "error",
                "detail": "the run failed after the last bar",
                "result": json.loads(result.model_dump_json()),
                "digest": result.digest(),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(EvidenceIntegrityError):
        import_phase7_artifact(
            tmp_path / "phase7.json",
            ledger=cast(TrialLedger, _fake_ledger()),
            evidence=EvidenceStore(tmp_path / "evidence"),
            now=_NOW,
        )


def test_a_missing_artifact_is_an_integrity_error(tmp_path: Path) -> None:
    with pytest.raises(EvidenceIntegrityError) as error:
        import_phase7_artifact(
            tmp_path / "absent.json",
            ledger=cast(TrialLedger, _fake_ledger()),
            evidence=EvidenceStore(tmp_path / "evidence"),
            now=_NOW,
        )
    assert error.value.__cause__ is not None


def test_a_truncated_artifact_is_an_integrity_error(tmp_path: Path) -> None:
    """A half-copied file is not evidence, and the parse failure it produces is
    the same refusal as a missing one rather than a JSON error at the call site."""

    (tmp_path / "phase7.json").write_bytes(b'{"status": "ok", "resu')

    with pytest.raises(EvidenceIntegrityError) as error:
        import_phase7_artifact(
            tmp_path / "phase7.json",
            ledger=cast(TrialLedger, _fake_ledger()),
            evidence=EvidenceStore(tmp_path / "evidence"),
            now=_NOW,
        )
    assert error.value.__cause__ is not None


def test_an_artifact_whose_result_does_not_parse_is_refused(tmp_path: Path) -> None:
    (tmp_path / "phase7.json").write_text(
        json.dumps({"status": "ok", "result": {"run_id": "run-1"}, "digest": "0" * 64}),
        encoding="utf-8",
    )

    with pytest.raises(EvidenceIntegrityError) as error:
        import_phase7_artifact(
            tmp_path / "phase7.json",
            ledger=cast(TrialLedger, _fake_ledger()),
            evidence=EvidenceStore(tmp_path / "evidence"),
            now=_NOW,
        )
    assert error.value.__cause__ is not None


def test_legacy_import_is_idempotent_by_source_digest(tmp_path: Path) -> None:
    artifact = _write_outer_artifact(tmp_path, _result_with_two_trades())
    ledger = _fake_ledger()

    first = _import(artifact, ledger, tmp_path)
    second = _import(artifact, ledger, tmp_path)

    assert second.already_present
    assert ledger.append_count == 1
    # The retry is the same import, so it must report the same identity back
    # rather than mint a second attempt.
    assert (second.trial_id, second.attempt_id) == (first.trial_id, first.attempt_id)
    assert second.evidence_sha256 == first.evidence_sha256
    assert second.source_result_sha256 == first.source_result_sha256


def test_a_second_import_under_a_different_clock_reports_the_recorded_digest(
    tmp_path: Path,
) -> None:
    """``now`` is operator-declared registration time, so a later retry produces
    different bundle bytes. The answer must still be the digest the ledger
    actually recorded, not the one this call would have written."""

    artifact = _write_outer_artifact(tmp_path, _result_with_two_trades())
    ledger = _fake_ledger()
    first = _import(artifact, ledger, tmp_path)

    later = import_phase7_artifact(
        artifact,
        ledger=cast(TrialLedger, ledger),
        evidence=EvidenceStore(tmp_path / "evidence"),
        now=_NOW + timedelta(days=30),
    )

    assert later.already_present
    assert later.evidence_sha256 == first.evidence_sha256
    assert ledger.append_count == 1


def test_idempotency_reads_the_recorded_digest_back_out_of_a_ledger_row(
    tmp_path: Path,
) -> None:
    """The production ledger answers ``events_for`` with rows, and the retry has
    to read the recorded digest through one. A second, unrelated event on the
    same trial is in the way: the match is by our deterministic event id, not by
    position."""

    artifact = _write_outer_artifact(tmp_path, _result_with_two_trades())
    inner = _fake_ledger()
    ledger = FakeRecordLedger(inner)
    first = _import(artifact, inner, tmp_path)
    # Ahead of the real event in chain order, so the retry has to walk past it:
    # the match is by our deterministic event id, not by position. The decoy
    # carries a *different* evidence digest as well as a different id, so a
    # lookup that dropped the id guard and took the first legacy event it saw
    # would report that digest instead of the recorded one.
    decoy = inner.events[0].model_copy(
        update={
            "event_id": uuid4(),
            "payload": inner.events[0].payload.model_copy(update={"evidence_sha256": "c" * 64}),
        }
    )
    inner.events.insert(0, decoy)
    before_retry = len(inner.events)

    second = import_phase7_artifact(
        artifact,
        ledger=cast(TrialLedger, ledger),
        evidence=EvidenceStore(tmp_path / "evidence"),
        now=_NOW,
    )

    assert second.already_present
    assert second.evidence_sha256 == first.evidence_sha256
    assert len(inner.events) == before_retry


def test_a_chain_row_the_importer_cannot_read_is_refused(tmp_path: Path) -> None:
    """``events_for`` hands back a row whose jsonb column no longer decodes to an
    event. A retry that guessed at it would answer "not imported yet" and append
    a second copy of the trial, so the unreadable row has to be the loud thing."""

    artifact = _write_outer_artifact(tmp_path, _result_with_two_trades())
    inner = _fake_ledger()
    _import(artifact, inner, tmp_path)
    broken = [
        _record(event).model_copy(update={"event_json": {"payload": "not an event"}})
        for event in inner.events
    ]

    class BrokenRowLedger:
        def events_for(self, trial_id: str) -> tuple[LedgerRecord, ...]:
            return tuple(broken)

    with pytest.raises(EvidenceIntegrityError) as error:
        import_phase7_artifact(
            artifact,
            ledger=cast(TrialLedger, BrokenRowLedger()),
            evidence=EvidenceStore(tmp_path / "evidence"),
            now=_NOW,
        )
    # The public message is deliberately empty; the parse failure stays on the
    # private cause so an operator can still tell a bad artifact from a bad
    # column.
    assert error.value.__cause__ is not None


def test_legacy_evidence_records_what_it_cannot_know(tmp_path: Path) -> None:
    artifact = _write_outer_artifact(tmp_path, _result_with_two_trades())
    store = EvidenceStore(tmp_path / "evidence")

    outcome = _import(artifact, _fake_ledger(), tmp_path)
    bundle = store.read(outcome.evidence_sha256)

    assert bundle.provenance.registration_state is RegistrationState.LEGACY_UNPREGISTERED
    assert bundle.provenance.holdout_state is HoldoutState.CONTAMINATED
    assert bundle.provenance.dataset_sha256 is None
    assert bundle.return_series_basis is ReturnSeriesBasis.REALIZED_CLOSED_TRADES
    # PARTIAL, not COMPLETE: spread and slippage were charged inside the fill
    # prices, so the result cannot separate them and the bundle must not imply a
    # zero by declaring an attribution it does not have.
    assert bundle.costs.status is CostAttributionStatus.PARTIAL
    assert bundle.costs.spread_cost is None
    assert bundle.costs.slippage_cost is None
    # The brief's fixture zeroes both terms, so its totals are zero. The sums
    # themselves are pinned against a result that carries real ones below.
    assert bundle.costs.commission == Decimal("0")
    assert bundle.costs.swap == Decimal("0")
    # Registration is declared at import time, not backdated to the run.
    assert bundle.provenance.registered_at == _NOW
    assert bundle.provenance.occurred_at == _result_with_two_trades().end
    assert bundle.provenance.agent_run_id == "run-1"
    # The digest an operator reproduces with ``sha256sum``, over the artifact's
    # own bytes rather than a re-encoding of what was parsed out of them.
    assert (
        bundle.provenance.source_artifact_sha256
        == hashlib.sha256(artifact.read_bytes()).hexdigest()
    )


def test_cost_totals_are_the_signed_sums_of_every_trade(tmp_path: Path) -> None:
    """The aggregation this pins: 7 + 11 of commission, and -3 + 5 of swap.

    Swap is signed, and that is the term a careless sum gets wrong. The two
    trades carry opposite-signed swap on purpose, so a hardcoded zero, a
    first-trade-only total, and an unsigned accumulation (which would read 8
    rather than 2) are three different wrong answers and all three fail.
    """

    result = _result_with_costs()
    artifact = _write_outer_artifact(tmp_path, result)
    store = EvidenceStore(tmp_path / "evidence")

    outcome = _import(artifact, _fake_ledger(), tmp_path)
    bundle = store.read(outcome.evidence_sha256)

    assert [trade.commission for trade in result.trades] == [
        Decimal("7"),
        Decimal("11"),
    ]
    assert [trade.swap for trade in result.trades] == [Decimal("-3"), Decimal("5")]
    assert bundle.costs.commission == Decimal("18")
    assert bundle.costs.swap == Decimal("2")
    # PARTIAL still: the terms that *were* attributable are attributed, and the
    # two that were folded into the fill prices stay unknown.
    assert bundle.costs.status is CostAttributionStatus.PARTIAL
    assert bundle.costs.spread_cost is None
    assert bundle.costs.slippage_cost is None


def test_legacy_bundle_carries_the_derived_realized_series(tmp_path: Path) -> None:
    artifact = _write_outer_artifact(tmp_path, _result_with_two_trades())
    result = _result_with_two_trades()
    store = EvidenceStore(tmp_path / "evidence")

    outcome = _import(artifact, _fake_ledger(), tmp_path)
    bundle = store.read(outcome.evidence_sha256)

    assert bundle.source_result_sha256 == result.digest()
    assert outcome.source_result_sha256 == result.digest()
    assert bundle.result == result
    assert bundle.daily_returns == derive_realized_daily_returns(result)


def test_legacy_import_appends_one_legacy_event_and_no_preregistration(
    tmp_path: Path,
) -> None:
    """Section 5.7: the import never synthesises a registration, and section 5.6
    wants the event counted in all three deflation denominators."""

    artifact = _write_outer_artifact(tmp_path, _result_with_two_trades())
    ledger = _fake_ledger()

    outcome = _import(artifact, ledger, tmp_path)

    assert ledger.append_count == 1
    event = ledger.events[0]
    assert event.event_type is LedgerEventType.LEGACY_IMPORTED
    assert event.scope_kind is ScopeKind.TRIAL
    assert event.scope_id == outcome.trial_id
    assert event.trial_id == outcome.trial_id
    assert event.attempt_id == outcome.attempt_id
    assert event.legacy is True
    assert event.legacy_reason is not None
    assert LedgerEventType.PREREGISTERED not in {appended.event_type for appended in ledger.events}
    assert isinstance(event.payload, LegacyImportedPayload)
    assert event.spec_sha256 == canonical_sha256(event.payload.trial)
    # The reason is on the event *and* inside the payload: the column is the
    # queryable projection, the payload is the sealed bytes.
    assert event.legacy_reason == "Phase 7 result imported after the concrete ledger existed"
    assert event.payload.legacy_reason == event.legacy_reason
    assert event.payload.evidence_sha256 == outcome.evidence_sha256
    assert event.payload.source_result_sha256 == outcome.source_result_sha256
    assert event.payload.trial == TrialSpec(
        trial_id=outcome.trial_id,
        spec_id="session-momentum-legacy",
        rationale="Phase 7 result imported after the concrete ledger existed",
        parameter_space=(),
    )
    assert trial_counters(ledger.events) == TrialCounters(
        audit_attempts=1,
        selection_lotteries=1,
        effective_specifications=1,
    )


def test_two_artifacts_import_as_two_independent_trials(tmp_path: Path) -> None:
    """Idempotency is keyed on the source result digest, not on the file: a
    second, genuinely different Phase 7 result is a second trial."""

    first = _write_outer_artifact(tmp_path, _result_with_two_trades())
    other_result = _result_with_two_trades().model_copy(update={"run_id": "run-2"})
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second = _write_outer_artifact(second_dir, other_result)
    ledger = _fake_ledger()

    first_outcome = _import(first, ledger, tmp_path)
    second_outcome = _import(second, ledger, tmp_path)

    assert first_outcome.trial_id != second_outcome.trial_id
    assert second_outcome.already_present is False
    assert ledger.append_count == 2
