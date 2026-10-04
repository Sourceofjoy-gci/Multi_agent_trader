"""Phase 10: the portfolio gates, each on both sides of its boundary.

Every boundary is worked from the real signed constitution at firm equity
100,000. The default proposal is the scalp book's: a 300-tick stop sizes 0.25
lots from its $75 budget (100,000 x 0.30 x 0.25%), 27,500 of notional at
1.10000.

    fx_scalp: book equity 30,000; daily stop 1.5% = 450; drawdown halt 6% =
              1,800; 10x gross = 300,000; 3 concurrent; 15 orders a minute
    firm:     drawdown halt 10% = 10,000; aggregate 4% = 4,000; cluster 1% =
              1,000; one instrument 0.7% = 700; 8x gross = 800,000; 30 orders
              a minute; 5 consecutive rejects
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal

import pytest

from tests.unit.risk.conftest import BOOKS, NOW, _contract, _facts, _proposal
from trading_house.constitution.models import Constitution
from trading_house.core.clock import FixedClock
from trading_house.core.schemas import (
    ExecutableRiskDecision,
    RejectedRiskDecision,
    RiskDecision,
    Side,
)
from trading_house.core.values import AssetClass
from trading_house.risk.engine import RejectionReason, RiskEngine
from trading_house.risk.portfolio import (
    BookPnl,
    OpenExposure,
    OrderStamp,
    PortfolioState,
    cluster_keys,
    loss_to_stop,
    notional_money,
)

ZERO = Decimal(0)
PORTFOLIO_CHECKS = (
    RejectionReason.UNMEASURED_OPEN_RISK,
    RejectionReason.PNL_UNAVAILABLE,
    RejectionReason.CONSECUTIVE_REJECTS_EXCEEDED,
    RejectionReason.ORDER_RATE_EXCEEDED,
    RejectionReason.MAX_CONCURRENT_POSITIONS,
    RejectionReason.DAILY_LOSS_STOP,
    RejectionReason.BOOK_DRAWDOWN_HALT,
    RejectionReason.FIRM_DRAWDOWN_HALT,
    RejectionReason.AGGREGATE_OPEN_RISK,
    RejectionReason.SINGLE_INSTRUMENT_RISK,
    RejectionReason.CORRELATED_CLUSTER_RISK,
    RejectionReason.BOOK_GROSS_LEVERAGE,
    RejectionReason.FIRM_GROSS_LEVERAGE,
)
USD_PAIR = frozenset({"ccy:EUR", "ccy:USD"})


def _engine(constitution: Constitution) -> RiskEngine:
    return RiskEngine(constitution, FixedClock(NOW))


def _exposure(
    *,
    book: str | None = "fx_scalp",
    instrument_id: str = "fx.eurusd",
    keys: frozenset[str] = USD_PAIR,
    risk: str = "0",
    notional: str = "0",
) -> OpenExposure:
    return OpenExposure(
        book=book,
        instrument_id=instrument_id,
        cluster_keys=keys,
        risk_money=Decimal(risk),
        notional_money=Decimal(notional),
    )


def _pnl(realized: str = "0", unrealized: str = "0", drawdown: str = "0") -> BookPnl:
    return BookPnl(
        realized_today=Decimal(realized),
        unrealized=Decimal(unrealized),
        drawdown=Decimal(drawdown),
    )


def _state(
    *,
    exposures: tuple[OpenExposure, ...] = (),
    unmeasured: int = 0,
    book_pnl: Mapping[str, BookPnl] | str | None = "flat",
    firm_drawdown: Decimal | None = ZERO,
    stamps: tuple[OrderStamp, ...] = (),
    rejects: int = 0,
) -> PortfolioState:
    pnl: Mapping[str, BookPnl] | None
    if book_pnl == "flat":
        pnl = {book: _pnl() for book in BOOKS}
    else:
        assert not isinstance(book_pnl, str)
        pnl = book_pnl
    return PortfolioState(
        exposures=exposures,
        unmeasured_positions=unmeasured,
        book_pnl=pnl,
        firm_drawdown=firm_drawdown,
        order_stamps=stamps,
        consecutive_rejects=rejects,
    )


def _scalp_pnl(**kwargs: str) -> dict[str, BookPnl]:
    return {**{book: _pnl() for book in BOOKS}, "fx_scalp": _pnl(**kwargs)}


def _evaluate(constitution: Constitution, state: PortfolioState, **facts: object) -> RiskDecision:
    return _engine(constitution).evaluate(
        _proposal(), contract=_contract(), **_facts(portfolio=state, **facts)
    )


def _refused_for(decision: RiskDecision) -> tuple[str, ...]:
    assert isinstance(decision, RejectedRiskDecision), decision
    return decision.reasons


def _approved(decision: RiskDecision) -> ExecutableRiskDecision:
    assert isinstance(decision, ExecutableRiskDecision), decision
    return decision


def _stamps(count: int, *, book: str = "fx_swing", age: timedelta = timedelta(0)) -> tuple:
    return tuple(OrderStamp(book=book, submitted_at=NOW - age) for _ in range(count))


# --- the flat state, and the arithmetic the gates are built from -------------------------


def test_a_flat_portfolio_passes_every_gate_and_says_so(constitution) -> None:
    decision = _approved(_evaluate(constitution, PortfolioState.flat(BOOKS)))

    assert decision.verdict == "APPROVED"
    assert decision.approved_quantity.amount == Decimal("0.25")
    for check in PORTFOLIO_CHECKS:
        assert check in decision.checks_passed


def test_clusters_are_currency_legs_for_fx_and_metal_and_the_class_otherwise() -> None:
    assert cluster_keys(_contract()) == USD_PAIR
    gold = _contract(
        instrument_id="metal.xauusd",
        asset_class=AssetClass.METAL,
        base_currency="XAU",
        quote_currency="USD",
    )
    assert cluster_keys(gold) == frozenset({"ccy:XAU", "ccy:USD"})
    stock = _contract(instrument_id="equity_cfd.aapl", asset_class=AssetClass.EQUITY_CFD)
    assert cluster_keys(stock) == frozenset({"class:equity_cfd"})


def test_notional_is_quantity_at_price_in_account_currency() -> None:
    assert notional_money(Decimal(1), Decimal("1.10000"), _contract()) == Decimal("110000")


def test_loss_to_stop_is_measured_from_the_given_price_and_floored_at_zero() -> None:
    contract = _contract()
    long_loss = loss_to_stop(
        quantity=Decimal("0.5"),
        from_price=Decimal("1.10000"),
        stop=Decimal("1.09900"),
        is_buy=True,
        contract=contract,
    )
    short_loss = loss_to_stop(
        quantity=Decimal("0.5"),
        from_price=Decimal("1.10000"),
        stop=Decimal("1.10100"),
        is_buy=False,
        contract=contract,
    )
    locked_in = loss_to_stop(
        quantity=Decimal("0.5"),
        from_price=Decimal("1.10000"),
        stop=Decimal("1.10050"),
        is_buy=True,
        contract=contract,
    )

    assert long_loss == short_loss == Decimal(50)
    assert locked_in == 0


def test_unrealised_gains_never_offset_a_realised_loss() -> None:
    assert _pnl(realized="-400", unrealized="100").loss_today() == Decimal(400)
    assert _pnl(realized="0", unrealized="-376").loss_today() == Decimal(376)
    assert _pnl(realized="200", unrealized="-50").loss_today() == 0
    assert _pnl(realized="300", unrealized="500").loss_today() == 0


# --- the state gates ----------------------------------------------------------------------


def test_an_open_position_whose_risk_cannot_be_measured_refuses_every_order(constitution) -> None:
    assert RejectionReason.UNMEASURED_OPEN_RISK in _refused_for(
        _evaluate(constitution, _state(unmeasured=1))
    )


@pytest.mark.parametrize(
    "state",
    [
        _state(book_pnl=None),
        _state(book_pnl={"fx_swing": _pnl()}),
        _state(firm_drawdown=None),
    ],
    ids=["no-pnl-at-all", "not-for-this-book", "no-firm-drawdown"],
)
def test_pnl_nobody_could_read_is_a_refusal_not_a_zero(constitution, state) -> None:
    reasons = _refused_for(_evaluate(constitution, state))

    assert RejectionReason.PNL_UNAVAILABLE in reasons


def test_the_reject_streak_refuses_at_the_signed_count(constitution) -> None:
    _approved(_evaluate(constitution, _state(rejects=4)))
    assert RejectionReason.CONSECUTIVE_REJECTS_EXCEEDED in _refused_for(
        _evaluate(constitution, _state(rejects=5))
    )


def test_the_firm_order_rate_counts_one_half_open_minute(constitution) -> None:
    _approved(_evaluate(constitution, _state(stamps=_stamps(29))))
    assert RejectionReason.ORDER_RATE_EXCEEDED in _refused_for(
        _evaluate(constitution, _state(stamps=_stamps(30)))
    )
    # Exactly a minute old is outside (now - 1 minute, now].
    _approved(
        _evaluate(
            constitution,
            _state(stamps=_stamps(29) + _stamps(5, age=timedelta(minutes=1))),
        )
    )


def test_a_scalp_book_also_holds_its_own_order_rate(constitution) -> None:
    _approved(_evaluate(constitution, _state(stamps=_stamps(14, book="fx_scalp"))))
    assert RejectionReason.ORDER_RATE_EXCEEDED in _refused_for(
        _evaluate(constitution, _state(stamps=_stamps(15, book="fx_scalp")))
    )
    # Another book's orders count toward the firm's 30, not the scalp book's 15.
    _approved(_evaluate(constitution, _state(stamps=_stamps(15, book="fx_swing"))))


def test_concurrency_counts_only_the_books_own_positions(constitution) -> None:
    two = (_exposure(), _exposure())
    _approved(_evaluate(constitution, _state(exposures=two)))
    assert RejectionReason.MAX_CONCURRENT_POSITIONS in _refused_for(
        _evaluate(constitution, _state(exposures=(*two, _exposure())))
    )
    others = (*two, _exposure(book="fx_swing"), _exposure(book=None))
    _approved(_evaluate(constitution, _state(exposures=others)))


# --- the halts: what is lost, plus what is still at risk -----------------------------------


def test_the_daily_stop_counts_todays_loss_and_every_open_risk(constitution) -> None:
    # 375 lost + 75 candidate = 450, the stop exactly.
    _approved(_evaluate(constitution, _state(book_pnl=_scalp_pnl(realized="-375"))))
    assert RejectionReason.DAILY_LOSS_STOP in _refused_for(
        _evaluate(constitution, _state(book_pnl=_scalp_pnl(realized="-375.01")))
    )
    # An unrealised gain does not buy back a realised loss.
    assert RejectionReason.DAILY_LOSS_STOP in _refused_for(
        _evaluate(constitution, _state(book_pnl=_scalp_pnl(realized="-400", unrealized="100")))
    )
    # 300 open in the book + 75 lost + 75 candidate = 450.
    lost = _scalp_pnl(realized="-75")
    _approved(_evaluate(constitution, _state(exposures=(_exposure(risk="300"),), book_pnl=lost)))
    assert RejectionReason.DAILY_LOSS_STOP in _refused_for(
        _evaluate(constitution, _state(exposures=(_exposure(risk="300.01"),), book_pnl=lost))
    )


def test_the_book_drawdown_halt_counts_open_risk_too(constitution) -> None:
    _approved(_evaluate(constitution, _state(book_pnl=_scalp_pnl(drawdown="1725"))))
    assert RejectionReason.BOOK_DRAWDOWN_HALT in _refused_for(
        _evaluate(constitution, _state(book_pnl=_scalp_pnl(drawdown="1725.01")))
    )


def test_the_firm_drawdown_halt_binds_on_firm_equity(constitution) -> None:
    _approved(_evaluate(constitution, _state(firm_drawdown=Decimal("9925"))))
    assert RejectionReason.FIRM_DRAWDOWN_HALT in _refused_for(
        _evaluate(constitution, _state(firm_drawdown=Decimal("9925.01")))
    )


# --- the exposure caps ---------------------------------------------------------------------


def _stock(risk: str) -> OpenExposure:
    return _exposure(
        book=None,
        instrument_id="equity_cfd.aapl",
        keys=frozenset({"class:equity_cfd"}),
        risk=risk,
    )


def test_aggregate_open_risk_includes_positions_no_book_owns(constitution) -> None:
    _approved(_evaluate(constitution, _state(exposures=(_stock("3925"),))))
    assert RejectionReason.AGGREGATE_OPEN_RISK in _refused_for(
        _evaluate(constitution, _state(exposures=(_stock("3925.01"),)))
    )


def test_one_instrument_is_capped_across_every_book(constitution) -> None:
    _approved(_evaluate(constitution, _state(exposures=(_exposure(book=None, risk="625"),))))
    assert RejectionReason.SINGLE_INSTRUMENT_RISK in _refused_for(
        _evaluate(constitution, _state(exposures=(_exposure(book=None, risk="625.01"),)))
    )


def test_a_shared_currency_leg_is_one_cluster_whatever_the_direction(constitution) -> None:
    def cable(risk: str) -> OpenExposure:
        return _exposure(
            book=None,
            instrument_id="fx.gbpusd",
            keys=frozenset({"ccy:GBP", "ccy:USD"}),
            risk=risk,
        )

    _approved(_evaluate(constitution, _state(exposures=(cable("925"),))))
    reasons = _refused_for(_evaluate(constitution, _state(exposures=(cable("925.01"),))))
    assert RejectionReason.CORRELATED_CLUSTER_RISK in reasons
    assert RejectionReason.SINGLE_INSTRUMENT_RISK not in reasons


# --- leverage: a ceiling on size, so it resizes ----------------------------------------------

TIGHT = {"atr": ZERO, "median_spread_points": ZERO, "tick_spread_points": ZERO}
"""A 10-tick stop: no volatility or cost term, so the 0.00010 structural term
binds and the $75 budget sizes 7.5 lots, 825,000 of notional."""


def _tight(constitution: Constitution, state: PortfolioState) -> RiskDecision:
    return _engine(constitution).evaluate(
        _proposal(invalidation_price=Decimal("1.09990")),
        contract=_contract(),
        **_facts(portfolio=state, **TIGHT),
    )


def test_a_size_past_the_books_leverage_is_resized_to_it(constitution) -> None:
    decision = _approved(_tight(constitution, PortfolioState.flat(BOOKS)))

    # 300,000 / 110,000 = 2.727..., floored to the 0.01 grid.
    assert decision.verdict == "RESIZED"
    assert decision.approved_quantity.amount == Decimal("2.72")
    assert decision.risk_money == Decimal("27.20")


def test_the_books_open_notional_shrinks_the_room_left(constitution) -> None:
    state = _state(exposures=(_exposure(notional="200000"),))

    # 100,000 left / 110,000 = 0.909...
    assert _approved(_tight(constitution, state)).approved_quantity.amount == Decimal("0.90")


def test_less_than_a_minimum_lot_of_room_names_the_limit_that_binds(constitution) -> None:
    book_full = _state(exposures=(_exposure(notional="299000"),))
    firm_full = _state(exposures=(_exposure(book=None, notional="799000"),))

    assert _refused_for(_tight(constitution, book_full)) == (RejectionReason.BOOK_GROSS_LEVERAGE,)
    assert _refused_for(_tight(constitution, firm_full)) == (RejectionReason.FIRM_GROSS_LEVERAGE,)


def test_a_size_within_leverage_is_not_resized(constitution) -> None:
    assert _approved(_evaluate(constitution, PortfolioState.flat(BOOKS))).verdict == "APPROVED"


# --- the re-check order submit runs on a decision made earlier ------------------------------


def _decision(constitution: Constitution) -> ExecutableRiskDecision:
    return _approved(_evaluate(constitution, PortfolioState.flat(BOOKS)))


def _recheck(
    constitution: Constitution,
    decision: RiskDecision,
    state: PortfolioState,
    *,
    price: str = "1.10000",
    book_id: str = "fx_scalp",
    firm_equity: str = "100000",
) -> RiskDecision:
    return _engine(constitution).recheck_portfolio(
        decision,
        book_id=book_id,
        side=Side.BUY,
        contract=_contract(),
        firm_equity=Decimal(firm_equity),
        price=Decimal(price),
        portfolio=state,
    )


def test_a_clean_portfolio_returns_the_decision_unchanged(constitution) -> None:
    decision = _decision(constitution)

    assert _recheck(constitution, decision, PortfolioState.flat(BOOKS)) is decision


def test_a_rejected_decision_is_returned_as_it_came(constitution) -> None:
    rejected = _refused_decision(constitution)

    assert _recheck(constitution, rejected, PortfolioState.flat(BOOKS)) is rejected


def _refused_decision(constitution: Constitution) -> RejectedRiskDecision:
    decision = _evaluate(constitution, _state(unmeasured=1))
    assert isinstance(decision, RejectedRiskDecision)
    return decision


def test_a_quote_further_from_the_stop_raises_the_risk_rechecked(constitution) -> None:
    """At 1.10100 the 0.25 lots stand 400 ticks above the 1.09700 stop: 100, not 75."""

    decision = _decision(constitution)
    held = _state(exposures=(_exposure(book=None, risk="600"),))
    over = _state(exposures=(_exposure(book=None, risk="600.01"),))

    assert _recheck(constitution, decision, held, price="1.10100") is decision
    assert RejectionReason.SINGLE_INSTRUMENT_RISK in _refused_for(
        _recheck(constitution, decision, over, price="1.10100")
    )


def test_a_quote_toward_the_stop_cannot_lower_the_risk_it_was_sized_for(constitution) -> None:
    decision = _decision(constitution)
    over = _state(exposures=(_exposure(book=None, risk="625.01"),))

    # 200 ticks at 1.09900 would be 50; the decision's own 75 is what counts.
    assert RejectionReason.SINGLE_INSTRUMENT_RISK in _refused_for(
        _recheck(constitution, decision, over, price="1.09900")
    )


def test_a_recheck_refuses_leverage_rather_than_resizing(constitution) -> None:
    full = _state(exposures=(_exposure(notional="280000"),))

    assert _refused_for(_recheck(constitution, _decision(constitution), full)) == (
        RejectionReason.BOOK_GROSS_LEVERAGE,
    )


def test_a_recheck_runs_the_state_gates_as_well(constitution) -> None:
    reasons = _refused_for(_recheck(constitution, _decision(constitution), _state(book_pnl=None)))

    assert reasons == (RejectionReason.PNL_UNAVAILABLE,)


def test_a_recheck_refuses_an_unknown_book_or_no_equity(constitution) -> None:
    decision = _decision(constitution)
    flat = PortfolioState.flat(BOOKS)

    assert _refused_for(_recheck(constitution, decision, flat, book_id="nope")) == (
        RejectionReason.UNKNOWN_BOOK,
    )
    assert _refused_for(_recheck(constitution, decision, flat, firm_equity="0")) == (
        RejectionReason.NON_POSITIVE_EQUITY,
    )
