"""The live ``PortfolioState``: what is open at the broker, what it has made and
lost, and what the intent ledger says was sent.

``risk/`` may not reach a broker or a store, so the state it judges is
assembled here, the same way ``ops/guard.py`` assembles the guard's ports
(Phase 10 design, section 5.2).

Two reads have no honest partial answer. An open-position list or an account
equity the terminal could not produce raises ``BrokerUnavailableError``: there
is no portfolio to judge without either. Deal history that could not be read
leaves the P&L fields ``None``, which the engine refuses as
``pnl_unavailable`` rather than reading as a flat day.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final, Protocol

from trading_house.brokers.base import MarketSnapshot
from trading_house.constitution.binding import VenueBinding
from trading_house.core.clock import ensure_utc
from trading_house.core.errors import BrokerUnavailableError, PortfolioRiskRefusedError
from trading_house.core.instruments import InstrumentContract
from trading_house.core.schemas import RejectedRiskDecision, RiskDecision, Side
from trading_house.core.values import BookId, InstrumentId
from trading_house.core.venue import DealMoney, PositionMark
from trading_house.risk.engine import RiskEngine
from trading_house.risk.portfolio import (
    ORDER_RATE_WINDOW,
    BookPnl,
    OpenExposure,
    OrderStamp,
    PortfolioState,
    cluster_keys,
    loss_to_stop,
    notional_money,
)

HISTORY_START: Final = datetime(2000, 1, 1, tzinfo=UTC)
"""Where the drawdown curve starts. Before any MT5 broker's deal history."""


class PortfolioVenue(Protocol):
    def account_equity(self) -> Decimal | None: ...
    def deal_money_since(self, start: datetime) -> Sequence[DealMoney] | None: ...
    def position_marks(self) -> Sequence[PositionMark] | None: ...
    def describe_instrument(self, instrument_id: InstrumentId) -> InstrumentContract: ...


class SubmissionVenue(PortfolioVenue, Protocol):
    def snapshot(self, instrument_ids: Sequence[InstrumentId]) -> MarketSnapshot: ...


class OrderHistory(Protocol):
    def submissions_since(self, start: datetime) -> Sequence[tuple[str, datetime]]: ...
    def consecutive_rejects(self, since: datetime) -> int: ...


@dataclass(frozen=True, slots=True)
class LivePortfolio:
    firm_equity: Decimal
    state: PortfolioState


@dataclass(frozen=True, slots=True)
class Baselines:
    """Where each running count restarts once a person has cleared its halt
    (Phase 11).

    Clearing a drawdown halt restarts that book's -- or the firm's -- curve
    from zero at the moment it was cleared: without that, the halt would
    re-latch on the very next read and the unlock would mean nothing. Clearing
    safe mode restarts the reject streak for the same reason. ``None`` and a
    missing book both mean never cleared: the start of history.
    """

    book_drawdown_since: Mapping[BookId, datetime]
    firm_drawdown_since: datetime | None
    rejects_since: datetime | None


NEVER_CLEARED: Final = Baselines(
    book_drawdown_since={}, firm_drawdown_since=None, rejects_since=None
)


def build_live_portfolio(
    venue: PortfolioVenue,
    history: OrderHistory,
    binding: VenueBinding,
    *,
    books: Iterable[BookId],
    now: datetime,
    baselines: Baselines = NEVER_CLEARED,
) -> LivePortfolio:
    now = ensure_utc(now)
    marks = venue.position_marks()
    equity = venue.account_equity()
    if marks is None or equity is None:
        raise BrokerUnavailableError()

    ranges = {book: binding.books[book].magic_range for book in binding.books}
    symbols = {entry.server_symbol: iid for iid, entry in binding.instruments.items()}
    contracts: dict[InstrumentId, InstrumentContract] = {}

    def book_of(magic: int) -> BookId | None:
        return next((book for book, (low, high) in ranges.items() if low <= magic <= high), None)

    exposures: list[OpenExposure] = []
    unmeasured = 0
    for mark in marks:
        instrument_id = symbols.get(mark.server_symbol)
        if instrument_id is None or mark.stop_loss is None:
            # A symbol the signed binding does not name has no contract to
            # price it with, and a position with no stop has no bounded loss.
            unmeasured += 1
            continue
        if instrument_id not in contracts:
            contracts[instrument_id] = venue.describe_instrument(instrument_id)
        contract = contracts[instrument_id]
        exposures.append(
            OpenExposure(
                book=book_of(mark.magic),
                instrument_id=instrument_id,
                cluster_keys=cluster_keys(contract),
                risk_money=loss_to_stop(
                    quantity=mark.volume,
                    from_price=mark.current_price,
                    stop=mark.stop_loss,
                    is_buy=mark.is_buy,
                    contract=contract,
                ),
                notional_money=notional_money(mark.volume, mark.current_price, contract),
            )
        )

    book_ids = tuple(books)
    book_pnl, firm_drawdown = _pnl(
        venue.deal_money_since(HISTORY_START), marks, book_of, book_ids, now, baselines
    )
    stamps = tuple(
        OrderStamp(book=book, submitted_at=at)
        for book, at in history.submissions_since(now - ORDER_RATE_WINDOW)
    )
    return LivePortfolio(
        firm_equity=equity,
        state=PortfolioState(
            exposures=tuple(exposures),
            unmeasured_positions=unmeasured,
            book_pnl=book_pnl,
            firm_drawdown=firm_drawdown,
            order_stamps=stamps,
            consecutive_rejects=history.consecutive_rejects(
                baselines.rejects_since or HISTORY_START
            ),
        ),
    )


def _pnl(
    deals: Sequence[DealMoney] | None,
    marks: Sequence[PositionMark],
    book_of: _BookOf,
    books: Sequence[BookId],
    now: datetime,
    baselines: Baselines,
) -> tuple[Mapping[BookId, BookPnl] | None, Decimal | None]:
    """Each book's P&L and the firm's drawdown, from system-owned deals and
    positions only. Balance operations and manual trades carry no book's magic
    and are outside both (Phase 10 design, section 6). Each drawdown curve
    starts at its baseline; today's realised P&L does not, because a person
    clearing a halt does not change what was lost today."""

    if deals is None:
        return None, None
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    owned = sorted(
        ((deal, book) for deal in deals if (book := book_of(deal.magic)) is not None),
        key=lambda pair: pair[0].dealt_at,
    )
    unrealized: dict[BookId, Decimal] = {}
    for mark in marks:
        book = book_of(mark.magic)
        if book is not None:
            unrealized[book] = unrealized.get(book, Decimal(0)) + mark.unrealized_money

    result: dict[BookId, BookPnl] = {}
    for book in books:
        book_deals = [deal for deal, owner in owned if owner == book]
        result[book] = BookPnl(
            realized_today=sum(
                (deal.net_money for deal in book_deals if deal.dealt_at >= midnight),
                Decimal(0),
            ),
            unrealized=unrealized.get(book, Decimal(0)),
            drawdown=_drawdown(
                _since(book_deals, baselines.book_drawdown_since.get(book)),
                unrealized.get(book, Decimal(0)),
            ),
        )
    firm = _drawdown(
        _since([deal for deal, _ in owned], baselines.firm_drawdown_since),
        sum(unrealized.values(), Decimal(0)),
    )
    return result, firm


def _since(deals: Sequence[DealMoney], start: datetime | None) -> Sequence[DealMoney]:
    return deals if start is None else [deal for deal in deals if deal.dealt_at > start]


class _BookOf(Protocol):
    def __call__(self, magic: int, /) -> BookId | None: ...


def _drawdown(deals: Sequence[DealMoney], unrealized: Decimal) -> Decimal:
    """Peak of the closed-trade curve (from zero) less where it stands now with
    the open positions marked, never negative."""

    running = peak = Decimal(0)
    for deal in deals:
        running += deal.net_money
        peak = max(peak, running)
    return max(Decimal(0), peak - (running + unrealized))


def recheck_submission(
    engine: RiskEngine,
    decision: RiskDecision,
    *,
    venue: SubmissionVenue,
    live: LivePortfolio,
    book: BookId,
    instrument_id: InstrumentId,
    side: Side,
) -> None:
    """Re-judge a decision against the live portfolio, or raise.

    Priced at the quote the order would cross -- the ask for a buy, the bid for
    a sell. No quote means no price to judge at, which is the terminal's
    failure to answer, not a market with nothing in it.
    """

    contract = venue.describe_instrument(instrument_id)
    quote = next(
        (q for q in venue.snapshot([instrument_id]).quotes if q.instrument_id == instrument_id),
        None,
    )
    if quote is None:
        raise BrokerUnavailableError()
    rechecked = engine.recheck_portfolio(
        decision,
        book_id=book,
        side=side,
        contract=contract,
        firm_equity=live.firm_equity,
        price=quote.ask if side is Side.BUY else quote.bid,
        portfolio=live.state,
    )
    if isinstance(rechecked, RejectedRiskDecision):
        raise PortfolioRiskRefusedError(rechecked.reasons)
