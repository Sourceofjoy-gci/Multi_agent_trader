"""The evidence bundle, and the store that keeps exactly the bytes it names.

An ``EvidenceBundle`` is the artifact a trial is judged on: the backtest result
it came from, the return series and cost attribution derived from that result,
and the provenance that says when and under which registration state it was
produced. It is serialized by ``research.canonical`` -- sorted keys, compact
separators, no NaN -- and addressed by ``canonical_sha256``, which prefixes the
research domain separator. The address is therefore *not* a bare SHA-256 of the
file, and ``read`` re-derives it from the bytes rather than from a parsed model
so verification never needs a parse to succeed before it can fail.

Three rules make the store worth trusting, and each is a place a shorter
implementation would quietly break them:

1. **A file appears complete or not at all.** Bytes are written to a temporary
   sibling, fsynced, and published with ``os.link``. A hard link is the only
   publish available that is atomic *and* refuses to clobber: ``os.replace``
   would silently overwrite on POSIX, and writing the target in place would
   expose a truncated document to any concurrent reader.
2. **An existing digest is never overwritten with different bytes.** The same
   ``os.link`` call enforces this, so the check and the publish are one step
   and there is no window in which two writers can both believe they won. Two
   writers producing the same bytes both succeed; two producing different
   documents at one digest is a conflict, and it raises rather than resolving.
3. **Failures are one error.** Missing, altered, unparseable, and non-canonical
   content all raise ``EvidenceIntegrityError``, so a caller has exactly one
   thing to catch. A bare ``OSError`` escaping this module would tell a caller
   about a filesystem without telling it that the evidence is unusable.

The store holds evidence, not the trial ledger: PostgreSQL owns registration
order, and a bundle's ``provenance.registered_at`` is operator-declared. The
ledger event's server-computed ``recorded_at`` is the only registration-order
authority, which is why the two timestamps are merely required to be UTC here
and never compared to each other.
"""

import hashlib
import os
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, NonNegativeInt, PositiveInt, field_validator, model_validator

from trading_house.core.clock import ensure_utc
from trading_house.core.errors import EvidenceIntegrityError, TimestampError
from trading_house.core.values import CanonicalModel, NonEmptyStr, NonNegativeDecimal

# Re-exported, not defined here: the type now lives in research/backtest/mark.py
# because BACKTEST_ALLOWED admits trading_house.research.backtest and not
# trading_house.research, so the direction of the dependency had to invert. The
# redundant alias is PEP 484's explicit re-export marker, which strict mypy's
# no_implicit_reexport requires and this module's many importers depend on. The
# serialized shape is byte-identical, so no digest moves. ``EquitySeries`` is
# imported from the same place for the same reason and is *not* a re-export: the
# bundle carries it, and a new key in a sealed document is a schema change that
# has to be declared rather than absorbed. ``CostAttribution`` comes down the
# same way, with the one addition that this module *calls* the rule rather than
# restating it: see ``the_summary_is_the_attribution_it_aggregates``.
from trading_house.research.backtest.costs_attribution import (
    CostAttribution,
    attribution_disagreement,
)
from trading_house.research.backtest.liquidity import Liquidity
from trading_house.research.backtest.mark import DailyReturnPoint as DailyReturnPoint
from trading_house.research.backtest.mark import EquitySeries
from trading_house.research.backtest.result import BacktestResult
from trading_house.research.backtest.sizing import SizingMode, is_constant_notional
from trading_house.research.canonical import DOMAIN_SEPARATOR, canonical_bytes, canonical_sha256
from trading_house.research.promotion import ValidationReport
from trading_house.research.trial_ledger import (
    CostAttributionStatus,
    HoldoutState,
    RegistrationState,
    ReturnSeriesBasis,
)

_HEX_DIGEST_LENGTH = 64
_HEX_DIGEST_ALPHABET = frozenset("0123456789abcdef")


def _is_digest(value: str) -> bool:
    return len(value) == _HEX_DIGEST_LENGTH and _HEX_DIGEST_ALPHABET.issuperset(value)


def _is_absent(value: object) -> bool:
    """``Field(exclude_if=...)``: leave ``None`` out of the serialized bytes.

    Named rather than inlined as a lambda so the field that depends on it can
    say what it does, and so mypy has a type to check the call against.
    """

    return value is None


def _utc(value: datetime) -> datetime:
    """``trial_ledger``'s own timestamp helper, mirrored rather than imported.

    Private there, and importing a private name across modules is how a second
    copy of a rule becomes two rules.
    """

    try:
        return ensure_utc(value)
    except TimestampError as error:
        raise ValueError(str(error)) from error


class CostSummary(CanonicalModel):
    """Cost attribution for one result, and how much of it is real.

    ``status`` is load-bearing rather than decorative, and it licenses exactly
    the components the summary carries. Spread and slippage are charged inside
    the fill prices, so a Phase 7 artifact cannot separate them from
    ``gross_pnl``; ``PARTIAL`` with two ``None`` components is the honest
    record of that, and ``UNAVAILABLE`` says there is no attribution at all.

    The coupling is **symmetric**, and the second direction is the one a bundle
    is received rather than built under. A ``COMPLETE`` summary that omits a
    component is refused because a promotion gate reading ``COMPLETE`` cannot
    tell an omitted term from a zero one. But a ``PARTIAL`` or ``UNAVAILABLE``
    summary carrying ``0`` in either component is refused for the same reason
    from the other side: those two statuses say the terms are *unknown*, and a
    zero is a measured value, not an unknown one. The design's "the legacy
    adapter must not write either unknown as zero holds by construction rather
    than by vigilance" is true of ``legacy_import.py`` and false of this model --
    a bundle is a document an operator can hand to ``research trial record``, so
    what protects the claim is this validator, not the producer's discipline.

    The two bounds are the per-trade split's own, and this summary is a *checked*
    aggregate of it: a spread or slippage figure here is a sum of
    ``TradeCostAttribution`` fields that cannot be negative, and a negative
    aggregate would mean a run was *paid* spread inside the very artifact that
    exists to say what it paid. ``commission`` and ``swap`` are left bare
    ``Decimal`` because they aggregate fields that are themselves signed
    (``SimulatedTrade.commission`` and ``.swap`` are not bounded by that model),
    and a Phase 7 bundle's totals are sealed from real trade data whose aggregate
    nothing here can re-derive. ``swap`` is genuinely signed -- a negative is a
    charge and a positive a credit -- so bounding it would be simply wrong.
    """

    status: CostAttributionStatus
    commission: Decimal
    swap: Decimal
    spread_cost: NonNegativeDecimal | None
    """``None`` when nothing measured it, and never negative when something did:
    a cost that was charged is not a credit."""
    slippage_cost: NonNegativeDecimal | None
    """As ``spread_cost`` -- the other half of the same decomposition, and the
    same reason a compensating pair could not pass here either."""

    @model_validator(mode="after")
    def the_status_licenses_exactly_the_components_it_carries(self) -> Self:
        """Both directions, because the coupling is symmetric.

        The name says what the rule is rather than which end of it this method
        happens to implement, so a later reader adding a third status is told to
        extend both branches rather than the one they can see.
        """

        if self.status is CostAttributionStatus.COMPLETE:
            if self.spread_cost is None or self.slippage_cost is None:
                raise ValueError("a complete cost summary has every component attributed")
            return self
        if self.spread_cost is not None or self.slippage_cost is not None:
            raise ValueError("an incomplete cost summary must attribute neither component")
        return self


class EvidenceProvenance(CanonicalModel):
    """Where this bundle came from, and when -- as declared, not as verified.

    ``registered_at`` is what the operator says they registered; it is not
    evidence of registration order, because a declared timestamp can be typed
    in after the fact. The ledger's own ``recorded_at`` is that authority. Both
    timestamps are still held to UTC so a cross-timezone comparison cannot be
    got wrong later.
    """

    agent_run_id: NonEmptyStr
    source_artifact_sha256: NonEmptyStr
    dataset_sha256: NonEmptyStr | None
    registered_at: datetime
    occurred_at: datetime
    registration_state: RegistrationState
    holdout_state: HoldoutState

    @field_validator("registered_at", "occurred_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


class EvidenceBundle(CanonicalModel):
    """One attempt's complete evidence, and the unit the store addresses.

    Carries the result inline rather than by reference: the ledger holds a
    digest, and a bundle that could not be reassembled from its own fields
    would make that digest a promise the file cannot keep.
    """

    bundle_schema_version: Literal[1] = 1
    result_schema_version: PositiveInt
    trial_id: NonEmptyStr
    attempt_id: NonEmptyStr
    spec_sha256: NonEmptyStr
    source_result_sha256: NonEmptyStr
    result: BacktestResult
    daily_returns: tuple[DailyReturnPoint, ...]
    return_series_basis: ReturnSeriesBasis
    costs: CostSummary
    provenance: EvidenceProvenance
    # The per-bar series ``daily_returns`` was reduced from, sealed whole. The
    # daily series is a *reduction*, and a reduction whose input is not retained
    # cannot be re-derived, re-audited, or checked against a later
    # ``BacktestOutcome`` -- so without this field the engine computes an equity
    # path and the evidence store keeps only its arithmetic.
    #
    # ``exclude_if`` is load-bearing, not cosmetic, and a plain ``= None`` would
    # be a real regression. ``EvidenceStore.read`` re-serializes what it decoded
    # and refuses any document whose bytes are not today's canonical encoding, so
    # a field that always wrote itself out would put ``"mark_to_market":null``
    # into every bundle -- and every v1 document already sealed in an operator's
    # store, three of which exist on no machine and cannot be regenerated, would
    # then fail that check. ``research trial verify`` would start failing on a
    # chain it sealed and verified itself. Omitting the key when it is absent
    # keeps a v1 document's bytes exactly what they were, so the extension is
    # invisible to it; the pinned known-answer bundle digest does not move, and
    # that is the property the exclusion buys rather than a consequence of it.
    #
    # Absent therefore means "this bundle has no mark-to-market series", and the
    # validator below is what makes that unambiguous rather than a hole. It does
    # mean a document that spells the key out as ``null`` is not canonical and
    # will not verify -- a shape nothing in this repo can produce, since every
    # write goes through ``canonical_bytes``.
    mark_to_market: EquitySeries | None = Field(default=None, exclude_if=_is_absent)
    # The same decision a second time, for the same reason: ``costs`` above is a
    # *sum* of the per-trade split sealed here, and a total whose inputs the
    # store does not hold cannot be re-derived or re-audited -- it can only be
    # believed. ``exclude_if`` is load-bearing here for exactly the reason it is
    # on ``mark_to_market``: without it a ``PARTIAL`` bundle would encode
    # ``"cost_attribution":null``, which moves the pinned v1 bundle digest and
    # makes ``EvidenceStore.read`` refuse every v1 document already sealed in an
    # operator's store, three of which exist on no machine and cannot be
    # regenerated. The rationale is stated in full on the field above rather than
    # restated; a second copy of the argument is a second thing to drift.
    #
    # Absent therefore means "this bundle has no per-trade split", and the
    # coupling validator below is what makes that unambiguous rather than a
    # hole.
    cost_attribution: CostAttribution | None = Field(default=None, exclude_if=_is_absent)
    # How the run sized its positions (Phase 8B3, spec 4.2). ``exclude_if`` is
    # load-bearing for the reason stated on ``mark_to_market``: a constant-
    # notional bundle omits the key, so every bundle sealed before this field
    # existed keeps its bytes and the pinned v1 digest does not move. Absent
    # therefore means constant notional.
    sizing: SizingMode = Field(
        default=SizingMode.CONSTANT_NOTIONAL, exclude_if=is_constant_notional
    )
    # What the market offered at each fill (Phase 12), sealed when the run's protocol
    # declares a capacity model to read it. ``exclude_if`` for the reason given on
    # ``mark_to_market``: every bundle sealed without it keeps its bytes. Absent means the
    # bundle cannot answer a capacity question, which the capacity measurement says.
    liquidity: Liquidity | None = Field(default=None, exclude_if=_is_absent)

    @model_validator(mode="after")
    def the_liquidity_is_the_trades(self) -> Self:
        """One record per trade, in order: a capacity figure over other trades is nobody's."""

        if self.liquidity is None:
            return self
        if [item.proposal_id for item in self.liquidity.trades] != [
            trade.proposal_id for trade in self.result.trades
        ]:
            raise ValueError("liquidity must be recorded for exactly the result's trades")
        return self

    @model_validator(mode="after")
    def a_compounding_bundle_is_mark_to_market(self) -> Self:
        """Compounding is defined over the per-bar equity path, so it needs the series."""

        if (
            self.sizing is SizingMode.COMPOUNDING
            and self.return_series_basis is not ReturnSeriesBasis.MARK_TO_MARKET
        ):
            raise ValueError("a compounding bundle must be on the mark-to-market basis")
        return self

    @model_validator(mode="after")
    def source_digest_is_the_result_it_carries(self) -> Self:
        if self.source_result_sha256 != self.result.digest():
            raise ValueError("source_result_sha256 must equal the digest of the carried result")
        return self

    @model_validator(mode="after")
    def the_attribution_is_present_exactly_where_the_summary_claims_one(self) -> Self:
        """A ``COMPLETE`` summary and a per-trade attribution are the same claim.

        ``PARTIAL`` with two ``None`` components is the honest record of a run
        that could not separate spread from slippage -- which is every Phase 7
        artifact, whose bar store was deleted. A ``COMPLETE`` summary with no
        attribution behind it asserts a breakdown it does not carry, and an
        attribution beside a ``PARTIAL`` summary claims a completeness the summary
        denies. Both directions fail closed, for the reason the basis/series
        coupling below does: a document whose two halves disagree is not a
        document anybody can check.
        """

        complete = self.costs.status is CostAttributionStatus.COMPLETE
        if complete and self.cost_attribution is None:
            raise ValueError("a complete cost summary must carry the attribution it aggregates")
        if not complete and self.cost_attribution is not None:
            raise ValueError("only a complete cost summary may carry a per-trade attribution")
        return self

    @model_validator(mode="after")
    def the_summary_is_the_attribution_it_aggregates(self) -> Self:
        """The totals are checked against the detail rather than trusted beside it.

        The per-trade checks are NOT restated here. Task 2 left a predicate,
        ``attribution_disagreement(attribution, result)``, in
        ``research/backtest/costs_attribution.py`` which returns the refusal
        reason or ``None``, and ``BacktestOutcome`` calls it. This bundle calls
        the same one: the two surfaces exist to catch the same defect in the same
        place, and duplicated string literals drift the moment one is edited and
        the other is not. ``evidence.py`` may import from ``research.backtest`` --
        it already does for ``BacktestResult`` and for the series -- so the import
        is available; the constraint runs the other way, and a module inside
        ``research/backtest/`` may not import from ``research/``.

        The four sums are this module's own because the predicate does not know
        about a summary: it binds a split to a result, and the summary is a
        bundle-level object. They are the reason ``costs`` stopped being an
        independently asserted total.

        The four are split across the two halves of this method because they
        come from two different places, and only two of them depend on the
        attribution. ``commission`` and ``swap`` are sums over ``result.trades``,
        which every bundle carries in full -- including every Phase 7 legacy
        artifact, which is whole precisely because the store keeps the result
        inline -- so they are checkable unconditionally and sit above the early
        return. ``spread_cost`` and ``slippage_cost`` are sums over
        ``cost_attribution.trades`` and exist only when there is a split to sum,
        which is what puts them below it. A ``PARTIAL`` summary is the honest
        record of a run that could not separate spread from slippage, and that
        inability is a statement about those two terms alone: commission and
        swap were never in doubt, and a legacy bundle claiming a total its own
        trades do not produce is refused like any other.
        """

        if self.costs.commission != sum(
            (trade.commission for trade in self.result.trades), Decimal(0)
        ):
            raise ValueError("the summary's commission must equal the sum of the trades'")
        if self.costs.swap != sum((trade.swap for trade in self.result.trades), Decimal(0)):
            raise ValueError("the summary's swap must equal the sum of the trades'")
        if self.cost_attribution is None:
            return self
        disagreement = attribution_disagreement(self.cost_attribution, self.result)
        if disagreement is not None:
            raise ValueError(disagreement)
        splits = self.cost_attribution.trades
        if sum((split.spread_cost for split in splits), Decimal(0)) != self.costs.spread_cost:
            raise ValueError("the summary's spread cost must equal the sum of the splits'")
        if sum((split.slippage_cost for split in splits), Decimal(0)) != self.costs.slippage_cost:
            raise ValueError("the summary's slippage cost must equal the sum of the splits'")
        return self

    @model_validator(mode="after")
    def the_series_is_present_exactly_where_the_basis_claims_one(self) -> Self:
        """The basis names the series, so the two cannot disagree.

        Both directions fail closed. A ``MARK_TO_MARKET`` bundle with no series
        claims a return series it does not carry; a bundle on any other basis
        carrying one would claim two different returns for a single run, with
        nothing to tell a reader which of them a downstream number used.
        """

        marked = self.return_series_basis is ReturnSeriesBasis.MARK_TO_MARKET
        if marked and self.mark_to_market is None:
            raise ValueError("a mark-to-market bundle must carry the series it reduced")
        if not marked and self.mark_to_market is not None:
            raise ValueError("only a mark-to-market bundle may carry a per-bar equity series")
        return self

    @model_validator(mode="after")
    def the_sealed_series_is_the_result_it_carries(self) -> Self:
        """The three equalities ``BacktestOutcome`` asserts, on the same pair of objects.

        A bundle may hold a series that satisfies every rule about itself and
        still be describing a different run: another run's series, or a truncated
        one, passes the coupling rule above, and the reduction of an equity path
        this result did not produce is still a reduction. The pair is checked here
        rather than left to ``BacktestOutcome`` because the bundle outlives it --
        the outcome is a command's memory and this is what a later reader is
        handed, sealed and re-read years later. All three refusals are worded as
        ``research/backtest/mark.py`` words them, so a disagreement is reported
        the same way whether it was caught at construction or at read time.

        The third is conditional on ``is_flat`` there too, and for the same
        reason: when the engine discarded a position the range ran out on, the
        final cumulative total legitimately excludes that mark and so does
        ``net_pnl``. Nothing is forced flat to make it pass, which is why a
        non-flat bundle still builds here -- it is honest evidence of a defect a
        later gate refuses, and refusing it here would only hide the defect.
        """

        if self.mark_to_market is None:
            return self
        if self.mark_to_market.firm_equity != self.result.firm_equity:
            raise ValueError("the series and the result must share one firm equity")
        if len(self.mark_to_market.observations) != self.result.bars_seen:
            raise ValueError("the series must hold one observation per processed bar")
        if self.mark_to_market.is_flat:
            final = self.mark_to_market.observations[-1].cumulative_realized_pnl
            if final != self.result.net_pnl:
                raise ValueError(
                    "a flat run's final realized total must equal the result's net PnL"
                )
        return self


class StoredEvidence(CanonicalModel):
    """What a write produced: the digest, the root-relative path, the size.

    The path is relative because the ledger stores it. An absolute path from
    the writing machine is not evidence, it is a machine name.
    """

    sha256: str
    path: str
    size_bytes: NonNegativeInt


class EvidenceStore:
    """Content-addressed storage for evidence bundles, keyed by their digest.

    Holds no index. The path is the index -- ``<root>/<first two hex>/<digest>``,
    the layout every CAS uses -- so there is no second copy of the truth that
    could disagree with the files, and a store that is copied or rsynced is
    still verifiable without a repair step.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, digest: str) -> Path:
        # A digest arrives from the ledger, so it is checked before it is used
        # to build a path: without this, a corrupted value walks out of the
        # evidence root through ``digest[:2]`` and reads a file that was never
        # sealed as evidence.
        if not _is_digest(digest):
            raise EvidenceIntegrityError()
        return self.root / digest[:2] / f"{digest}.json"

    def write(self, bundle: EvidenceBundle) -> StoredEvidence:
        return self._publish(canonical_bytes(bundle), canonical_sha256(bundle))

    def write_report(self, report: ValidationReport) -> StoredEvidence:
        """Seal a validation report: a second document kind, in this store and no other.

        Same root, same layout, same atomic link-publish and the same domain-separated
        digest as a bundle. Nothing in the path says which kind a file is; ``read`` and
        ``read_report`` each refuse the other's documents, because a digest that named the
        wrong kind of document would verify and mean something else.
        """

        return self._publish(canonical_bytes(report), canonical_sha256(report))

    def _publish(self, data: bytes, digest: str) -> StoredEvidence:
        target = self._path(digest)

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".evidence-", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    # Published by someone else. Identical bytes are a
                    # successful retry; different bytes at our own digest mean
                    # the address is ambiguous, and picking a winner is exactly
                    # the decision a content-addressed store must not make.
                    if target.read_bytes() != data:
                        raise EvidenceIntegrityError() from None
                # ponytail: the file's bytes are fsynced but its directory entry
                # is not, so a crash here can lose the link. The file is never
                # partial either way; fsync the parent dir if losing the address
                # to power loss stops being acceptable (POSIX only).
            finally:
                Path(temporary).unlink(missing_ok=True)
        except OSError as error:
            # The filesystem's own message names the path and the reason; it
            # belongs on the private cause, not in the public message.
            raise EvidenceIntegrityError() from error

        return StoredEvidence(
            sha256=digest,
            path=target.relative_to(self.root).as_posix(),
            size_bytes=len(data),
        )

    def _read_bytes(self, digest: str) -> bytes:
        target = self._path(digest)
        try:
            data = target.read_bytes()
        except OSError as error:
            # Missing, unreadable, or a directory at the path: none of those is
            # evidence, and none of them should reach a caller as an OSError.
            raise EvidenceIntegrityError() from error

        # The formula canonical_sha256() applies, computed over the bytes alone:
        # whether the file is the one its digest names must not depend on it
        # parsing first.
        if hashlib.sha256(DOMAIN_SEPARATOR + data).hexdigest() != digest:
            raise EvidenceIntegrityError()
        return data

    def read(self, digest: str) -> EvidenceBundle:
        data = self._read_bytes(digest)
        try:
            bundle = EvidenceBundle.model_validate_json(data)
        except (ValueError, TypeError) as error:
            raise EvidenceIntegrityError() from error

        # A matching digest is not enough. The bytes must also be the canonical
        # encoding of what they decode to, or the same evidence has more than
        # one valid representation and only the one that happens to be stored
        # would verify.
        if canonical_bytes(bundle) != data:
            raise EvidenceIntegrityError()
        return bundle

    def read_report(self, digest: str) -> ValidationReport:
        """The report a digest names, or ``EvidenceIntegrityError`` for anything else.

        A bundle's digest is refused here as a report's is refused by ``read``: both are
        strict documents with no field in common, so the wrong kind never parses.
        """

        data = self._read_bytes(digest)
        try:
            report = ValidationReport.model_validate_json(data)
        except (ValueError, TypeError) as error:
            raise EvidenceIntegrityError() from error
        if canonical_bytes(report) != data:
            raise EvidenceIntegrityError()
        return report

    def verify(self, digest: str) -> None:
        self.read(digest)
