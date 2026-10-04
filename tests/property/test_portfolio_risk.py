"""Phase 10: whatever is already open, lost or sent, an order the engine admits
cannot be the one that takes the book or the firm through a signed limit.

The unit tests pin each boundary by hand. This states the ceilings themselves,
over generated portfolios, against the real signed constitution.
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from tests.unit.risk.conftest import (
    BOOKS,
    CONFIG_DIR,
    NOW,
    _contract,
    _facts,
    _proposal,
)
from trading_house.constitution.loader import load_constitution
from trading_house.core.clock import FixedClock
from trading_house.core.schemas import ExecutableRiskDecision
from trading_house.risk.engine import RiskEngine
from trading_house.risk.portfolio import (
    BookPnl,
    OpenExposure,
    PortfolioState,
    cluster_keys,
    notional_money,
)

CONSTITUTION = load_constitution(
    CONFIG_DIR / "risk_constitution.yaml",
    CONFIG_DIR / "risk_constitution.yaml.sig",
    CONFIG_DIR / "risk_constitution.public.pem",
).constitution
FIRM_EQUITY = Decimal(100000)
HUNDRED = Decimal(100)

INSTRUMENTS = {
    "fx.eurusd": frozenset({"ccy:EUR", "ccy:USD"}),
    "fx.gbpusd": frozenset({"ccy:GBP", "ccy:USD"}),
    "fx.eurgbp": frozenset({"ccy:EUR", "ccy:GBP"}),
    "metal.xauusd": frozenset({"ccy:XAU", "ccy:USD"}),
}

money = st.decimals(min_value=0, max_value=2000, places=2)
signed = st.decimals(min_value=-1000, max_value=1000, places=2)


@st.composite
def exposures(draw: st.DrawFn) -> OpenExposure:
    instrument = draw(st.sampled_from(sorted(INSTRUMENTS)))
    return OpenExposure(
        book=draw(st.sampled_from([*BOOKS, None])),
        instrument_id=instrument,
        cluster_keys=INSTRUMENTS[instrument],
        risk_money=draw(money),
        notional_money=draw(st.decimals(min_value=0, max_value=300000, places=0)),
    )


@st.composite
def portfolios(draw: st.DrawFn) -> PortfolioState:
    return PortfolioState(
        exposures=tuple(draw(st.lists(exposures(), max_size=3))),
        unmeasured_positions=0,
        book_pnl={
            book: BookPnl(
                realized_today=draw(signed), unrealized=draw(signed), drawdown=draw(money)
            )
            for book in BOOKS
        },
        firm_drawdown=draw(st.decimals(min_value=0, max_value=10000, places=2)),
        order_stamps=(),
        consecutive_rejects=0,
    )


@settings(max_examples=300)
@given(state=portfolios(), invalidation_ticks=st.integers(min_value=10, max_value=600))
def test_an_admitted_order_never_breaches_a_portfolio_ceiling(
    state: PortfolioState, invalidation_ticks: int
) -> None:
    proposal = _proposal(
        invalidation_price=Decimal("1.10000") - Decimal("0.00001") * invalidation_ticks
    )
    contract = _contract()
    decision = RiskEngine(CONSTITUTION, FixedClock(NOW)).evaluate(
        proposal,
        contract=contract,
        **_facts(
            portfolio=state,
            atr=Decimal(0),
            median_spread_points=Decimal(0),
            tick_spread_points=Decimal(0),
        ),
    )
    if not isinstance(decision, ExecutableRiskDecision):
        return

    book_id = proposal.book
    book = CONSTITUTION.books[book_id]
    firm = CONSTITUTION.firm
    book_equity = FIRM_EQUITY * book.capital_fraction
    risk = decision.risk_money
    notional = notional_money(decision.approved_quantity.amount, proposal.entry_price_ref, contract)
    assert state.book_pnl is not None
    pnl = state.book_pnl[book_id]

    assert (pnl.loss_today() + state.open_risk(book=book_id) + risk) * HUNDRED <= (
        book.daily_loss_stop_pct * book_equity
    )
    assert (pnl.drawdown + state.open_risk(book=book_id) + risk) * HUNDRED <= (
        book.max_drawdown_halt_pct * book_equity
    )
    assert state.firm_drawdown is not None
    assert (state.firm_drawdown + state.open_risk() + risk) * HUNDRED <= (
        firm.max_total_drawdown_halt_pct * FIRM_EQUITY
    )
    assert (state.open_risk() + risk) * HUNDRED <= firm.max_aggregate_open_risk_pct * FIRM_EQUITY
    assert (state.instrument_risk(proposal.instrument_id) + risk) * HUNDRED <= (
        firm.max_single_instrument_risk_pct * FIRM_EQUITY
    )
    for key in cluster_keys(contract):
        assert (state.cluster_risk(key) + risk) * HUNDRED <= (
            firm.max_correlated_cluster_risk_pct * FIRM_EQUITY
        )
    assert state.notional(book=book_id) + notional <= book.max_gross_leverage * book_equity
    assert state.notional() + notional <= firm.max_gross_leverage * FIRM_EQUITY
    assert len(state.book_exposures(book_id)) < book.max_concurrent_positions
