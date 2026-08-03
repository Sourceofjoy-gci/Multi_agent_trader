from trading_house.core.errors import ExitCode


def test_exit_codes_are_stable() -> None:
    assert ExitCode.CONFIGURATION == 2
    assert ExitCode.SIGNATURE == 3
    assert ExitCode.DATABASE == 4
    assert ExitCode.MIGRATION == 5
    assert ExitCode.AUDIT_INTEGRITY == 6
