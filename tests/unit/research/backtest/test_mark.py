from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction

import pytest
from pydantic import ValidationError

from trading_house.core.errors import EquityEvidenceError
from trading_house.core.exits import NoExitPolicy
from trading_house.core.schemas import Side
from trading_house.marketdata.models import Timeframe
from trading_house.research.backtest.costs import CostModel
from trading_house.research.backtest.fills import ExitKind
from trading_house.research.backtest.mark import (
    BacktestOutcome,
    EquityObservation,
    EquitySeries,
    derive_daily_returns,
)
from trading_house.research.backtest.result import BacktestResult, SimulatedTrade

_FIRM = Decimal("100000")
_NOW = datetime(2026, 9, 27, 9, 0, tzinfo=UTC)


def _point(
    *,
    minutes: int = 0,
    realized: Decimal = Decimal(0),
    unrealized: Decimal = Decimal(0),
    open_: int = 0,
) -> EquityObservation:
    return EquityObservation(
        marked_at=_NOW + timedelta(minutes=minutes),
        equity=_FIRM + realized + unrealized,
        cumulative_realized_pnl=realized,
        unrealized_pnl=unrealized,
        open_positions=open_,
    )


def _series(*points: EquityObservation) -> EquitySeries:
    return EquitySeries(firm_equity=_FIRM, observations=points or (_point(),))


def test_every_point_must_satisfy_the_mark_identity() -> None:
    good = _point(realized=Decimal("10"), unrealized=Decimal("-4"))
    assert good.equity == _FIRM + Decimal("6")

    with pytest.raises(ValidationError, match="firm equity"):
        EquitySeries(
            firm_equity=_FIRM, observations=(good.model_copy(update={"equity": Decimal("999")}),)
        )


def test_a_point_with_nothing_open_cannot_carry_unrealized_pnl() -> None:
    # The premise ``BacktestOutcome``'s flat branch reasons from and cannot see:
    # the mark identity reduces to ``equity == firm_equity + net_pnl`` only
    # because the unrealized term is zero, so a stale unrealized left on the last
    # bar would book a final-day return no trade produced.
    with pytest.raises(ValidationError, match="nothing open"):
        _series(_point(realized=Decimal("10"), unrealized=Decimal("5")))

    # The same two numbers with the position that owns the unrealized is fine.
    held = _series(_point(realized=Decimal("10"), unrealized=Decimal("5"), open_=1))
    assert held.observations[-1].unrealized_pnl == Decimal("5")
    assert held.is_flat is False


def test_observations_must_move_forward_and_never_repeat() -> None:
    with pytest.raises(ValidationError, match="strictly increasing"):
        _series(_point(minutes=5), _point(minutes=5))

    with pytest.raises(ValidationError, match="strictly increasing"):
        _series(_point(minutes=5), _point(minutes=1))


def test_an_empty_series_is_refused() -> None:
    # Spelled out rather than going through _series(), whose ``or (_point(),)``
    # default exists for the tests that want a populated series and would hand
    # this one a point instead of the empty tuple it is testing.
    with pytest.raises(ValidationError, match="at least one observation"):
        EquitySeries(firm_equity=_FIRM, observations=())


def test_is_flat_reads_the_final_open_position_count_not_a_stored_flag() -> None:
    assert _series(_point(), _point(minutes=15, open_=1)).is_flat is False
    assert _series(_point(), _point(minutes=15)).is_flat is True


def test_daily_returns_are_rectangular_across_the_requested_range() -> None:
    # Two marks on the first and last day of the range, so the days between
    # them have no mark and must still appear. Building the marks from _NOW
    # would place them in 2026 and silently make every requested day a
    # carried-forward zero, which is rectangular for the wrong reason.
    series = _series(
        EquityObservation(
            marked_at=datetime(2024, 1, 1, 21, 0, tzinfo=UTC),
            equity=_FIRM + Decimal("100"),
            cumulative_realized_pnl=Decimal("100"),
            unrealized_pnl=Decimal(0),
            open_positions=0,
        ),
        EquityObservation(
            marked_at=datetime(2024, 1, 4, 21, 0, tzinfo=UTC),
            equity=_FIRM + Decimal("400"),
            cumulative_realized_pnl=Decimal("400"),
            unrealized_pnl=Decimal(0),
            open_positions=0,
        ),
    )

    points = derive_daily_returns(series, first_day=date(2024, 1, 1), last_day=date(2024, 1, 4))

    assert [point.day.isoformat() for point in points] == [
        "2024-01-01",
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
    ]
    # The interior days carry the prior close forward rather than being absent.
    assert points[1].value == Decimal(0)
    assert points[3].value == Decimal("300") / (_FIRM + Decimal("100"))


def test_a_day_with_no_mark_carries_the_prior_close_and_returns_zero() -> None:
    series = _series(
        EquityObservation(
            marked_at=datetime(2024, 1, 1, 21, 0, tzinfo=UTC),
            equity=_FIRM + Decimal("100"),
            cumulative_realized_pnl=Decimal("100"),
            unrealized_pnl=Decimal(0),
            open_positions=0,
        )
    )

    points = derive_daily_returns(series, first_day=date(2024, 1, 1), last_day=date(2024, 1, 3))

    assert points[0].value == Decimal("100") / _FIRM
    assert points[1].value == Decimal(0)
    assert points[2].value == Decimal(0)


def test_the_first_day_denominator_is_initial_firm_equity_and_later_ones_the_prior_close() -> None:
    series = _series(
        EquityObservation(
            marked_at=datetime(2024, 1, 1, 21, 0, tzinfo=UTC),
            equity=_FIRM * 2,
            cumulative_realized_pnl=_FIRM,
            unrealized_pnl=Decimal(0),
            open_positions=0,
        )
    )

    points = derive_daily_returns(series, first_day=date(2024, 1, 1), last_day=date(2024, 1, 2))

    assert points[0].value == Decimal(1)
    assert points[1].value == Decimal(0)


def test_a_non_positive_prior_close_is_refused_rather_than_defaulted() -> None:
    series = _series(
        EquityObservation(
            marked_at=datetime(2024, 1, 1, 21, 0, tzinfo=UTC),
            equity=Decimal(0),
            cumulative_realized_pnl=-_FIRM,
            unrealized_pnl=Decimal(0),
            open_positions=0,
        )
    )

    with pytest.raises(EquityEvidenceError):
        derive_daily_returns(series, first_day=date(2024, 1, 1), last_day=date(2024, 1, 2))


def test_a_one_day_range_ending_on_a_zero_close_is_refused_not_reported_as_minus_one() -> None:
    # Both shapes end refused, but by different checks and for different
    # reasons. A range that still has a day left refuses in-loop, on the next
    # day's denominator. A range whose LAST day closes at zero has no next day
    # to be caught by, so it reaches the post-loop check -- which is also the
    # shape ``legacy_import.derive_realized_daily_returns`` refuses. Without
    # that check this returned a clean -100% for a wiped-out account.
    series = _series(
        EquityObservation(
            marked_at=datetime(2024, 1, 1, 21, 0, tzinfo=UTC),
            equity=Decimal(0),
            cumulative_realized_pnl=-_FIRM,
            unrealized_pnl=Decimal(0),
            open_positions=0,
        )
    )

    with pytest.raises(EquityEvidenceError):
        derive_daily_returns(series, first_day=date(2024, 1, 1), last_day=date(2024, 1, 1))


def test_a_range_with_no_marks_at_all_is_an_all_zero_rectangle_rather_than_a_refusal() -> None:
    # Deliberate, and the docstring says so: an untraded period and a flat period
    # are the same measurement, so the honest answer is the zero rectangle. This
    # is the line that keeps a later reader from "fixing" it into a refusal.
    series = _series(
        EquityObservation(
            marked_at=datetime(2024, 1, 1, 21, 0, tzinfo=UTC),
            equity=_FIRM + Decimal("100"),
            cumulative_realized_pnl=Decimal("100"),
            unrealized_pnl=Decimal(0),
            open_positions=0,
        )
    )

    points = derive_daily_returns(series, first_day=date(2024, 6, 1), last_day=date(2024, 6, 3))

    assert [point.value for point in points] == [Decimal(0), Decimal(0), Decimal(0)]


def test_a_range_that_runs_backwards_is_refused_rather_than_returned_as_nothing() -> None:
    with pytest.raises(EquityEvidenceError):
        derive_daily_returns(_series(), first_day=date(2024, 1, 2), last_day=date(2024, 1, 1))


def _cost_model() -> CostModel:
    return CostModel(
        commission_per_lot_per_side=Decimal("3.50"),
        slippage_points_per_side=Decimal("1"),
        swap_long_points_per_day=Decimal("-1"),
        swap_short_points_per_day=Decimal("-1"),
        triple_swap_weekday=2,
    )


def _result(*, bars_seen: int, net_pnl: Decimal) -> BacktestResult:
    trade = SimulatedTrade(
        proposal_id="p-1",
        side=Side.BUY,
        lots=Decimal("0.10"),
        entry_price=Decimal("1.10000"),
        entry_at=datetime(2024, 1, 1, 20, 0, tzinfo=UTC),
        exit_price=Decimal("1.10100"),
        exit_at=datetime(2024, 1, 1, 21, 0, tzinfo=UTC),
        exit_kind=ExitKind.TIME,
        gross_pnl=Decimal("100"),
        commission=Decimal("7"),
        swap=Decimal("-3"),
        net_pnl=net_pnl,
    )
    return BacktestResult(
        run_id="run-1",
        strategy_id="strat-1",
        strategy_version="v1",
        exit_policy=NoExitPolicy(kind="none"),
        constitution_sha256="a" * 64,
        contract_sha256="b" * 64,
        instrument_id="fx.eurusd",
        timeframe=Timeframe.M1,
        start=datetime(2024, 1, 1, 20, 0, tzinfo=UTC),
        end=datetime(2024, 1, 1, 21, 0, tzinfo=UTC),
        firm_equity=_FIRM,
        cost_model=_cost_model(),
        atr_period=14,
        spread_window=20,
        defective_bar_tolerance=Fraction(0),
        trades=(trade,),
        rejections=(),
        bars_seen=bars_seen,
        snapshots_skipped=0,
        net_pnl=net_pnl,
    )


def _outcome_observation(
    *,
    realized: Decimal,
    unrealized: Decimal = Decimal(0),
    minutes: int = 0,
    open_: int = 0,
) -> EquityObservation:
    return EquityObservation(
        marked_at=datetime(2024, 1, 1, 21, 0, tzinfo=UTC) + timedelta(minutes=minutes),
        equity=_FIRM + realized + unrealized,
        cumulative_realized_pnl=realized,
        unrealized_pnl=unrealized,
        open_positions=open_,
    )


def test_the_outcome_cross_checks_the_series_against_the_result_it_came_from() -> None:
    """The three assertions ``result.py`` could not make without a curve. Each
    is a disagreement the pair could otherwise carry silently, so each is
    refused rather than reconciled."""

    result = _result(bars_seen=1, net_pnl=Decimal("90"))
    outcome = BacktestOutcome(
        result=result, equity=_series(_outcome_observation(realized=Decimal("90")))
    )
    assert outcome.equity.is_flat is True

    with pytest.raises(ValidationError, match="one firm equity"):
        BacktestOutcome(
            result=result,
            equity=EquitySeries(
                firm_equity=_FIRM * 2,
                # Rebased with the series, so the only thing wrong here is the
                # disagreement with the result: a series that broke its own
                # identity first would be refused for that reason instead.
                observations=(
                    EquityObservation(
                        marked_at=datetime(2024, 1, 1, 21, 0, tzinfo=UTC),
                        equity=_FIRM * 2 + Decimal("90"),
                        cumulative_realized_pnl=Decimal("90"),
                        unrealized_pnl=Decimal(0),
                        open_positions=0,
                    ),
                ),
            ),
        )

    with pytest.raises(ValidationError, match="one observation per processed bar"):
        BacktestOutcome(
            result=result,
            equity=_series(
                _outcome_observation(realized=Decimal("90")),
                _outcome_observation(realized=Decimal("90"), minutes=15),
            ),
        )

    with pytest.raises(ValidationError, match="net PnL"):
        # Same result, so the disagreement is the series's closing realized
        # total: 50 against a result whose own trade sums to 90.
        BacktestOutcome(
            result=result,
            equity=_series(_outcome_observation(realized=Decimal("50"))),
        )


def test_the_final_realized_total_is_reconciled_against_the_result_only_when_flat() -> None:
    """Both halves of ``BacktestOutcome``'s single condition, because the
    condition is the only thing that lets a run ending in a discarded open
    position exist honestly at all.

    The non-flat half is the load-bearing one and was previously unproven: it is
    what notices if the ``is_flat`` guard is deleted, and without it nothing in
    the suite would say the guard is load-bearing.
    """

    result = _result(bars_seen=1, net_pnl=Decimal("90"))

    # Ends holding one position worth 40 unrealized, so its closing realized
    # total of 50 falls short of the result's 90 on purpose: the difference is
    # the open position's, and the run is discarded rather than reconciled, so
    # there is nothing left to check it against.
    outcome = BacktestOutcome(
        result=result,
        equity=_series(
            _outcome_observation(realized=Decimal("50"), unrealized=Decimal("40"), open_=1)
        ),
    )
    assert outcome.equity.is_flat is False

    # The same closing realized total against the same result, with the
    # position closed: now the two numbers must be one number, and a flat run
    # cannot carry the unrealized 40 that reconciled the other series.
    with pytest.raises(ValidationError, match="net PnL"):
        BacktestOutcome(
            result=result,
            equity=_series(_outcome_observation(realized=Decimal("50"))),
        )
