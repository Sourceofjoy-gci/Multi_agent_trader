from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    CONFIGURATION = 2
    SIGNATURE = 3
    DATABASE = 4
    MIGRATION = 5
    AUDIT_INTEGRITY = 6
    AUDIT_APPEND = 7


class TradingHouseError(Exception):
    """Base exception for domain-specific failures."""


class TimestampError(TradingHouseError):
    """Raised when a timestamp violates the UTC boundary."""


class ConfigurationError(TradingHouseError):
    """Raised when application configuration is invalid."""


class SignatureVerificationError(TradingHouseError):
    """Raised when signature verification fails."""


class SchemaValidationError(TradingHouseError):
    """Raised when schema validation fails."""


class DatabaseUnavailableError(TradingHouseError):
    """Raised when the database cannot be reached."""


class MigrationMismatchError(TradingHouseError):
    """Raised when the database migration state is incompatible."""


class AuditAppendError(TradingHouseError):
    """Raised when an audit record cannot be appended."""


class AuditIntegrityError(TradingHouseError):
    """Raised when audit-log integrity validation fails."""
