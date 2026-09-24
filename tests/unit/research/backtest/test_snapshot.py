from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_house.features.sessions import Session, session_of
from trading_house.marketdata.models import Bar, BarQuality, Timeframe, duration
from trading_house.research.backtest.snapshot import (
    MIN_HORIZON_BARS,
    FeatureSnapshot,
    horizon_is_simulatable,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _snapshot(
    *,
    spread: int = 17,
    event_time: datetime | None = None,
    as_of: datetime | None = None,
    availability_time: datetime | None = None,
    tick_spread_points: Decimal | None = None,
    tick_time: datetime | None = None,
    session: Session | None = None,
    prior_session_return: Decimal | None = None,
    session_open_price: Decimal | None = None,
    bars_since_session_open: int = 0,
) -> FeatureSnapshot:
    timeframe = Timeframe.M1
    if availability_time is not None:
        resolved_availability_time = availability_time
    elif event_time is not None:
        resolved_availability_time = event_time + duration(timeframe)
    else:
        resolved_availability_time = NOW
    bar = Bar(
        instrument_id="fx.eurusd",
        timeframe=timeframe,
        event_time=resolved_availability_time - duration(timeframe),
        availability_time=resolved_availability_time,
        open=Decimal("1.10000"),
        high=Decimal("1.10050"),
        low=Decimal("1.09950"),
        close=Decimal("1.10020"),
        tick_volume=100,
        spread=spread,
        real_volume=0,
        quality=BarQuality.OK,
    )
    return FeatureSnapshot(
        as_of=as_of if as_of is not None else resolved_availability_time,
        instrument_id="fx.eurusd",
        timeframe=timeframe,
        bar=bar,
        atr=Decimal("0.00050"),
        median_spread_points=Decimal(spread),
        tick_spread_points=(
            tick_spread_points if tick_spread_points is not None else Decimal(spread)
        ),
        tick_time=tick_time if tick_time is not None else bar.availability_time,
        session=session if session is not None else session_of(bar.event_time),
        prior_session_return=prior_session_return,
        session_open_price=(session_open_price if session_open_price is not None else bar.open),
        bars_since_session_open=bars_since_session_open,
    )


def test_the_snapshot_ties_its_tick_fields_to_the_bar_that_closed() -> None:
    """tick_spread_points and tick_time are named for a tick stream this phase
    does not have. Their meaning is the closing bar's own spread and
    availability_time -- stated as a constructed invariant rather than left to
    whichever caller builds a snapshot next."""

    snapshot = _snapshot(spread=17)

    assert snapshot.tick_spread_points == Decimal(17)
    assert snapshot.tick_time == snapshot.bar.availability_time
    assert snapshot.tick_time == snapshot.as_of


def test_a_non_utc_but_consistent_triple_is_still_normalized_to_utc() -> None:
    """I-10 is UTC-awareness, not merely naive-rejection -- a distinction the
    cross-check above cannot see. ``tick_fields_match_the_closing_bar`` compares
    datetimes with ``==``, which Python resolves by instant regardless of
    ``tzinfo``, so a ``tick_time``/``as_of`` pair that agrees with the bar's
    ``availability_time`` at a non-UTC offset satisfies that check without ever
    exercising ``normalize_timestamp``. This is the case the naive-datetime
    property test cannot reach either, since it only ever flips one field to
    naive at a time. Assert the *stored* values are UTC, not merely equal."""

    same_instant_plus_two = NOW.astimezone(timezone(timedelta(hours=2)))

    snapshot = _snapshot(
        spread=17,
        as_of=same_instant_plus_two,
        availability_time=same_instant_plus_two,
        tick_time=same_instant_plus_two,
    )

    assert snapshot.as_of.tzinfo is UTC
    assert snapshot.tick_time.tzinfo is UTC


def test_a_snapshot_whose_tick_fields_disagree_with_its_bar_is_refused() -> None:
    """Without this the two representations drift, and the risk engine's
    spread-blowout gate silently judges a different bar than the strategy saw."""

    with pytest.raises(ValueError, match="tick_spread_points"):
        _snapshot(spread=17, tick_spread_points=Decimal(3))


def test_a_snapshot_whose_tick_time_disagrees_with_its_bars_availability_is_refused() -> None:
    """Pins the branch distinct from as_of: tick_time matches as_of here, so
    only a tick_time/bar.availability_time mismatch can be firing this error."""

    with pytest.raises(ValueError, match="availability_time"):
        _snapshot(
            spread=17,
            as_of=NOW,
            availability_time=NOW - timedelta(seconds=1),
            tick_time=NOW,
        )


def test_a_snapshot_whose_tick_time_disagrees_with_as_of_is_refused() -> None:
    """Pins the branch distinct from availability_time: tick_time matches the
    bar's availability_time here, so only a tick_time/as_of mismatch can be
    firing this error."""

    with pytest.raises(ValueError, match="tick_time must equal as_of"):
        _snapshot(
            spread=17,
            as_of=NOW - timedelta(seconds=1),
            availability_time=NOW,
            tick_time=NOW,
        )


def test_a_snapshot_whose_session_disagrees_with_its_bar_is_refused() -> None:
    """The session label is derived from the bar, so it cannot be asserted
    independently of it. A caller that computes the session itself and gets it
    wrong would otherwise hand the strategy a snapshot that lies about when it
    is -- and the strategy's whole entry condition is a session test."""

    with pytest.raises(ValidationError):
        _snapshot(
            event_time=datetime(2026, 9, 21, 9, 0, tzinfo=UTC),  # London
            session=Session.ASIAN,
        )


def test_a_snapshot_carries_the_session_features() -> None:
    """M4: ``bars_since_session_open >= 0`` is guaranteed by ``NonNegativeInt``
    at the type level and cannot fail from any production change, so this
    pins the actual value the helper produces instead."""

    snapshot = _snapshot(event_time=datetime(2026, 9, 21, 9, 0, tzinfo=UTC))

    assert snapshot.session is Session.LONDON
    assert snapshot.bars_since_session_open == 0


def test_a_snapshot_carries_the_prior_session_return_and_session_open_price() -> None:
    """Pins the wiring past the helper's own defaults: the values a caller
    passes in are the values that land on the model, not some default that
    happens to satisfy the other assertions above."""

    snapshot = _snapshot(
        prior_session_return=Decimal("0.0012"),
        session_open_price=Decimal("1.09800"),
        bars_since_session_open=4,
    )

    assert snapshot.prior_session_return == Decimal("0.0012")
    assert snapshot.session_open_price == Decimal("1.09800")
    assert snapshot.bars_since_session_open == 4


def test_a_snapshot_refuses_a_negative_bars_since_session_open() -> None:
    with pytest.raises(ValidationError):
        _snapshot(bars_since_session_open=-1)


@pytest.mark.parametrize(
    ("horizon_seconds", "timeframe", "expected"),
    [
        (600, Timeframe.M1, True),  # 10 bars exactly -- the boundary is allowed
        (599, Timeframe.M1, False),  # one second under it
        (30, Timeframe.M1, False),  # the 30-second scalp D-1 exists to refuse
        (36_000, Timeframe.H1, True),
    ],
)
def test_a_horizon_under_ten_bars_is_not_simulatable(
    horizon_seconds: int, timeframe: Timeframe, expected: bool
) -> None:
    """D-1. Below roughly ten bars most trades open and close inside a couple of
    bars, so D-2's stop-first pessimism dominates and the number being measured
    is the simulator's convention rather than the strategy."""

    assert horizon_is_simulatable(horizon_seconds=horizon_seconds, timeframe=timeframe) is expected


def test_the_threshold_is_a_named_constant_not_a_literal() -> None:
    """A future phase that ingests ticks lowers this. A literal buried in a
    comparison is one nobody finds."""

    assert MIN_HORIZON_BARS == 10
