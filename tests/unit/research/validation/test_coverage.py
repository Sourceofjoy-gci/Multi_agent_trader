"""Phase 8C3: net expectancy, the CPCV 5th percentile, out-of-sample coverage, scenarios.

Every sample is built by hand: a trade is a net P&L and an entry session, so each expected
mean, rank and count is arithmetic on the numbers written in the test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from trading_house.core.errors import StatisticalInputError
from trading_house.research.validation.coverage import (
    cpcv_p5,
    net_expectancy,
    oos_coverage,
    scenario_expectancy,
    trade_subset,
)
from trading_house.research.validation.series import TradeSample

DIGESTS = ("a" * 64,)


def _sample(*trades: tuple[float, str]) -> TradeSample:
    """Each trade is ``(net P&L, entry session)``, one per consecutive day."""

    first = datetime(2024, 1, 1, 9, tzinfo=UTC)
    entries = tuple(first + timedelta(days=i) for i in range(len(trades)))
    exits = tuple(moment + timedelta(hours=25) for moment in entries)  # the next day
    return TradeSample(
        entry_day=tuple(moment.date() for moment in entries),
        exit_day=tuple(moment.date() for moment in exits),
        entry_at=entries,
        exit_at=exits,
        net_pnl=np.array([pnl for pnl, _ in trades], dtype=np.float64),
        session=tuple(name for _, name in trades),
    )


def _one(value: float) -> TradeSample:
    return _sample((value, "london"))


def _p5(values: list[float], *, differ: bool = True):  # type: ignore[no-untyped-def]
    return cpcv_p5(
        [_one(v) for v in values],
        paths_differ=differ,
        closed_trades=1,
        evidence_sha256=DIGESTS,
        basis_is_mark_to_market=True,
    )


# --- net expectancy ------------------------------------------------------------------


def test_expectancy_is_the_mean_net_pnl_per_trade() -> None:
    assert net_expectancy(_sample((1.0, "london"), (2.0, "asian"), (6.0, "london"))) == 3.0
    assert net_expectancy(_sample((-4.0, "london"), (1.0, "london"))) == -1.5


def test_a_sample_with_no_trades_has_no_expectancy() -> None:
    assert net_expectancy(_sample()) is None


def test_an_expectancy_that_overflows_is_refused() -> None:
    with pytest.raises(StatisticalInputError) as error:
        net_expectancy(_sample((1e308, "london"), (1e308, "london")))

    assert "not finite" in str(error.value.__cause__)


def test_a_subset_keeps_the_chosen_trades_in_the_order_given() -> None:
    sample = _sample((1.0, "asian"), (2.0, "london"), (3.0, "new_york"), (4.0, "off"))

    picked = trade_subset(sample, [3, 1])

    assert picked.net_pnl.tolist() == [4.0, 2.0]
    assert picked.session == ("off", "london")
    assert picked.entry_day == (sample.entry_day[3], sample.entry_day[1])
    assert picked.exit_day == (sample.exit_day[3], sample.exit_day[1])
    assert picked.entry_at == (sample.entry_at[3], sample.entry_at[1])
    assert picked.exit_at == (sample.exit_at[3], sample.exit_at[1])
    assert len(trade_subset(sample, [])) == 0


# --- the CPCV 5th percentile ----------------------------------------------------------


def test_five_paths_read_rank_one_the_lowest_expectancy() -> None:
    # ceil(0.05 * 5) = ceil(0.25) = 1: the smallest of [5, 1, 4, 2, 3].
    result = _p5([5.0, 1.0, 4.0, 2.0, 3.0])

    assert result.rank == 1
    assert result.p5.value == 1.0


def test_twenty_paths_read_rank_one() -> None:
    # ceil(0.05 * 20) = ceil(1.0) = 1 (0.05 * 20 is exactly 1.0 in floating point).
    values = [
        float(v) for v in (7, 3, 9, 1, 20, 5, 11, 2, 18, 4, 6, 8, 10, 12, 13, 14, 15, 16, 17, 19)
    ]
    result = _p5(values)

    assert result.rank == 1
    assert result.p5.value == 1.0


def test_twenty_one_paths_read_rank_two() -> None:
    # ceil(0.05 * 21) = ceil(1.05) = 2: the second smallest of 1..21 in scrambled order.
    values = [
        float(v)
        for v in (7, 3, 9, 1, 21, 5, 11, 2, 18, 4, 6, 8, 10, 12, 13, 14, 15, 16, 17, 19, 20)
    ]
    result = _p5(values)

    assert result.rank == 2
    assert result.p5.value == 2.0


def test_the_rank_is_a_ceiling_at_forty_and_forty_one_paths() -> None:
    # 0.05 * 40 = 2.0 -> rank 2 ; 0.05 * 41 = 2.05 -> rank 3, over the values 1..n reversed.
    forty = _p5([float(v) for v in range(40, 0, -1)])
    forty_one = _p5([float(v) for v in range(41, 0, -1)])

    assert (forty.rank, forty.p5.value) == (2, 2.0)
    assert (forty_one.rank, forty_one.p5.value) == (3, 3.0)


def test_negative_expectancies_sort_below_positive_ones() -> None:
    assert _p5([2.0, -3.0, 0.5, -1.0, 4.0]).p5.value == -3.0


def test_a_path_with_no_kept_trade_makes_the_measurement_undefined_and_names_it() -> None:
    result = cpcv_p5(
        [_one(1.0), _one(2.0), _sample(), _one(3.0), _one(4.0)],
        paths_differ=True,
        closed_trades=1,
        evidence_sha256=DIGESTS,
        basis_is_mark_to_market=True,
    )

    assert result.p5.value is None
    assert result.p5.undefined_reason == (
        "CPCV path 2 kept no closed trade, so it has no expectancy"
    )
    assert result.rank is None
    assert [(p.index, p.trades_kept, p.net_expectancy) for p in result.paths] == [
        (0, 1, 1.0),
        (1, 1, 2.0),
        (2, 0, None),
        (3, 1, 3.0),
        (4, 1, 4.0),
    ]


def test_the_first_of_several_empty_paths_is_the_one_named() -> None:
    result = cpcv_p5(
        [_one(1.0), _sample(), _one(2.0), _sample(), _one(3.0)],
        paths_differ=True,
        closed_trades=1,
        evidence_sha256=DIGESTS,
        basis_is_mark_to_market=True,
    )

    assert "CPCV path 1 kept no closed trade" in (result.p5.undefined_reason or "")


def test_no_paths_is_undefined() -> None:
    result = cpcv_p5(
        [],
        paths_differ=False,
        closed_trades=0,
        evidence_sha256=DIGESTS,
        basis_is_mark_to_market=True,
    )

    assert result.p5.value is None
    assert result.p5.undefined_reason == "there are no CPCV paths"
    assert result.paths == ()


def test_identical_paths_are_measured_and_say_they_are_identical() -> None:
    """The amended decision: paths_differ false does NOT make the measurement undefined.
    Five identical paths each have expectancy (2 + 3) / 2 = 2.5, so the 5th percentile is
    2.5, the aggregate out-of-sample expectancy."""

    same = _sample((2.0, "london"), (3.0, "asian"))
    result = cpcv_p5(
        [same] * 5,
        paths_differ=False,
        closed_trades=2,
        evidence_sha256=DIGESTS,
        basis_is_mark_to_market=True,
    )

    assert result.p5.value == 2.5
    assert result.paths_differ is False
    assert [p.net_expectancy for p in result.paths] == [2.5] * 5


def test_the_result_carries_its_quantile_flag_evidence_and_basis() -> None:
    differing = _p5([1.0, 2.0, 3.0, 4.0, 5.0], differ=True)
    realized = cpcv_p5(
        [_one(1.0)] * 5,
        paths_differ=False,
        closed_trades=1,
        evidence_sha256=("x" * 64, "y" * 64),
        basis_is_mark_to_market=False,
    )

    assert differing.paths_differ is True
    assert differing.quantile == 0.05
    assert (differing.p5.name, differing.p5.evidence_sha256) == ("cpcv_p5", DIGESTS)
    assert differing.p5.basis_is_mark_to_market is True
    assert realized.p5.evidence_sha256 == ("x" * 64, "y" * 64)
    assert realized.p5.basis_is_mark_to_market is False


def test_an_undefined_p5_still_carries_evidence_and_basis() -> None:
    result = cpcv_p5(
        [_sample()] * 5,
        paths_differ=True,
        closed_trades=1,
        evidence_sha256=("x" * 64,),
        basis_is_mark_to_market=False,
    )

    assert result.p5.evidence_sha256 == ("x" * 64,)
    assert result.p5.basis_is_mark_to_market is False
    assert result.paths_differ is True


# --- out-of-sample coverage ----------------------------------------------------------


def _coverage(paths: list[TradeSample], labels: tuple[str, ...] = ("london", "asian")):  # type: ignore[no-untyped-def]
    return oos_coverage(
        paths,
        labels,
        closed_trades=max((len(p) for p in paths), default=0),
        evidence_sha256=DIGESTS,
        basis_is_mark_to_market=True,
    )


def _n(count: int, name: str = "london") -> TradeSample:
    return _sample(*[(1.0, name)] * count)


def test_the_out_of_sample_sample_is_the_path_with_the_fewest_kept_trades() -> None:
    result = _coverage([_n(3), _n(2), _n(4)])

    assert result.oos_path_index == 1
    assert result.oos_trades.value == 2.0


def test_ties_for_fewest_take_the_lowest_path_index() -> None:
    assert _coverage([_n(2), _n(2), _n(5)]).oos_path_index == 0
    assert _coverage([_n(4), _n(2), _n(2)]).oos_path_index == 1


def test_regimes_represented_counts_declared_labels_with_a_trade() -> None:
    # Fewest path holds london, london, asian: of (asian, london, new_york) two are present.
    sample = _sample((1.0, "london"), (1.0, "london"), (1.0, "asian"))

    result = _coverage([_n(5), sample], ("asian", "london", "new_york"))

    assert result.regimes_represented.value == 2.0
    assert result.declared_labels == ("asian", "london", "new_york")


def test_a_trade_in_a_session_nobody_declared_is_not_a_represented_regime() -> None:
    sample = _sample((1.0, "london"), (1.0, "new_york"))

    assert _coverage([sample], ("london", "asian")).regimes_represented.value == 1.0


def test_a_single_regime_sample_reports_one_represented() -> None:
    assert _coverage([_n(4)], ("london", "asian")).regimes_represented.value == 1.0


def test_a_label_that_is_not_a_session_makes_the_regimes_undefined_but_not_the_count() -> None:
    result = _coverage([_n(3)], ("trend", "range"))

    assert result.regimes_represented.value is None
    assert result.regimes_represented.undefined_reason == (
        "regime label 'trend' is not a session name, the only classifier available"
    )
    assert result.oos_trades.value == 3.0


def test_every_label_must_be_a_session_name_not_just_one() -> None:
    result = _coverage([_n(3)], ("london", "trend"))

    assert result.regimes_represented.value is None
    assert "'trend'" in (result.regimes_represented.undefined_reason or "")


def test_the_off_session_is_a_session_name() -> None:
    sample = _sample((1.0, "off"), (1.0, "london"))

    assert _coverage([sample], ("off", "london")).regimes_represented.value == 2.0


def test_no_declared_labels_is_undefined_not_zero() -> None:
    result = _coverage([_n(3)], ())

    assert result.regimes_represented.value is None
    assert result.regimes_represented.undefined_reason == "the protocol declares no regime labels"
    assert result.oos_trades.value == 3.0
    assert result.declared_labels == ()


def test_an_empty_sample_has_zero_trades_and_undefined_regimes() -> None:
    result = _coverage([_n(3), _sample()])

    assert result.oos_trades.value == 0.0
    assert result.regimes_represented.value is None
    assert result.regimes_represented.undefined_reason == (
        "the out-of-sample sample holds no trades"
    )


def test_no_paths_leaves_both_undefined() -> None:
    result = _coverage([])

    assert result.oos_trades.value is None
    assert result.regimes_represented.value is None
    assert result.oos_path_index is None


def test_coverage_names_its_evidence_and_basis() -> None:
    result = oos_coverage(
        [_n(2)],
        ("london",),
        closed_trades=2,
        evidence_sha256=("q" * 64,),
        basis_is_mark_to_market=False,
    )

    for item in (result.oos_trades, result.regimes_represented):
        assert item.evidence_sha256 == ("q" * 64,)
        assert item.basis_is_mark_to_market is False
    assert (result.oos_trades.name, result.regimes_represented.name) == (
        "oos_trades",
        "regimes_represented",
    )


# --- scenario expectancy -------------------------------------------------------------


def test_a_scenario_is_the_expectancy_of_the_full_sealed_sample_named_for_its_level() -> None:
    sample = _sample((1.0, "london"), (-3.0, "asian"), (5.0, "london"))

    one_and_a_half = scenario_expectancy(
        sample, Decimal("1.5"), evidence_sha256=DIGESTS, basis_is_mark_to_market=True
    )
    two = scenario_expectancy(
        sample, Decimal(2), evidence_sha256=DIGESTS, basis_is_mark_to_market=True
    )

    assert one_and_a_half.name == "scenario_expectancy_1.5"
    assert two.name == "scenario_expectancy_2.0"
    assert one_and_a_half.value == 1.0  # (1 - 3 + 5) / 3
    assert one_and_a_half.evidence_sha256 == DIGESTS
    assert one_and_a_half.basis_is_mark_to_market is True
    assert "not a locked out-of-sample" in (scenario_expectancy.__doc__ or "")


def test_a_scenario_with_no_trade_or_no_run_is_undefined_with_distinct_reasons() -> None:
    empty = scenario_expectancy(
        _sample(), Decimal("1.5"), evidence_sha256=DIGESTS, basis_is_mark_to_market=False
    )
    absent = scenario_expectancy(
        None, Decimal(2), evidence_sha256=DIGESTS, basis_is_mark_to_market=False
    )

    assert empty.value is None
    assert empty.undefined_reason == (
        "the stressed run closed no trade, so its sealed sample has no expectancy"
    )
    assert absent.value is None
    assert absent.undefined_reason == "no constant-notional run is sealed at 2.0x"
    assert (empty.name, absent.name) == ("scenario_expectancy_1.5", "scenario_expectancy_2.0")
    assert empty.basis_is_mark_to_market is False


# --- how much of the run the measurements hold ----------------------------------------


def test_p5_says_how_many_trades_the_run_closed_and_whether_every_path_kept_them_all() -> None:
    whole = cpcv_p5(
        [_n(3)] * 5,
        paths_differ=False,
        closed_trades=3,
        evidence_sha256=DIGESTS,
        basis_is_mark_to_market=True,
    )
    one_dropped = cpcv_p5(
        [_n(3), _n(3), _n(2), _n(3), _n(3)],
        paths_differ=True,
        closed_trades=3,
        evidence_sha256=DIGESTS,
        basis_is_mark_to_market=True,
    )
    none = cpcv_p5(
        [],
        paths_differ=False,
        closed_trades=0,
        evidence_sha256=DIGESTS,
        basis_is_mark_to_market=True,
    )

    assert (whole.closed_trades, whole.all_trades_kept) == (3, True)
    assert (one_dropped.closed_trades, one_dropped.all_trades_kept) == (3, False)
    assert none.all_trades_kept is False  # no path kept anything


def test_coverage_says_whether_its_sample_is_the_whole_run() -> None:
    whole = oos_coverage(
        [_n(3), _n(3)],
        ("london",),
        closed_trades=3,
        evidence_sha256=DIGESTS,
        basis_is_mark_to_market=True,
    )
    partial = oos_coverage(
        [_n(3), _n(2)],
        ("london",),
        closed_trades=3,
        evidence_sha256=DIGESTS,
        basis_is_mark_to_market=True,
    )
    none = oos_coverage(
        [], ("london",), closed_trades=3, evidence_sha256=DIGESTS, basis_is_mark_to_market=True
    )

    assert (whole.oos_trades.value, whole.closed_trades, whole.all_trades_kept) == (3.0, 3, True)
    assert (partial.oos_trades.value, partial.closed_trades, partial.all_trades_kept) == (
        2.0,
        3,
        False,
    )
    assert (none.closed_trades, none.all_trades_kept) == (3, False)


def test_the_documentation_calls_an_identical_path_sample_in_sample_not_out_of_sample() -> None:
    import inspect

    import trading_house.research.validation.coverage as module

    doc = " ".join((module.__doc__ or "").split())
    field = " ".join(inspect.getsource(module.CpcvP5Result).split())
    assert "the full sealed research-window sample" in doc
    assert "IN-SAMPLE on the research window" in doc
    assert "in-sample on the research window for a fixed-parameter candidate" in field
    assert "not out-of-sample evidence" in field
