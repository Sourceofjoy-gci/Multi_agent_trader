"""I-16: cross-book budgets live only on ``FirmLimits``, never on ``BookLimits``.

All four books trade on one broker account and share one margin pool. An
``fx_scalp`` EURUSD long and an ``fx_swing`` EURUSD long are the same market
exposure wearing two hats. If correlation and leverage budgets were declared
per book, the book split would hide that shared risk instead of containing
it.
"""

from trading_house.constitution.models import BookLimits, FirmLimits

FIRM_ONLY_LIMITS = {
    "max_aggregate_open_risk_pct",
    "max_gross_leverage",
    "max_correlated_cluster_risk_pct",
    "max_single_instrument_risk_pct",
}


def test_firm_declares_every_cross_book_budget() -> None:
    assert set(FirmLimits.model_fields) >= FIRM_ONLY_LIMITS


def test_no_book_declares_a_correlation_budget() -> None:
    """I-16: an fx_scalp and an fx_swing EURUSD long are one exposure.
    A per-book correlation budget would hide that instead of containing it."""

    assert "max_correlated_cluster_risk_pct" not in BookLimits.model_fields
    assert "max_single_instrument_risk_pct" not in BookLimits.model_fields
    assert "max_aggregate_open_risk_pct" not in BookLimits.model_fields
