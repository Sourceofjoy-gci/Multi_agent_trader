"""The portfolio conditions that latch a halt.

Phase 10's gates already refuse an order that would breach a drawdown halt or
extend a reject streak. What they cannot do is hold: the arithmetic is redone
on every order, and a halt the constitution says "requires human unlock" has to
outlive the numbers that tripped it. These are the conditions that, once seen,
are written down as halts a person must clear (Phase 11 design).

Unlike the gates, these compare what has already happened -- the drawdown, the
streak -- and not what one more order could add. A book one trade from its halt
is refused that trade; a book at its halt is halted.
"""

from __future__ import annotations

from decimal import Decimal

from trading_house.constitution.models import Constitution
from trading_house.core.control import SYSTEM_ACTOR, HaltKind, HaltRequest, HaltScope
from trading_house.risk.portfolio import PortfolioState

_HUNDRED = Decimal(100)


def latch_requests(
    constitution: Constitution, portfolio: PortfolioState, firm_equity: Decimal
) -> tuple[HaltRequest, ...]:
    requests: list[HaltRequest] = []
    if portfolio.book_pnl is not None:
        for book_id, pnl in sorted(portfolio.book_pnl.items()):
            book = constitution.books.get(book_id)
            if book is None:
                continue
            book_equity = firm_equity * book.capital_fraction
            if pnl.drawdown * _HUNDRED >= book.max_drawdown_halt_pct * book_equity:
                requests.append(
                    HaltRequest(
                        kind=HaltKind.DRAWDOWN_HALT,
                        scope=HaltScope.BOOK,
                        target=book_id,
                        reason="book_drawdown_halt",
                        actor=SYSTEM_ACTOR,
                    )
                )
    if (
        portfolio.firm_drawdown is not None
        and portfolio.firm_drawdown * _HUNDRED
        >= constitution.firm.max_total_drawdown_halt_pct * firm_equity
    ):
        requests.append(
            HaltRequest(
                kind=HaltKind.DRAWDOWN_HALT,
                scope=HaltScope.FIRM,
                target=None,
                reason="firm_drawdown_halt",
                actor=SYSTEM_ACTOR,
            )
        )
    if portfolio.consecutive_rejects >= constitution.firm.max_consecutive_rejects:
        requests.append(
            HaltRequest(
                kind=HaltKind.SAFE_MODE,
                scope=HaltScope.FIRM,
                target=None,
                reason="consecutive_rejects",
                actor=SYSTEM_ACTOR,
            )
        )
    return tuple(requests)
