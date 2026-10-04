"""Phase 10: the live portfolio, assembled from the broker and the intent ledger.

The broker and the ledger are fakes; the binding is the signed one's shape, so
book attribution runs on the real magic ranges (fx_scalp 110000-119999,
fx_swing 120000-129999) and symbols (EURUSD, XAUUSD).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.unit.risk.conftest import BOOKS, CONFIG_DIR, _contract, _facts, _proposal
from trading_house.brokers.base import MarketSnapshot, Quote
from trading_house.constitution.binding import parse_venue_binding
from trading_house.constitution.loader import load_constitution
from trading_house.core.clock import FixedClock
from trading_house.core.errors import BrokerUnavailableError, PortfolioRiskRefusedError
from trading_house.core.instruments import InstrumentContract
from trading_house.core.schemas import ExecutableRiskDecision, Side
from trading_house.core.values import AssetClass
from trading_house.core.venue import DealMoney, PositionMark
from trading_house.ops.portfolio import (
    HISTORY_START,
    Baselines,
    build_live_portfolio,
    recheck_submission,
)
from trading_house.risk.engine import RiskEngine
from trading_house.risk.portfolio import PortfolioState

NOW = datetime(2026, 10, 4, 15, 0, tzinfo=UTC)
MIDNIGHT = datetime(2026, 10, 4, tzinfo=UTC)
SCALP = 110042
SWING = 120042
MANUAL = 0

BINDING = parse_venue_binding(
    b"""
venue: mt5
books:
  fx_scalp:     { magic_range: [110000, 119999] }
  fx_swing:     { magic_range: [120000, 129999] }
  equity_swing: { magic_range: [130000, 139999] }
  sleeve:       { magic_range: [140000, 149999] }
instruments:
  fx.eurusd:    { server_symbol: "EURUSD" }
  metal.xauusd: { server_symbol: "XAUUSD" }
"""
)
GOLD = _contract(
    instrument_id="metal.xauusd",
    asset_class=AssetClass.METAL,
    base_currency="XAU",
    quote_currency="USD",
    price_increment=Decimal("0.01"),
    point_size=Decimal("0.01"),
)


def _mark(
    *,
    magic: int = SCALP,
    symbol: str = "EURUSD",
    volume: str = "0.10",
    is_buy: bool = True,
    stop: str | None = "1.09500",
    price: str = "1.10000",
    unrealized: str = "0",
) -> PositionMark:
    return PositionMark(
        magic=magic,
        server_symbol=symbol,
        volume=Decimal(volume),
        is_buy=is_buy,
        stop_loss=None if stop is None else Decimal(stop),
        current_price=Decimal(price),
        unrealized_money=Decimal(unrealized),
    )


def _deal(magic: int, at: datetime, money: str) -> DealMoney:
    return DealMoney(magic=magic, dealt_at=at, net_money=Decimal(money))


@dataclass
class FakeVenue:
    marks: Sequence[PositionMark] | None = ()
    equity: Decimal | None = Decimal(100000)
    deals: Sequence[DealMoney] | None = ()
    quotes: tuple[Quote, ...] = (
        Quote(
            instrument_id="fx.eurusd",
            bid=Decimal("1.09990"),
            ask=Decimal("1.10000"),
            observed_at=NOW,
        ),
    )
    deal_starts: list[datetime] = field(default_factory=list)
    described: list[str] = field(default_factory=list)

    def account_equity(self) -> Decimal | None:
        return self.equity

    def deal_money_since(self, start: datetime) -> Sequence[DealMoney] | None:
        self.deal_starts.append(start)
        return self.deals

    def position_marks(self) -> Sequence[PositionMark] | None:
        return self.marks

    def describe_instrument(self, instrument_id: str) -> InstrumentContract:
        self.described.append(instrument_id)
        return GOLD if instrument_id == "metal.xauusd" else _contract()

    def snapshot(self, instrument_ids: Sequence[str]) -> MarketSnapshot:
        return MarketSnapshot(quotes=self.quotes, taken_at=NOW)


@dataclass
class FakeHistory:
    stamps: Sequence[tuple[str, datetime]] = ()
    rejects: int = 0
    since: list[datetime] = field(default_factory=list)
    rejects_since: list[datetime] = field(default_factory=list)

    def submissions_since(self, start: datetime) -> Sequence[tuple[str, datetime]]:
        self.since.append(start)
        return self.stamps

    def consecutive_rejects(self, since: datetime) -> int:
        self.rejects_since.append(since)
        return self.rejects


def _build(venue: FakeVenue, history: FakeHistory | None = None) -> PortfolioState:
    return build_live_portfolio(
        venue, history or FakeHistory(), BINDING, books=BOOKS, now=NOW
    ).state


# --- positions --------------------------------------------------------------------------


def test_an_empty_account_is_the_flat_portfolio() -> None:
    venue = FakeVenue()
    live = build_live_portfolio(venue, FakeHistory(), BINDING, books=BOOKS, now=NOW)

    assert live.state == PortfolioState.flat(BOOKS)
    assert live.firm_equity == Decimal(100000)
    assert venue.deal_starts == [HISTORY_START]


def test_a_position_is_attributed_by_magic_and_measured_from_its_mark() -> None:
    """0.10 lots marked at 1.10000 over a 1.09500 stop: 500 ticks at $1 a tick
    a lot is $50 at risk, and 11,000 of notional."""

    state = _build(FakeVenue(marks=(_mark(),)))

    (exposure,) = state.exposures
    assert exposure.book == "fx_scalp"
    assert exposure.instrument_id == "fx.eurusd"
    assert exposure.cluster_keys == frozenset({"ccy:EUR", "ccy:USD"})
    assert exposure.risk_money == Decimal(50)
    assert exposure.notional_money == Decimal(11000)


def test_a_manual_position_belongs_to_no_book_but_is_still_counted() -> None:
    (exposure,) = _build(FakeVenue(marks=(_mark(magic=MANUAL),))).exposures

    assert exposure.book is None


def test_a_position_with_no_stop_or_an_unbound_symbol_is_unmeasured() -> None:
    state = _build(
        FakeVenue(marks=(_mark(stop=None), _mark(symbol="GBPJPY"), _mark(symbol="XAUUSD")))
    )

    assert state.unmeasured_positions == 2
    assert [exposure.instrument_id for exposure in state.exposures] == ["metal.xauusd"]


def test_each_contract_is_described_once() -> None:
    venue = FakeVenue(marks=(_mark(), _mark(), _mark()))

    _build(venue)

    assert venue.described == ["fx.eurusd"]


@pytest.mark.parametrize(
    "venue",
    [FakeVenue(marks=None), FakeVenue(equity=None)],
    ids=["positions-unreadable", "equity-unreadable"],
)
def test_no_positions_or_no_equity_is_no_portfolio(venue: FakeVenue) -> None:
    with pytest.raises(BrokerUnavailableError):
        _build(venue)


# --- P&L --------------------------------------------------------------------------------


def test_unreadable_deal_history_leaves_the_pnl_unknown() -> None:
    state = _build(FakeVenue(deals=None))

    assert state.book_pnl is None
    assert state.firm_drawdown is None


def test_today_starts_at_utc_midnight_and_each_book_keeps_its_own_deals() -> None:
    yesterday = MIDNIGHT - timedelta(seconds=1)
    state = _build(
        FakeVenue(
            deals=(
                _deal(SCALP, yesterday, "-500"),
                _deal(SCALP, MIDNIGHT, "-120"),
                _deal(SCALP, NOW, "30"),
                _deal(SWING, NOW, "-40"),
                _deal(MANUAL, NOW, "-9999"),
            )
        )
    )

    assert state.book_pnl is not None
    assert state.book_pnl["fx_scalp"].realized_today == Decimal(-90)
    assert state.book_pnl["fx_swing"].realized_today == Decimal(-40)
    assert state.book_pnl["sleeve"].realized_today == 0


def test_drawdown_is_peak_to_now_on_the_closed_curve_plus_the_open_marks() -> None:
    """Scalp's closed curve runs 0 -> 300 -> 100: a 200 drawdown, and an open
    position 50 under water makes it 250. Swing never rose above zero, so its
    -40 is 40 down from the starting peak. The firm's own curve (manual deals
    excluded) runs 0, 300, 100, 60, peaking at 300: 240 closed plus the 50."""

    day = MIDNIGHT - timedelta(days=3)
    state = _build(
        FakeVenue(
            marks=(_mark(unrealized="-50"),),
            deals=(
                _deal(SCALP, day, "300"),
                _deal(SCALP, day + timedelta(hours=1), "-200"),
                _deal(SWING, day + timedelta(hours=2), "-40"),
                _deal(MANUAL, day + timedelta(hours=3), "5000"),
            ),
        )
    )

    assert state.book_pnl is not None
    assert state.book_pnl["fx_scalp"].drawdown == Decimal(250)
    assert state.book_pnl["fx_scalp"].unrealized == Decimal(-50)
    assert state.book_pnl["fx_swing"].drawdown == Decimal(40)
    assert state.firm_drawdown == Decimal(290)


def test_deals_out_of_order_are_replayed_in_time_order() -> None:
    day = MIDNIGHT - timedelta(days=2)
    state = _build(
        FakeVenue(deals=(_deal(SCALP, day + timedelta(hours=1), "-200"), _deal(SCALP, day, "300")))
    )

    assert state.book_pnl is not None
    assert state.book_pnl["fx_scalp"].drawdown == Decimal(200)


# --- the ledger -------------------------------------------------------------------------


def test_order_stamps_come_from_the_last_minute_and_the_streak_is_passed_through() -> None:
    history = FakeHistory(stamps=(("fx_scalp", NOW - timedelta(seconds=5)),), rejects=3)

    state = _build(FakeVenue(), history)

    assert history.since == [NOW - timedelta(minutes=1)]
    assert [stamp.book for stamp in state.order_stamps] == ["fx_scalp"]
    assert state.consecutive_rejects == 3


# --- the re-check order submit runs -----------------------------------------------------

CONSTITUTION = load_constitution(
    CONFIG_DIR / "risk_constitution.yaml",
    CONFIG_DIR / "risk_constitution.yaml.sig",
    CONFIG_DIR / "risk_constitution.public.pem",
).constitution


def _decision() -> ExecutableRiskDecision:
    engine = RiskEngine(CONSTITUTION, FixedClock(NOW))
    decision = engine.evaluate(
        _proposal(),
        contract=_contract(),
        **_facts(tick_time=NOW, portfolio=PortfolioState.flat(BOOKS)),
    )
    assert isinstance(decision, ExecutableRiskDecision)
    return decision


def _recheck(venue: FakeVenue, history: FakeHistory | None = None) -> None:
    recheck_submission(
        RiskEngine(CONSTITUTION, FixedClock(NOW)),
        _decision(),
        venue=venue,
        live=build_live_portfolio(venue, history or FakeHistory(), BINDING, books=BOOKS, now=NOW),
        book="fx_scalp",
        instrument_id="fx.eurusd",
        side=Side.BUY,
    )


def test_a_clean_account_lets_the_decision_through() -> None:
    _recheck(FakeVenue())


def test_a_refusal_names_every_gate_that_refused() -> None:
    with pytest.raises(PortfolioRiskRefusedError) as refusal:
        _recheck(FakeVenue(marks=(_mark(stop=None),), deals=None))

    assert refusal.value.reasons == ("unmeasured_open_risk", "pnl_unavailable")


def test_the_streak_refuses_at_submission() -> None:
    with pytest.raises(PortfolioRiskRefusedError) as refusal:
        _recheck(FakeVenue(), FakeHistory(rejects=5))

    assert refusal.value.reasons == ("consecutive_rejects_exceeded",)


def test_no_quote_for_the_instrument_is_no_price_to_judge_at() -> None:
    with pytest.raises(BrokerUnavailableError):
        _recheck(FakeVenue(quotes=()))


# --- Phase 11: a person's clear restarts the counts -------------------------------------


def test_a_cleared_drawdown_halt_restarts_that_curve_from_the_clear() -> None:
    """Scalp lost 500 before its halt was cleared and 40 after: only the 40
    counts. The firm's own clear is later still, so only the swing deal after
    it reaches the firm curve."""

    cleared = MIDNIGHT - timedelta(days=1)
    firm_cleared = MIDNIGHT - timedelta(hours=1)
    venue = FakeVenue(
        deals=(
            _deal(SCALP, cleared - timedelta(hours=1), "-500"),
            _deal(SCALP, cleared + timedelta(hours=1), "-40"),
            _deal(SWING, NOW, "-25"),
        )
    )
    history = FakeHistory()

    state = build_live_portfolio(
        venue,
        history,
        BINDING,
        books=BOOKS,
        now=NOW,
        baselines=Baselines(
            book_drawdown_since={"fx_scalp": cleared},
            firm_drawdown_since=firm_cleared,
            rejects_since=firm_cleared,
        ),
    ).state

    assert state.book_pnl is not None
    assert state.book_pnl["fx_scalp"].drawdown == Decimal(40)
    assert state.book_pnl["fx_swing"].drawdown == Decimal(25)
    assert state.firm_drawdown == Decimal(25)
    assert history.rejects_since == [firm_cleared]


def test_never_cleared_counts_from_the_start_of_history() -> None:
    history = FakeHistory()

    _build(FakeVenue(), history)

    assert history.rejects_since == [HISTORY_START]
