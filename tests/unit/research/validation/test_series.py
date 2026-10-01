"""Phase 8C1: the validated statistical input.

Every refusal has its own case, and each case asserts the *private cause* as well as
the typed error: ``MIN_SERIES_DAYS`` alone would refuse an empty series, so a test
that only expected "some StatisticalInputError" would stay green with the emptiness
clause deleted.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from tests.unit.research.test_evidence import _bundle, _with
from trading_house.core.errors import StatisticalInputError
from trading_house.core.schemas import Side
from trading_house.research.backtest.fills import ExitKind
from trading_house.research.backtest.result import SimulatedTrade
from trading_house.research.evidence import DailyReturnPoint, EvidenceBundle
from trading_house.research.trial_ledger import ReturnSeriesBasis
from trading_house.research.validation.series import MIN_SERIES_DAYS, ReturnSeries, TradeSample

FIRST = date(2026, 1, 1)
DIGEST = "e" * 64


def _days(count: int, first: date = FIRST) -> tuple[date, ...]:
    return tuple(first + timedelta(days=i) for i in range(count))


def _points(values: list[Decimal], first: date = FIRST) -> tuple[DailyReturnPoint, ...]:
    return tuple(
        DailyReturnPoint(day=day, value=value)
        for day, value in zip(_days(len(values), first), values, strict=True)
    )


def _with_returns(points: tuple[DailyReturnPoint, ...]) -> EvidenceBundle:
    return _with(_bundle(), daily_returns=points)


def _with_basis(bundle: EvidenceBundle, basis: ReturnSeriesBasis) -> EvidenceBundle:
    """The basis flipped without revalidation: a real MARK_TO_MARKET bundle needs a
    whole equity series, and ``from_bundle`` only reads the basis."""

    return bundle.model_copy(update={"return_series_basis": basis})


def _refused(error: pytest.ExceptionInfo[StatisticalInputError], fragment: str) -> None:
    assert str(error.value) == StatisticalInputError.public_message
    cause = error.value.__cause__
    assert isinstance(cause, ValueError)
    assert fragment in str(cause)


def _flat(count: int = MIN_SERIES_DAYS) -> list[Decimal]:
    return [Decimal("0.001")] * count


def _series(**changes: object) -> ReturnSeries:
    fields: dict[str, object] = {
        "days": _days(MIN_SERIES_DAYS),
        "values": np.full(MIN_SERIES_DAYS, 0.001, dtype=np.float64),
        "basis": ReturnSeriesBasis.REALIZED_CLOSED_TRADES,
        "evidence_sha256": DIGEST,
    }
    return ReturnSeries(**{**fields, **changes})  # type: ignore[arg-type]


# --- ReturnSeries -------------------------------------------------------------


def test_a_real_bundle_becomes_a_series_with_its_own_days_values_basis_and_digest() -> None:
    values = [Decimal(n) / Decimal(1000) for n in range(-15, 15)]
    bundle = _with_returns(_points(values))

    series = ReturnSeries.from_bundle(bundle, DIGEST)

    assert series.days == _days(30)
    assert series.values.tolist() == [n / 1000 for n in range(-15, 15)]
    assert series.values.dtype == np.float64
    assert series.basis is ReturnSeriesBasis.REALIZED_CLOSED_TRADES
    assert series.evidence_sha256 == DIGEST


def test_decimal_becomes_the_nearest_float_and_nothing_else() -> None:
    odd = [Decimal("0.1"), Decimal("-0.3333333333333333333333"), Decimal("1E-7")]
    bundle = _with_returns(_points(odd + _flat(MIN_SERIES_DAYS - len(odd))))

    series = ReturnSeries.from_bundle(bundle, DIGEST)

    assert series.values[:3].tolist() == [0.1, -0.3333333333333333, 1e-07]
    assert series.values[3] == 0.001


def test_the_values_cannot_be_written_to() -> None:
    series = ReturnSeries.from_bundle(_with_returns(_points(_flat())), DIGEST)

    with pytest.raises(ValueError, match="read-only"):
        series.values[0] = 1.0


@pytest.mark.parametrize(
    ("basis", "grade"),
    [
        (ReturnSeriesBasis.MARK_TO_MARKET, True),
        (ReturnSeriesBasis.REALIZED_CLOSED_TRADES, False),
    ],
)
def test_only_the_mark_to_market_basis_is_promotion_grade(
    basis: ReturnSeriesBasis, grade: bool
) -> None:
    bundle = _with_basis(_with_returns(_points(_flat())), basis)

    assert ReturnSeries.from_bundle(bundle, DIGEST).promotion_grade is grade


def test_a_missing_basis_is_refused() -> None:
    bundle = _with_basis(_with_returns(_points(_flat())), ReturnSeriesBasis.MISSING)

    with pytest.raises(StatisticalInputError) as error:
        ReturnSeries.from_bundle(bundle, DIGEST)

    _refused(error, "basis is missing")


def test_an_empty_series_is_refused_as_empty_and_not_as_short() -> None:
    with pytest.raises(StatisticalInputError) as error:
        ReturnSeries.from_bundle(_with_returns(()), DIGEST)

    _refused(error, "is empty")


@pytest.mark.parametrize("offset", [0, -3], ids=["repeated day", "earlier day"])
def test_days_that_do_not_increase_are_refused(offset: int) -> None:
    points = list(_points(_flat()))
    repeated = points[10].day + timedelta(days=offset)
    points[11] = DailyReturnPoint(day=repeated, value=Decimal("0.001"))

    with pytest.raises(StatisticalInputError) as error:
        ReturnSeries.from_bundle(_with_returns(tuple(points)), DIGEST)

    _refused(error, "not strictly increasing")


def test_a_gap_in_the_days_is_refused_as_a_gap() -> None:
    points = list(_points(_flat()))
    points[12] = DailyReturnPoint(day=points[12].day + timedelta(days=1), value=Decimal("0.001"))

    with pytest.raises(StatisticalInputError) as error:
        ReturnSeries.from_bundle(_with_returns(tuple(points)), DIGEST)

    _refused(error, "not contiguous")


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "1E+400"])
def test_a_value_that_is_not_finite_as_a_float_is_refused(bad: str) -> None:
    points = list(_points(_flat()))
    points[5] = DailyReturnPoint.model_construct(day=points[5].day, value=Decimal(bad))

    with pytest.raises(StatisticalInputError) as error:
        ReturnSeries.from_bundle(_with_returns(tuple(points)), DIGEST)

    _refused(error, "must be finite")


def test_a_signalling_nan_that_cannot_be_converted_is_refused() -> None:
    points = list(_points(_flat()))
    points[5] = DailyReturnPoint.model_construct(day=points[5].day, value=Decimal("sNaN"))

    with pytest.raises(StatisticalInputError) as error:
        ReturnSeries.from_bundle(_with_returns(tuple(points)), DIGEST)

    _refused(error, "signaling NaN")


def test_the_series_must_hold_at_least_thirty_days() -> None:
    assert MIN_SERIES_DAYS == 30

    with pytest.raises(StatisticalInputError) as error:
        ReturnSeries.from_bundle(_with_returns(_points(_flat(29))), DIGEST)
    _refused(error, "holds 29 days; at least 30")

    assert len(ReturnSeries.from_bundle(_with_returns(_points(_flat(30))), DIGEST).days) == 30


@pytest.mark.parametrize(
    ("values", "fragment"),
    [
        (np.zeros((MIN_SERIES_DAYS, 1)), "one-dimensional"),
        (np.zeros(MIN_SERIES_DAYS, dtype=np.float32), "float64"),
        (np.zeros(MIN_SERIES_DAYS + 1), "one value per entry"),
        (np.full(MIN_SERIES_DAYS, np.nan), "must be finite"),
    ],
    ids=["two-dimensional", "float32", "length mismatch", "non-finite array"],
)
def test_a_hand_built_series_is_held_to_the_same_shape(values: np.ndarray, fragment: str) -> None:
    with pytest.raises(StatisticalInputError) as error:
        _series(values=values)

    _refused(error, fragment)


# --- TradeSample --------------------------------------------------------------


def _trade(entry: datetime, exit_: datetime, net: str, proposal: str = "p") -> SimulatedTrade:
    return SimulatedTrade(
        proposal_id=proposal,
        side=Side.BUY,
        lots=Decimal("0.10"),
        entry_price=Decimal("1.1"),
        entry_at=entry,
        exit_price=Decimal("1.2"),
        exit_at=exit_,
        exit_kind=ExitKind.TIME,
        gross_pnl=Decimal(net),
        commission=Decimal(0),
        swap=Decimal(0),
        net_pnl=Decimal(net),
    )


def _with_trades(*trades: SimulatedTrade) -> EvidenceBundle:
    bundle = _bundle()
    result = bundle.result.model_copy(
        update={"trades": tuple(trades), "net_pnl": sum((t.net_pnl for t in trades), Decimal(0))}
    )
    return bundle.model_copy(update={"result": result})


def _at(day: int, hour: int) -> datetime:
    return datetime(2026, 3, day, hour, 0, tzinfo=UTC)


def test_the_unit_bundles_own_trade_is_a_sample() -> None:
    sample = TradeSample.from_bundle(_bundle())

    assert len(sample) == 1
    assert sample.net_pnl.tolist() == [90.0]
    assert sample.session == ("london",)
    assert sample.entry_day == sample.exit_day == (date(2026, 9, 21),)
    assert sample.entry_at == (datetime(2026, 9, 21, 9, 0, tzinfo=UTC),)
    assert sample.exit_at == (datetime(2026, 9, 21, 9, 12, tzinfo=UTC),)


def test_every_column_is_read_from_its_own_trade_in_order() -> None:
    trades = (
        _trade(_at(1, 3), _at(1, 5), "10.5"),
        _trade(_at(2, 9), _at(2, 10), "-2.25"),
        _trade(_at(3, 14), _at(3, 15), "0"),
        _trade(_at(4, 18), _at(4, 20), "7"),
        _trade(_at(5, 23), _at(6, 2), "-1"),
    )

    sample = TradeSample.from_bundle(_with_trades(*trades))

    assert sample.session == ("asian", "london", "london", "new_york", "off")
    assert sample.net_pnl.tolist() == [10.5, -2.25, 0.0, 7.0, -1.0]
    assert sample.entry_day == tuple(date(2026, 3, d) for d in (1, 2, 3, 4, 5))
    assert sample.exit_day == tuple(date(2026, 3, d) for d in (1, 2, 3, 4, 6))
    assert sample.net_pnl.dtype == np.float64


def test_zero_trades_is_an_empty_sample_and_not_an_error() -> None:
    sample = TradeSample.from_bundle(_with_trades())

    assert len(sample) == 0
    assert sample.net_pnl.shape == (0,)
    assert sample.session == ()


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "1E+400"])
def test_a_trade_pnl_that_is_not_finite_is_refused(bad: str) -> None:
    broken = SimulatedTrade.model_construct(
        **{**_trade(_at(1, 9), _at(1, 10), "1").__dict__, "net_pnl": Decimal(bad)}
    )

    with pytest.raises(StatisticalInputError) as error:
        TradeSample.from_bundle(_with_trades(_trade(_at(1, 9), _at(1, 10), "1"), broken))

    _refused(error, "must be finite")


def test_a_trade_that_exits_before_it_enters_is_refused() -> None:
    with pytest.raises(StatisticalInputError) as error:
        TradeSample.from_bundle(_with_trades(_trade(_at(2, 9), _at(1, 9), "1")))

    _refused(error, "exits before it enters")


def test_a_trade_that_exits_the_instant_it_enters_is_accepted() -> None:
    assert len(TradeSample.from_bundle(_with_trades(_trade(_at(2, 9), _at(2, 9), "1")))) == 1


def test_columns_of_different_lengths_are_refused() -> None:
    with pytest.raises(StatisticalInputError) as error:
        TradeSample(
            entry_day=(date(2026, 3, 1),),
            exit_day=(),
            entry_at=(_at(1, 9),),
            exit_at=(_at(1, 10),),
            net_pnl=np.array([1.0]),
            session=("london",),
        )

    _refused(error, "differ in length")


def test_a_pnl_array_of_the_wrong_length_is_refused() -> None:
    with pytest.raises(StatisticalInputError) as error:
        TradeSample(
            entry_day=(date(2026, 3, 1),),
            exit_day=(date(2026, 3, 1),),
            entry_at=(_at(1, 9),),
            exit_at=(_at(1, 10),),
            net_pnl=np.array([1.0, 2.0]),
            session=("london",),
        )

    _refused(error, "one value per entry")
