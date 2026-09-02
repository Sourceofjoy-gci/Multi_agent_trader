from datetime import UTC, datetime, timedelta

import pytest

from trading_house.marketdata.models import Timeframe, duration
from trading_house.marketdata.paging import PAGE_BARS, plan_backward


def test_a_short_range_is_one_page() -> None:
    newest = datetime(2026, 8, 25, tzinfo=UTC)
    pages = plan_backward(Timeframe.H1, newest=newest, oldest=newest - timedelta(days=1))

    assert len(pages) == 1
    assert pages[0] == (newest - timedelta(days=1), newest)


def test_pages_are_returned_newest_first() -> None:
    """Backfill walks backwards from what it already has."""

    newest = datetime(2026, 8, 25, tzinfo=UTC)
    pages = plan_backward(Timeframe.M1, newest=newest, oldest=newest - timedelta(days=90))

    assert pages[0][1] == newest
    assert all(pages[i][0] >= pages[i + 1][1] for i in range(len(pages) - 1))


def test_pages_tile_the_range_with_no_gap_and_no_overlap() -> None:
    """A hole here is a permanently missing week nobody notices."""

    newest = datetime(2026, 8, 25, tzinfo=UTC)
    oldest = newest - timedelta(days=90)
    pages = plan_backward(Timeframe.M1, newest=newest, oldest=oldest)

    assert pages[-1][0] == oldest
    for older, newer in zip(pages[1:], pages[:-1], strict=True):
        assert older[1] == newer[0]


def test_no_page_can_exceed_the_brokers_row_cap() -> None:
    """Measured: 50,000 rows succeeds, 100,000 fails with Invalid params."""

    newest = datetime(2026, 8, 25, tzinfo=UTC)
    pages = plan_backward(Timeframe.M1, newest=newest, oldest=newest - timedelta(days=365))

    for start, end in pages:
        assert (end - start) <= duration(Timeframe.M1) * PAGE_BARS
    assert PAGE_BARS <= 50_000


def test_an_empty_range_plans_nothing() -> None:
    now = datetime(2026, 8, 25, tzinfo=UTC)

    assert plan_backward(Timeframe.M1, newest=now, oldest=now) == ()


def test_an_inverted_range_is_a_caller_bug() -> None:
    now = datetime(2026, 8, 25, tzinfo=UTC)

    with pytest.raises(ValueError, match="oldest"):
        plan_backward(Timeframe.M1, newest=now, oldest=now + timedelta(days=1))
