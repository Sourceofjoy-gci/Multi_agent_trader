"""The append-only write path: duplicates are silent, conflicts are counted."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from tests.integration.marketdata.conftest import NINE, _bar, seed
from trading_house.marketdata.models import BarQuality, Timeframe
from trading_house.marketdata.store import BarStore

pytestmark = pytest.mark.integration


def test_appending_the_same_bar_twice_stores_it_once(bar_store: BarStore) -> None:
    """Page boundaries overlap by design, so a second identical write is
    normal traffic and must be silent -- not an error, and not a conflict."""

    first = seed(bar_store, [_bar(0)])
    second = seed(bar_store, [_bar(0)])

    assert first.stored == 1
    assert second.stored == 0
    assert second.duplicate == 1
    assert second.conflicting == 0


def test_a_refetch_that_disagrees_is_counted_not_applied(bar_store: BarStore) -> None:
    """Brokers revise history. The first observation stands, the disagreement
    is counted, and nothing is silently rewritten (spec 4.3). Overwriting
    would mean a backtest run today and the same backtest run next month
    could differ with no code change and no record of why."""

    seed(bar_store, [_bar(0)])
    revised = _bar(0).model_copy(update={"close": Decimal("9.99999")})

    result = seed(bar_store, [revised])

    assert result.conflicting == 1
    assert result.stored == 0
    held = bar_store.bars(
        "fx.eurusd",
        Timeframe.M1,
        start=NINE,
        end=NINE + timedelta(hours=1),
        as_of=NINE + timedelta(hours=1),
    )
    assert held[0].close == Decimal("1.10020")


def test_a_defective_bar_is_stored_alongside_clean_ones(bar_store: BarStore) -> None:
    """D-2: the forensic record is the point. A gate I decide was too strict
    next month can only be re-adjudicated against data that was kept."""

    result = seed(bar_store, [_bar(0), _bar(1, quality=BarQuality.OHLC_INCOHERENT)])

    assert result.stored == 2
    assert bar_store.coverage("fx.eurusd", Timeframe.M1).defective_bars == 1


def test_a_bar_cannot_be_written_without_a_run_to_blame(bar_store: BarStore) -> None:
    """The foreign key is the point: every bar is traceable to the run that
    fetched it, so a suspect window can be tied back to how it was obtained."""

    import psycopg

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        bar_store.append_bars([_bar(0)], run_id=uuid4())
