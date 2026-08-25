"""I-16: cross-book budgets live only on ``FirmLimits``, never on ``BookLimits``.

All four books trade on one broker account and share one margin pool. An
``fx_scalp`` EURUSD long and an ``fx_swing`` EURUSD long are the same market
exposure wearing two hats. If correlation and leverage budgets were declared
per book, the book split would hide that shared risk instead of containing
it.

``max_gross_leverage`` is the one exception: spec 3.4 endorses a per-book
leverage ceiling as necessary (it bounds that book's own risk-taking) but not
sufficient, since every book draws on the same shared margin pool -- so it
legitimately exists on both ``BookLimits`` and ``FirmLimits``, with the firm
value binding across the pool. It is a cross-book *budget* (the firm must
still declare it) without being a book-*forbidden* field.
"""

from trading_house.constitution.models import BookLimits, FirmLimits

CROSS_BOOK_BUDGETS = {
    "max_aggregate_open_risk_pct",
    "max_gross_leverage",
    "max_correlated_cluster_risk_pct",
    "max_single_instrument_risk_pct",
}

# Every cross-book budget except leverage must never appear on a book: a
# per-book value would hide correlated exposure instead of containing it.
# max_gross_leverage is deliberately excluded -- it legitimately exists on
# BookLimits too (see module docstring).
BOOK_FORBIDDEN_BUDGETS = CROSS_BOOK_BUDGETS - {"max_gross_leverage"}


def test_firm_declares_every_cross_book_budget() -> None:
    assert set(FirmLimits.model_fields) >= CROSS_BOOK_BUDGETS


def test_no_book_declares_a_correlation_budget() -> None:
    """I-16: an fx_scalp and an fx_swing EURUSD long are one exposure.
    A per-book correlation budget would hide that instead of containing it."""

    assert BOOK_FORBIDDEN_BUDGETS.isdisjoint(BookLimits.model_fields)


def test_book_leverage_is_a_real_per_book_ceiling_not_a_forbidden_leak() -> None:
    """max_gross_leverage on BookLimits is an endorsed second layer (spec 3.4),
    not the same kind of leak as a per-book correlation budget."""

    assert "max_gross_leverage" in BookLimits.model_fields
    assert "max_gross_leverage" in FirmLimits.model_fields
