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

from pydantic import NonNegativeInt, PositiveInt, field_validator, model_validator

from trading_house.core.clock import ensure_utc
from trading_house.core.errors import EvidenceIntegrityError, TimestampError
from trading_house.core.values import CanonicalModel, NonEmptyStr

# Re-exported, not defined here: the type now lives in research/backtest/mark.py
# because BACKTEST_ALLOWED admits trading_house.research.backtest and not
# trading_house.research, so the direction of the dependency had to invert. The
# redundant alias is PEP 484's explicit re-export marker, which strict mypy's
# no_implicit_reexport requires and this module's many importers depend on. The
# serialized shape is byte-identical, so no digest moves.
from trading_house.research.backtest.mark import DailyReturnPoint as DailyReturnPoint
from trading_house.research.backtest.result import BacktestResult
from trading_house.research.canonical import DOMAIN_SEPARATOR, canonical_bytes, canonical_sha256
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

    ``status`` is load-bearing rather than decorative. Spread and slippage are
    charged inside the fill prices, so a Phase 7 artifact cannot separate them
    from ``gross_pnl``; ``PARTIAL`` with two ``None`` components is the honest
    record of that, and ``UNAVAILABLE`` says there is no attribution at all. A
    ``COMPLETE`` summary that still omits a component is refused rather than
    accepted, because a promotion gate reading ``COMPLETE`` cannot tell an
    omitted term from a zero one.
    """

    status: CostAttributionStatus
    commission: Decimal
    swap: Decimal
    spread_cost: Decimal | None
    slippage_cost: Decimal | None

    @model_validator(mode="after")
    def complete_attribution_carries_every_component(self) -> Self:
        if self.status is CostAttributionStatus.COMPLETE and (
            self.spread_cost is None or self.slippage_cost is None
        ):
            raise ValueError("a complete cost summary has every component attributed")
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

    @model_validator(mode="after")
    def source_digest_is_the_result_it_carries(self) -> Self:
        if self.source_result_sha256 != self.result.digest():
            raise ValueError("source_result_sha256 must equal the digest of the carried result")
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
        data = canonical_bytes(bundle)
        digest = canonical_sha256(bundle)
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

    def read(self, digest: str) -> EvidenceBundle:
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

    def verify(self, digest: str) -> None:
        self.read(digest)
