"""The read path: point-in-time visibility, quality filtering, and coverage bounds."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.integration.marketdata.conftest import NINE, _bar, seed
from trading_house.core.errors import CoverageError
from trading_house.marketdata.models import BarQuality, Timeframe
from trading_house.marketdata.store import BarStore

pytestmark = pytest.mark.integration


def test_a_bar_that_had_not_closed_yet_is_invisible(bar_store: BarStore) -> None:
    """THE test of this phase (I-17).

    Bars opening at 09:00, 09:01 and 09:02 become knowable at 09:01, 09:02
    and 09:03. Standing at 09:02 you may see the first two and must not see
    the third -- it has not closed. An implementation that filters on
    event_time instead of availability_time returns all three, and every
    backtest built on it is reading its own future.
    """

    seed(bar_store, [_bar(0), _bar(1), _bar(2)])

    visible = bar_store.bars(
        "fx.eurusd",
        Timeframe.M1,
        start=NINE,
        end=NINE + timedelta(hours=1),
        as_of=NINE + timedelta(minutes=2),
    )

    assert [b.event_time for b in visible] == [NINE, NINE + timedelta(minutes=1)]


def test_a_bar_becomes_visible_exactly_at_its_close(bar_store: BarStore) -> None:
    """The boundary is inclusive. Exclusive would make every bar invisible for
    one tick -- the kind of off-by-one that surfaces months later as an
    inexplicable one-bar lag in a strategy."""

    seed(bar_store, [_bar(0)])

    at_close = bar_store.bars(
        "fx.eurusd",
        Timeframe.M1,
        start=NINE,
        end=NINE + timedelta(hours=1),
        as_of=NINE + timedelta(minutes=1),
    )

    assert len(at_close) == 1


def test_defective_bars_are_absent_by_default_and_present_on_request(
    bar_store: BarStore,
) -> None:
    seed(bar_store, [_bar(0), _bar(1, quality=BarQuality.OHLC_INCOHERENT)])
    window = {
        "start": NINE,
        "end": NINE + timedelta(hours=1),
        "as_of": NINE + timedelta(hours=1),
    }

    assert len(bar_store.bars("fx.eurusd", Timeframe.M1, **window)) == 1
    assert len(bar_store.bars("fx.eurusd", Timeframe.M1, **window, include_defective=True)) == 2


def test_asking_beyond_coverage_raises_rather_than_returning_less(
    bar_store: BarStore,
) -> None:
    """Returning three months when five years were asked for, with a healthy
    exit code, is the exact failure this phase exists to prevent."""

    seed(bar_store, [_bar(0)])

    with pytest.raises(CoverageError):
        bar_store.bars(
            "fx.eurusd",
            Timeframe.M1,
            start=NINE - timedelta(days=365),
            end=NINE + timedelta(hours=1),
            as_of=NINE + timedelta(hours=1),
        )


def test_coverage_reports_what_is_actually_held(bar_store: BarStore) -> None:
    seed(bar_store, [_bar(0), _bar(1), _bar(2, quality=BarQuality.NEGATIVE_SPREAD)])

    coverage = bar_store.coverage("fx.eurusd", Timeframe.M1)

    assert coverage.earliest_event_time == NINE
    assert coverage.latest_event_time == NINE + timedelta(minutes=2)
    assert coverage.clean_bars == 2
    assert coverage.defective_bars == 1


def test_coverage_of_an_empty_key_is_not_an_error(bar_store: BarStore) -> None:
    """Starting from nothing is an ordinary state, and distinct from a short
    read. A caller checking coverage before its first backfill must not get an
    exception for it."""

    coverage = bar_store.coverage("metal.xauusd", Timeframe.H1)

    assert coverage.earliest_event_time is None
    assert coverage.clean_bars == 0


def test_reading_an_entirely_uncovered_key_raises_coverage_error(
    bar_store: BarStore,
) -> None:
    """A key that has never been backfilled has zero coverage, so every
    request against it exceeds what is held. Returning an empty result
    instead would be indistinguishable from 'the market was closed for the
    entire requested window' -- exactly the silent-truncation failure this
    guarantee exists to prevent, just relocated to the boundary where
    nothing has been backfilled yet. A caller that only wants to probe a
    fresh key should call ``coverage()``, not ``bars()``."""

    with pytest.raises(CoverageError):
        bar_store.bars(
            "metal.xauusd",
            Timeframe.H1,
            start=NINE - timedelta(days=365),
            end=NINE + timedelta(hours=1),
            as_of=NINE + timedelta(hours=1),
        )


def test_include_defective_still_respects_point_in_time(bar_store: BarStore) -> None:
    """Asking to see defective bars too must not also waive the
    point-in-time guarantee -- a caller inspecting bad data must not thereby
    be able to see the future. One clean and one defective bar have closed
    by ``as_of``; one clean and one defective bar have not."""

    as_of = NINE + timedelta(minutes=2)
    seed(
        bar_store,
        [
            _bar(0),
            _bar(1, quality=BarQuality.OHLC_INCOHERENT),
            _bar(2),
            _bar(3, quality=BarQuality.OHLC_INCOHERENT),
        ],
    )

    visible = bar_store.bars(
        "fx.eurusd",
        Timeframe.M1,
        start=NINE,
        end=NINE + timedelta(hours=1),
        as_of=as_of,
        include_defective=True,
    )

    assert [b.event_time for b in visible] == [NINE, NINE + timedelta(minutes=1)]
