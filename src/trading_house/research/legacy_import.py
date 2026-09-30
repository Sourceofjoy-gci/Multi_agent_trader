"""Importing a Phase 7 backtest as the legacy evidence it is.

A Phase 7 artifact is a backtest result that was produced before the trial
ledger existed, so importing it is a claim about provenance as much as a
conversion. Four rules make the claim honest, and each is a place a shorter
importer would quietly break it:

1. **The artifact's own digest decides whether it is read at all.** The
   document's ``digest`` field is compared against a re-derivation from the
   result it carries, not accepted because it is well formed: an artifact whose
   result was edited after the run still carries 64 hex characters, and section
   12 of the design stops the migration on exactly that.
2. **The import never claims to be a registration.** The event is
   ``LEGACY_IMPORTED`` and is flagged ``legacy``; no ``PREREGISTERED`` event is
   ever synthesised, and ``registered_at`` is the import clock rather than the
   run's own timestamps. Backdating a registration is the false provenance
   decision D-10 exists to prevent, and a date an operator typed in after the
   fact cannot order anything anyway.
3. **Absences are recorded as absences.** ``LEGACY_UNPREGISTERED``,
   ``CONTAMINATED``, no dataset hash, ``PARTIAL`` cost attribution with spread
   and slippage left unknown. Spread and slippage were charged inside the fill
   prices, so the result cannot separate them; a zero there would be a number
   nobody measured.
4. **The return series says what it is.** ``REALIZED_CLOSED_TRADES`` over every
   UTC calendar day in the run's window, and never called a mark-to-market
   series: a day with no closed trade carries a return of exactly zero rather
   than no point at all, because a calendar-day series cannot leave a day out.
   Reading that flat zero as a day the account was marked on is the mistake the
   ``ReturnSeriesBasis`` field exists to stop.

Idempotency is by source result digest. The event id is a UUID5 over that
digest, so a second import of the same artifact recognises its own event and
reports the evidence digest *the chain recorded* rather than re-deriving one --
``now`` is operator-declared, and a retry under a later clock must not report a
document the ledger has never seen.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from trading_house.core.errors import EvidenceIntegrityError
from trading_house.core.values import CanonicalModel, NonEmptyStr
from trading_house.research.backtest.result import BacktestResult
from trading_house.research.canonical import canonical_sha256
from trading_house.research.evidence import (
    CostSummary,
    DailyReturnPoint,
    EvidenceBundle,
    EvidenceProvenance,
    EvidenceStore,
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
    TrialLedger,
    TrialSpec,
)

_LEGACY_SPEC_ID = "session-momentum-legacy"
_LEGACY_RATIONALE = "Phase 7 result imported after the concrete ledger existed"
# ``BacktestResult`` carries no schema version of its own, and 1 is the only
# honest value: it is the first version of the result schema this bundle can
# describe. Guessing ahead of it would make an unverifiable claim.
_RESULT_SCHEMA_VERSION = 1


class LegacyImportResult(CanonicalModel):
    """What one import did, and where its evidence ended up.

    ``already_present`` distinguishes "I imported this" from "this was already
    imported and here is the same answer". A caller that cannot tell those apart
    has to guess whether its own write happened, and a guessed retry is how a
    ledger grows a second copy of a trial.
    """

    trial_id: NonEmptyStr
    attempt_id: NonEmptyStr
    source_result_sha256: NonEmptyStr
    evidence_sha256: NonEmptyStr
    already_present: bool


def derive_realized_daily_returns(result: BacktestResult) -> tuple[DailyReturnPoint, ...]:
    """Realized closed-trade returns, one point per UTC calendar day.

    Phase 7 artifacts are constant-notional (D-4), so the running equity *is* the
    running sum of ``net_pnl`` and a day's return is that day's closed P&L over the equity it
    started from. A day with no closed trade carries its own pnl of zero, which
    is a real zero return rather than a missing one -- the reason the series can
    be a run of calendar days instead of a run of trade exits.

    This is not a mark-to-market series and does not claim to be: open
    positions are invisible here, and pretending otherwise is what the
    ``ReturnSeriesBasis`` field exists to prevent. Spread and slippage are not
    inferred either; they were charged inside the fill prices.

    The walk runs to ``max(end.date(), every exit day)``, not to
    ``end.date()``. ``BacktestResult.end`` is ``request.end``, and
    ``Backtester._replay_bars`` asks the store for one bar more than that:
    ``request.start``/``end`` are inclusive bar open times while the store's range
    is half-open, so it moves the end forward a bar's duration
    (``horizon = request.end + duration(timeframe)``). A position opened on the
    final bar of the request is therefore closed on that extra bar, and its
    ``exit_at`` is legitimately after ``result.end``. Stopping at ``end.date()``
    would drop that P&L from the series while ``net_pnl`` still counted it -- a
    series that does not reconcile with the result printed beside it, and one no
    model above would notice, because the series is an opaque tuple. Extending
    the walk keeps the whole point in the series and keeps the artifact
    importable, which refusing it would not.

    An exit *before* ``start`` is the opposite and is refused. There is no
    legitimate route to one: a position cannot be opened on a bar the run never
    read, and its day precedes the first day the walk visits, so folding it in
    would attribute P&L to a day that is not in the series at all. Raising here
    is also what refuses the import, because the bundle cannot be built without
    this series; the rule lives in one place so it cannot be enforced on one path
    and not the other.
    """

    by_day: dict[date, Decimal] = {}
    for trade in result.trades:
        if trade.exit_at < result.start:
            raise EvidenceIntegrityError()
        by_day[trade.exit_at.date()] = by_day.get(trade.exit_at.date(), Decimal(0)) + trade.net_pnl

    equity = result.firm_equity
    previous = equity
    points: list[DailyReturnPoint] = []
    day = result.start.date()
    last = max([result.end.date(), *by_day])
    while day <= last:
        equity += by_day.get(day, Decimal(0))
        if previous <= 0:
            # A return over a non-positive base is not a number. Emitting one
            # would let a wiped-out account report a spectacular daily series.
            raise EvidenceIntegrityError()
        points.append(DailyReturnPoint(day=day, value=(equity - previous) / previous))
        previous = equity
        day += timedelta(days=1)
    if previous <= 0:
        # The final day took the account to zero or below and the walk has run
        # out of days to notice it on. Same refusal as the in-loop check, and
        # for the same reason: the next day's divisor would be this equity.
        raise EvidenceIntegrityError()
    return tuple(points)


def _verified_artifact(path: Path) -> tuple[BacktestResult, str]:
    """The result a Phase 7 document carries, and the digest of the file itself.

    Failures are one error: a missing file, a truncated one, a failed run, an
    unedited-but-unparseable result and a doctored digest are all
    ``EvidenceIntegrityError``, so a caller migrating a directory of artifacts
    has exactly one thing to catch. The artifact digest is a bare SHA-256 of the
    bytes rather than a domain-separated one, because it names a file an
    operator can check with ``sha256sum``.
    """

    try:
        data = path.read_bytes()
    except OSError as error:
        raise EvidenceIntegrityError() from error
    try:
        payload = json.loads(data)
    except ValueError as error:
        raise EvidenceIntegrityError() from error
    # A failed Phase 7 run writes ``{"status": "error", ...}`` with no result at
    # all. Importing that would seal an absence as evidence.
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise EvidenceIntegrityError()

    # ``margin_modelled`` sits beside the result in the document and is not
    # read. EvidenceBundle has no field for it, and the pinned artifact bytes
    # retain it, so nothing is lost by leaving it here.
    try:
        # The JSON path rather than model_validate: every research contract is
        # strict, so the dict of JSON strings an artifact is made of would have
        # its timestamps, decimals and UUIDs refused.
        result = BacktestResult.model_validate_json(json.dumps(payload["result"]))
    except (KeyError, TypeError, ValueError) as error:
        raise EvidenceIntegrityError() from error
    if payload.get("digest") != result.digest():
        raise EvidenceIntegrityError()
    return result, hashlib.sha256(data).hexdigest()


def _recorded_event(entry: LedgerEvent | LedgerRecord) -> LedgerEvent:
    """The event behind one chain row.

    The ledger's ``events_for`` returns ``LedgerRecord`` and a test double
    returns ``LedgerEvent``; only the second has a parsed payload. Both carry the
    same jsonb, and re-parsing it through the JSON path is what ``ledger_store``
    does for the same reason -- a strict model will not accept the strings that
    column holds.
    """

    if isinstance(entry, LedgerEvent):
        return entry
    try:
        return LedgerEvent.model_validate_json(json.dumps(entry.event_json, sort_keys=True))
    except (TypeError, ValueError) as error:
        raise EvidenceIntegrityError() from error


def _recorded_evidence_sha256(ledger: TrialLedger, trial_id: str, event_id: UUID) -> str | None:
    """The evidence digest this import already recorded, or ``None``.

    Read from the chain rather than recomputed, because the bundle is not a pure
    function of the artifact: ``registered_at`` is the operator's import clock,
    so a retry under a later clock would re-derive a document the ledger has
    never seen and report it as the recorded one.

    The digest is read out of the row's JSONB projection, and ``verify()`` is
    what keeps that projection honest: it recomputes the canonical bytes and
    fails with ``event_json_mismatch`` if the column and the chain's own bytes
    disagree. So the projection is safe to read, and a chain that has drifted is
    a report an operator can read rather than a silent wrong answer.
    """

    for entry in ledger.events_for(trial_id):
        if entry.event_id != event_id:
            continue
        event = _recorded_event(entry)
        if not isinstance(event.payload, LegacyImportedPayload):  # pragma: no cover
            # Narrowing rather than a reachable guard: ``LedgerEvent`` refuses a
            # payload whose event_type disagrees with its own, so a legacy-import
            # event can only carry this payload. Kept fail-closed anyway, because
            # the day that validator loosens, this is the line that notices.
            raise EvidenceIntegrityError()
        return event.payload.evidence_sha256
    return None


def import_phase7_artifact(
    path: Path,
    *,
    ledger: TrialLedger,
    evidence: EvidenceStore,
    now: datetime,
) -> LegacyImportResult:
    """Seal one preserved Phase 7 result as legacy evidence and record it once.

    The bundle is written before the event is appended, so an append that fails
    leaves evidence nobody points at rather than a ledger row whose document is
    missing -- and the reverse, a ledger row with no evidence, is the failure
    mode verification cannot report its way out of.

    ponytail: that ordering has a ceiling, and it costs a file rather than a
    trial. A crash between the write and the append leaves an orphan document
    that no ledger row references; a retry under a *later* clock builds a
    different bundle, so it succeeds, appends the same deterministic event id
    (nothing is in the way), and adds a second evidence file. One import, one
    event, one unreferenced file -- never a duplicate trial and never a
    collision. Reusing the same ``now`` across a retry is the mitigation: the
    bundle bytes come out identical, the write lands on the same digest, and
    ``EvidenceStore.write``'s identical-bytes path keeps it to one file. Task 5's
    CLI must therefore pass one ``now`` through a retry; if that stops being
    achievable, seal the bundle under an artifact-derived timestamp instead.
    """

    result, source_artifact_sha256 = _verified_artifact(path)
    source_result_sha256 = result.digest()
    trial_id = f"legacy-{source_result_sha256[:16]}"
    attempt_id = f"attempt-{source_result_sha256[:16]}"
    event_id = uuid5(NAMESPACE_URL, f"trading-house:legacy-import:{source_result_sha256}")

    already_recorded = _recorded_evidence_sha256(ledger, trial_id, event_id)
    if already_recorded is not None:
        return LegacyImportResult(
            trial_id=trial_id,
            attempt_id=attempt_id,
            source_result_sha256=source_result_sha256,
            evidence_sha256=already_recorded,
            already_present=True,
        )

    trial = TrialSpec(
        trial_id=trial_id,
        spec_id=_LEGACY_SPEC_ID,
        rationale=_LEGACY_RATIONALE,
        # Empty, and that is the record: the Phase 7 artifact never carried the
        # parameter values it ran with, and a plausible pair invented here would
        # be a fabricated specification.
        parameter_space=(),
    )
    # A TrialSpec, not a TrialProtocol. The ledger's ``register`` seals a
    # whole protocol and digests that, so its ``spec_sha256`` covers the data,
    # execution, cost, validation and holdout specs as well as the candidate
    # family. A legacy artifact has no such envelope -- there was no protocol --
    # so this digest covers the one candidate's declaration and nothing else,
    # and the two digests must never be compared as if they were the same kind
    # of address. ``TrialSpec`` embeds ``trial_id``, which is itself derived from
    # the source result digest, so no two legacy imports can share a spec digest
    # and each import counts as its own effective specification (design §5.6).
    spec_sha256 = canonical_sha256(trial)
    bundle = EvidenceBundle(
        result_schema_version=_RESULT_SCHEMA_VERSION,
        trial_id=trial_id,
        attempt_id=attempt_id,
        spec_sha256=spec_sha256,
        source_result_sha256=source_result_sha256,
        result=result,
        daily_returns=derive_realized_daily_returns(result),
        return_series_basis=ReturnSeriesBasis.REALIZED_CLOSED_TRADES,
        costs=CostSummary(
            status=CostAttributionStatus.PARTIAL,
            commission=sum((trade.commission for trade in result.trades), Decimal(0)),
            swap=sum((trade.swap for trade in result.trades), Decimal(0)),
            spread_cost=None,
            slippage_cost=None,
        ),
        provenance=EvidenceProvenance(
            agent_run_id=result.run_id,
            source_artifact_sha256=source_artifact_sha256,
            # Never invented: no dataset hash was recorded when the run
            # happened, and a reconstructed one would vouch for data nobody kept.
            dataset_sha256=None,
            registered_at=now,
            occurred_at=result.end,
            registration_state=RegistrationState.LEGACY_UNPREGISTERED,
            holdout_state=HoldoutState.CONTAMINATED,
        ),
    )
    stored = evidence.write(bundle)

    ledger.append(
        LedgerEvent(
            event_id=event_id,
            scope_kind=ScopeKind.TRIAL,
            scope_id=trial_id,
            event_type=LedgerEventType.LEGACY_IMPORTED,
            trial_id=trial_id,
            attempt_id=attempt_id,
            spec_sha256=spec_sha256,
            # The run's own end, not ``now``: it is the only occurrence time the
            # artifact supports, and a wall-clock stamp would make a retry's
            # canonical bytes differ from the original's.
            occurred_at=result.end,
            payload=LegacyImportedPayload(
                event_type=LedgerEventType.LEGACY_IMPORTED,
                trial=trial,
                evidence_sha256=stored.sha256,
                source_result_sha256=source_result_sha256,
                legacy_reason=_LEGACY_RATIONALE,
            ),
            legacy=True,
            legacy_reason=_LEGACY_RATIONALE,
        )
    )
    return LegacyImportResult(
        trial_id=trial_id,
        attempt_id=attempt_id,
        source_result_sha256=source_result_sha256,
        evidence_sha256=stored.sha256,
        already_present=False,
    )
