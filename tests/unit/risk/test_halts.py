"""Phase 11: the portfolio conditions that latch a halt, at their boundaries.

Firm equity 100,000. fx_scalp's 6% halt of its 30,000 slice is 1,800; the
firm's 10% halt is 10,000; five consecutive rejects is the signed streak.
"""

from __future__ import annotations

from decimal import Decimal

from tests.unit.risk.conftest import BOOKS
from trading_house.core.control import HaltKind, HaltScope
from trading_house.risk.halts import latch_requests
from trading_house.risk.portfolio import BookPnl, PortfolioState

EQUITY = Decimal(100000)
ZERO = Decimal(0)


def _state(
    *, scalp_drawdown: str = "0", firm_drawdown: str = "0", rejects: int = 0
) -> PortfolioState:
    pnl = {book: BookPnl(realized_today=ZERO, unrealized=ZERO, drawdown=ZERO) for book in BOOKS}
    pnl["fx_scalp"] = BookPnl(
        realized_today=ZERO, unrealized=ZERO, drawdown=Decimal(scalp_drawdown)
    )
    return PortfolioState(
        exposures=(),
        unmeasured_positions=0,
        book_pnl=pnl,
        firm_drawdown=Decimal(firm_drawdown),
        order_stamps=(),
        consecutive_rejects=rejects,
    )


def test_a_clean_portfolio_latches_nothing(constitution) -> None:
    assert latch_requests(constitution, PortfolioState.flat(BOOKS), EQUITY) == ()


def test_a_book_at_its_halt_latches_that_book(constitution) -> None:
    assert latch_requests(constitution, _state(scalp_drawdown="1799.99"), EQUITY) == ()
    (request,) = latch_requests(constitution, _state(scalp_drawdown="1800"), EQUITY)

    assert (request.kind, request.scope, request.target) == (
        HaltKind.DRAWDOWN_HALT,
        HaltScope.BOOK,
        "fx_scalp",
    )
    assert request.actor == "system"


def test_the_firm_at_its_halt_latches_the_firm(constitution) -> None:
    assert latch_requests(constitution, _state(firm_drawdown="9999.99"), EQUITY) == ()
    (request,) = latch_requests(constitution, _state(firm_drawdown="10000"), EQUITY)

    assert (request.kind, request.scope, request.target) == (
        HaltKind.DRAWDOWN_HALT,
        HaltScope.FIRM,
        None,
    )


def test_the_reject_streak_latches_safe_mode(constitution) -> None:
    assert latch_requests(constitution, _state(rejects=4), EQUITY) == ()
    (request,) = latch_requests(constitution, _state(rejects=5), EQUITY)

    assert (request.kind, request.scope, request.reason) == (
        HaltKind.SAFE_MODE,
        HaltScope.FIRM,
        "consecutive_rejects",
    )


def test_unknown_pnl_latches_no_drawdown_halt(constitution) -> None:
    unknown = PortfolioState(
        exposures=(),
        unmeasured_positions=0,
        book_pnl=None,
        firm_drawdown=None,
        order_stamps=(),
        consecutive_rejects=0,
    )

    # The gate already refuses unknown P&L; a halt latched on a number nobody
    # read would have to be cleared by a person for no reason.
    assert latch_requests(constitution, unknown, EQUITY) == ()
