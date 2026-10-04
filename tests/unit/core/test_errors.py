import pytest

from trading_house.core.errors import (
    AuditAppendError,
    AuditIntegrityError,
    BrokerUnavailableError,
    ConfigurationError,
    DatabaseUnavailableError,
    ExitCode,
    MigrationMismatchError,
    NonDemoAccountError,
    PortfolioRiskRefusedError,
    SchemaValidationError,
    SignatureVerificationError,
    StatisticalInputError,
    TimestampError,
    TradingHouseError,
    TrialLedgerAppendError,
    TrialLedgerIntegrityError,
)


def test_exit_codes_are_stable() -> None:
    assert ExitCode.CONFIGURATION == 2
    assert ExitCode.SIGNATURE == 3
    assert ExitCode.DATABASE == 4
    assert ExitCode.MIGRATION == 5
    assert ExitCode.AUDIT_INTEGRITY == 6


@pytest.mark.parametrize(
    ("error_type", "expected_message"),
    [
        (TradingHouseError, "trading-house operation failed"),
        (TimestampError, "timestamp must be timezone-aware"),
        (ConfigurationError, "configuration invalid"),
        (SignatureVerificationError, "signature verification failed"),
        (SchemaValidationError, "schema validation failed"),
        (DatabaseUnavailableError, "database connection failed"),
        (MigrationMismatchError, "migration revision mismatch"),
        (AuditAppendError, "audit append failed"),
        (AuditIntegrityError, "audit integrity verification failed"),
        (TrialLedgerAppendError, "trial ledger append failed"),
        (TrialLedgerIntegrityError, "trial ledger integrity verification failed"),
        (StatisticalInputError, "sealed evidence is not a usable statistical input"),
    ],
)
def test_errors_expose_only_canonical_public_messages(
    error_type: type[TradingHouseError], expected_message: str
) -> None:
    assert str(error_type()) == expected_message


@pytest.mark.parametrize(
    "error_type",
    [
        TradingHouseError,
        TimestampError,
        ConfigurationError,
        SignatureVerificationError,
        SchemaValidationError,
        DatabaseUnavailableError,
        MigrationMismatchError,
        AuditAppendError,
        AuditIntegrityError,
        TrialLedgerAppendError,
        TrialLedgerIntegrityError,
        StatisticalInputError,
    ],
)
def test_errors_reject_secret_bearing_positional_details(
    error_type: type[TradingHouseError],
) -> None:
    raw_detail = "postgresql://operator:" + "password" + "@db.example/trading"

    with pytest.raises(TypeError):
        error_type(raw_detail)

    assert raw_detail not in str(error_type())


def test_broker_errors_carry_fixed_public_messages() -> None:
    assert str(BrokerUnavailableError()) == "broker terminal unavailable"
    assert str(NonDemoAccountError()) == "refusing to operate a non-demo account"


def test_broker_exit_codes_are_distinct_and_new() -> None:
    assert ExitCode.BROKER == 8
    assert ExitCode.ACCOUNT_MODE == 9
    assert len({member.value for member in ExitCode}) == len(list(ExitCode))


def test_a_portfolio_refusal_names_its_gates_and_has_its_own_code() -> None:
    error = PortfolioRiskRefusedError(("daily_loss_stop", "aggregate_open_risk"))

    assert ExitCode.PORTFOLIO_RISK == 22
    assert error.reasons == ("daily_loss_stop", "aggregate_open_risk")
    assert str(error) == (
        "portfolio risk limits refuse this order: daily_loss_stop, aggregate_open_risk"
    )
