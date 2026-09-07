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


class BrokerUnavailableError(TradingHouseError):
    """Raised when the broker terminal cannot be reached or initialised."""

    public_message = "broker terminal unavailable"


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
