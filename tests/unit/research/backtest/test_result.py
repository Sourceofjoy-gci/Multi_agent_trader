from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_house.core.schemas import Side
from trading_house.marketdata.models import Timeframe
from trading_house.research.backtest.costs import CostModel
from trading_house.research.backtest.fills import ExitKind
from trading_house.research.backtest.result import BacktestResult, SimulatedTrade

NOW = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


def _cost_model(**overrides: object) -> CostModel:
    defaults: dict[str, object] = {
        "commission_per_lot_per_side": Decimal("3.50"),
        "slippage_points_per_side": Decimal("1"),
        "swap_long_points_per_day": Decimal("-1"),
        "swap_short_points_per_day": Decimal("-1"),
        "triple_swap_weekday": 2,
    }
    return CostModel(**{**defaults, **overrides})  # type: ignore[arg-type]


def _trade(**overrides: object) -> SimulatedTrade:
    gross_pnl = overrides.pop("gross_pnl", Decimal("100"))
    commission = overrides.pop("commission", Decimal("7"))
    swap = overrides.pop("swap", Decimal("-3"))
    net_pnl = overrides.pop("net_pnl", gross_pnl - commission + swap)  # type: ignore[operator]
    defaults: dict[str, object] = {
        "proposal_id": "p-1",
        "side": Side.BUY,
        "lots": Decimal("0.10"),
        "entry_price": Decimal("1.10000"),
        "entry_at": NOW,
        "exit_price": Decimal("1.10100"),
        "exit_at": NOW + timedelta(minutes=12),
        "exit_kind": ExitKind.TIME,
        "gross_pnl": gross_pnl,
        "commission": commission,
        "swap": swap,
        "net_pnl": net_pnl,
    }
    return SimulatedTrade(**{**defaults, **overrides})  # type: ignore[arg-type]


def _result(**overrides: object) -> BacktestResult:
    trades = overrides.pop("trades", (_trade(),))
    net_pnl = overrides.pop(
        "net_pnl",
        sum((t.net_pnl for t in trades), Decimal(0)),  # type: ignore[union-attr]
    )
    defaults: dict[str, object] = {
        "run_id": "run-1",
        "strategy_id": "strat-1",
        "strategy_version": "v1",
        "instrument_id": "fx.eurusd",
        "timeframe": Timeframe.M1,
        "start": NOW,
        "end": NOW + timedelta(hours=1),
        "firm_equity": Decimal("100000"),
        "cost_model": _cost_model(),
        "trades": trades,
        "rejections": (),
        "bars_seen": 60,
        "net_pnl": net_pnl,
    }
    return BacktestResult(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_net_pnl_is_gross_less_every_cost_term() -> None:
    """The headline number. If it is computed anywhere but from the trades'
    own fields, the result and its trades can disagree and nothing notices."""

    result = _result(
        trades=(
            _trade(gross_pnl=Decimal("100"), commission=Decimal("7"), swap=Decimal("-3")),
            _trade(gross_pnl=Decimal("-40"), commission=Decimal("7"), swap=Decimal("0")),
        )
    )

    assert result.net_pnl == Decimal("43")  # 100-7-3 + (-40-7-0)


def test_a_trade_whose_net_does_not_reconcile_is_refused() -> None:
    """A SimulatedTrade is the audit record of one round trip. One that claims
    a net its own terms do not produce is a bug that would otherwise surface as
    an inexplicable equity curve."""

    with pytest.raises(ValidationError):
        SimulatedTrade(
            proposal_id="p-1",
            side=Side.BUY,
            lots=Decimal("0.10"),
            entry_price=Decimal("1.10000"),
            entry_at=NOW,
            exit_price=Decimal("1.10100"),
            exit_at=NOW + timedelta(minutes=12),
            exit_kind=ExitKind.TIME,
            gross_pnl=Decimal("100"),
            commission=Decimal("7"),
            swap=Decimal("0"),
            net_pnl=Decimal("100"),  # should be 93; the validator must refuse it
        )


def test_the_digest_changes_when_any_cost_input_changes() -> None:
    """Phase 8 hashes this into a trial. Two runs that differ in what they
    assumed costs were must not share a digest, or the trial ledger records a
    Sharpe against the wrong assumptions."""

    base = _result()
    dearer = base.model_copy(
        update={
            "cost_model": base.cost_model.model_copy(
                update={"commission_per_lot_per_side": Decimal("99")}
            )
        }
    )

    assert base.digest() != dearer.digest()


def test_the_digest_is_stable_across_equal_results() -> None:
    assert _result().digest() == _result().digest()


def test_rejections_are_carried_not_counted() -> None:
    """D-8. A count says a strategy was vetoed; the reasons say why, and that is
    what tells you whether its edge lived in trades the constitution forbids."""

    result = _result(rejections=(("spread_blowout", "stale_feed"),))

    assert result.rejections == (("spread_blowout", "stale_feed"),)
