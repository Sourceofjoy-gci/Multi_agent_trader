"""Phase 8C3: the signed constants, each pinned by a literal so none can move quietly.

A mutation that edits any value, or changes its type, fails here. The three that the
8C3 measurements read (``MAX_DRAWDOWN``, ``CPCV_P5_QUANTILE``, ``MC_POLICY_VERSION``) are
also pinned by the behaviour tests of the module that reads them.
"""

from __future__ import annotations

from trading_house.research.validation import policy


def test_every_signed_constant_is_its_literal() -> None:
    assert policy.DSR_MINIMUM == 0.95
    assert policy.PBO_MAXIMUM == 0.50
    assert policy.MAX_DRAWDOWN == 0.10
    assert policy.DRAWDOWN_TOLERANCE == 1e-12
    assert policy.MIN_OOS_TRADES == 30
    assert policy.MIN_REGIMES == 2
    assert policy.CPCV_P5_QUANTILE == 0.05
    assert policy.MIN_WFA_FOLDS == 1
    assert policy.MC_POLICY_VERSION == "8c-mc-1"


def test_the_types_are_the_intended_ones_and_nothing_else_is_defined() -> None:
    assert type(policy.MIN_OOS_TRADES) is int
    assert type(policy.MIN_REGIMES) is int
    assert type(policy.MIN_WFA_FOLDS) is int
    for name in (
        "DSR_MINIMUM",
        "PBO_MAXIMUM",
        "MAX_DRAWDOWN",
        "DRAWDOWN_TOLERANCE",
        "CPCV_P5_QUANTILE",
    ):
        assert type(getattr(policy, name)) is float
    public = {name for name in vars(policy) if name.isupper()}
    assert public == {
        "DSR_MINIMUM",
        "PBO_MAXIMUM",
        "MAX_DRAWDOWN",
        "DRAWDOWN_TOLERANCE",
        "MIN_OOS_TRADES",
        "MIN_REGIMES",
        "CPCV_P5_QUANTILE",
        "MIN_WFA_FOLDS",
        "MC_POLICY_VERSION",
    }
