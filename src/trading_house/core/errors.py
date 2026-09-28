from collections.abc import Sequence
from enum import IntEnum
from typing import ClassVar


class ExitCode(IntEnum):
    OK = 0
    CONFIGURATION = 2
    SIGNATURE = 3
    DATABASE = 4
    MIGRATION = 5
    AUDIT_INTEGRITY = 6
    AUDIT_APPEND = 7
    BROKER = 8
    ACCOUNT_MODE = 9
    COVERAGE = 10
    INSUFFICIENT_HISTORY = 11
    DUPLICATE_INTENT = 12
    UNRESOLVED_INTENTS = 13
    CONCURRENT_SUBMISSION = 14
    TRIAL_LEDGER_APPEND = 15
    TRIAL_LEDGER_INTEGRITY = 16
    EVIDENCE_INTEGRITY = 17
    EQUITY_EVIDENCE = 18
    SCENARIO_EVIDENCE = 19


class TradingHouseError(Exception):
    """Base exception for domain-specific failures."""

    public_message: ClassVar[str] = "trading-house operation failed"

    def __init__(self) -> None:
        super().__init__(self.public_message)


class TimestampError(TradingHouseError):
    """Raised when a timestamp violates the UTC boundary."""

    public_message = "timestamp must be timezone-aware"


class ConfigurationError(TradingHouseError):
    """Raised when application configuration is invalid."""

    public_message = "configuration invalid"


class SignatureVerificationError(TradingHouseError):
    """Raised when signature verification fails."""

    public_message = "signature verification failed"


class SchemaValidationError(TradingHouseError):
    """Raised when schema validation fails."""

    public_message = "schema validation failed"


class DatabaseUnavailableError(TradingHouseError):
    """Raised when the database cannot be reached."""

    public_message = "database connection failed"


class MigrationMismatchError(TradingHouseError):
    """Raised when the database migration state is incompatible."""

    public_message = "migration revision mismatch"


class AuditAppendError(TradingHouseError):
    """Raised when an audit record cannot be appended."""

    public_message = "audit append failed"


class AuditIntegrityError(TradingHouseError):
    """Raised when audit-log integrity validation fails."""

    public_message = "audit integrity verification failed"


class EvidenceIntegrityError(TradingHouseError):
    """Raised when stored research evidence is missing, altered, or not the
    canonical bytes its digest names.

    Deliberately uninformative. The public message says only that verification
    failed; the ``OSError`` or parse failure that caused it stays on the private
    ``__cause__`` for an operator reading a log, because the filesystem path that
    failed is not something to echo back out of a failed seal.
    """

    public_message = "evidence integrity verification failed"


class EquityEvidenceError(TradingHouseError):
    """Raised when mark-to-market equity evidence cannot be produced or reduced.

    Three ways, all of them refusals of a *whole run* rather than of a malformed
    document: a run whose observation count exceeds the storage ceiling, a
    requested daily range whose first day is after its last, and a day whose
    prior close is not strictly positive to divide by.

    Deliberately NOT raised for a series that violates its own identity or its
    cross-checks against the result. Those are pydantic ``ValidationError``s, by
    design, and a caller that reached for this class to mean "equity evidence I
    cannot trust" would miss every one of them -- the two are different failure
    modes with different remedies, and conflating them here would send a
    document bug to the same exit code as a disk limit.
    """

    public_message = "mark-to-market equity evidence is not trustworthy"


class ScenarioEvidenceError(TradingHouseError):
    """Raised when a candidate's sealed scenarios are not the ones it declared.

    Six distinct ways that happens — a level missing or duplicated, a summary
    that is not ``COMPLETE`` or carries no split, a baseline that is not the
    declared one, a stressed level that changed something other than the
    multiplier, a scenario belonging to another candidate, and a window the
    protocol did not declare — and one remedy for all of them: this candidate's
    cost grid is not the grid that was preregistered, so nothing downstream may
    read it as one. Distinct from ``EvidenceIntegrityError``, which says a
    document is missing, altered, or not the canonical bytes its digest names;
    here every document verifies and the *set* is wrong.

    The specifics ride on the private cause so an operator can be told which of
    the six they hit, while the public message stays as uninformative as every
    other code here.
    """

    public_message = "sealed scenarios do not match the declared cost grid"


class BrokerUnavailableError(TradingHouseError):
    """Raised when the broker terminal cannot be reached or initialised."""

    public_message = "broker terminal unavailable"


class BrokerError(TradingHouseError):
    """Raised when a broker response leaves an order's outcome unknown.

    A ``None`` result from an order-send call is never a rejection -- the
    order may have executed with its confirmation lost in transit. Reporting
    it as a rejection would let the caller record REJECTED for a position
    that actually exists, and nothing would ever look for it again. Raising
    keeps the intent in an unresolved state for reconciliation to settle.
    """

    public_message = "broker response unusable; order outcome unknown"


class NonDemoAccountError(TradingHouseError):
    """Raised when the connected account is not a demo account."""

    public_message = "refusing to operate a non-demo account"


class CoverageError(TradingHouseError):
    """Raised when a market-data read asks for more than the store holds."""

    public_message = "requested market data exceeds stored coverage"


class InsufficientHistoryError(TradingHouseError):
    """Raised when a feature needs more history than the store holds."""

    public_message = "insufficient history to compute the feature"


class IntentAlreadySubmittedError(TradingHouseError):
    """Raised when ``submit()`` is called for an intent_id the ledger already
    has events for. A genuine retry must mint a fresh intent_id (§3.6);
    resubmitting the same one is exactly the resend that risks doubling a
    position."""

    public_message = "intent already submitted; retry requires a fresh intent_id"


class UnresolvedIntentsError(TradingHouseError):
    """Raised by the gate when the ledger holds intents still in a
    non-terminal state (I-20). Names them: an operator who cannot see which
    intent is stuck cannot clear it."""

    public_message = "unresolved intents block further order submission"

    def __init__(self, intent_ids: Sequence[str]) -> None:
        self.intent_ids = tuple(intent_ids)
        message = f"{self.public_message}: {', '.join(self.intent_ids)}"
        Exception.__init__(self, message)


class ConcurrentSubmissionError(TradingHouseError):
    """Raised when another invocation already holds the submission lock.

    Two ``order submit`` runs a second apart would otherwise both read a
    clean ledger and both send (I-6). Serialising them is the point; the
    loser refuses rather than queueing, because by the time the lock frees
    the ledger it read is stale anyway.
    """

    public_message = "another order submission is in progress"


class TrialLedgerAppendError(TradingHouseError):
    """Raised when a trial ledger event cannot be appended.

    One error for every append failure -- a lost race, a duplicate event id, a
    result recorded against no registration, an unreachable database -- because
    the caller has exactly one remedy for all of them: do not record the trial
    as run. The driver's message, which names the constraint or the connection
    string, stays on the private cause.
    """

    public_message = "trial ledger append failed"


class TrialLedgerIntegrityError(TradingHouseError):
    """Raised when trial ledger integrity verification cannot be completed.

    Distinct from a *failed* verification, which ``PostgresTrialLedger.verify``
    returns as a ``LedgerIntegrityReport`` so an operator can be told what is
    wrong. This is for "the ledger could not be read to be checked at all",
    which is not an answer and must not be reported as one.
    """

    public_message = "trial ledger integrity verification failed"
