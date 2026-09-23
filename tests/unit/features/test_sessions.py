from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from trading_house.core.errors import TimestampError
from trading_house.features.sessions import (
    Session,
    preceding_session_window,
    session_bounds,
    session_of,
)

UTC = ZoneInfo("UTC")


@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        (0, Session.ASIAN),
        (6, Session.ASIAN),
        (7, Session.LONDON),  # half-open: the boundary belongs to the later window
        (13, Session.LONDON),  # the London/New York overlap resolves to London
        (16, Session.NEW_YORK),
        (20, Session.NEW_YORK),
        (21, Session.OFF),
        (23, Session.OFF),
    ],
)
def test_the_session_of_a_moment(hour: int, expected: Session) -> None:
    assert session_of(datetime(2026, 9, 21, hour, 0, tzinfo=UTC)) is expected


def test_the_overlap_resolves_to_london_and_the_test_can_tell() -> None:
    """13:00 is inside both London (07-16) and New York (12-21).

    Asserted separately from the table because it is the one case where two
    windows are both correct answers and the tie-break is a decision, not
    arithmetic. If the order of the window table is reversed, this fails and
    the parametrized case above does too -- which is the point.
    """

    assert session_of(datetime(2026, 9, 21, 13, 0, tzinfo=UTC)) is Session.LONDON


def test_the_window_preceding_the_london_open_is_the_asian_session() -> None:
    session, start, end = preceding_session_window(datetime(2026, 9, 21, 7, 0, tzinfo=UTC))

    assert session is Session.ASIAN
    assert start == datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 21, 7, 0, tzinfo=UTC)


def test_a_naive_moment_is_refused() -> None:
    with pytest.raises(TimestampError):
        session_of(datetime(2026, 9, 21, 7, 0))


def test_the_window_preceding_early_asian_crosses_midnight() -> None:
    """At 00:30 UTC the preceding window is the previous day's New York session.

    This tests the cross-midnight lookback (offset=-1 branch) that the strategy
    signal depends on. At 00:30, offset=0 finds only Asian (started at 00:00,
    ends at 07:00), which is *later* than the offset=-1 New York (previous day,
    ended at 21:00). The max() on end time selects New York.
    """

    session, start, end = preceding_session_window(datetime(2026, 9, 21, 0, 30, tzinfo=UTC))

    assert session is Session.NEW_YORK
    assert start == datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 20, 21, 0, tzinfo=UTC)


def test_session_bounds_for_london() -> None:
    start, end = session_bounds(datetime(2026, 9, 21, 13, 0, tzinfo=UTC), Session.LONDON)

    assert start == datetime(2026, 9, 21, 7, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 21, 16, 0, tzinfo=UTC)


def test_session_bounds_for_new_york() -> None:
    start, end = session_bounds(datetime(2026, 9, 21, 13, 0, tzinfo=UTC), Session.NEW_YORK)

    assert start == datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 21, 21, 0, tzinfo=UTC)


def test_session_bounds_rejects_off() -> None:
    with pytest.raises(ValueError, match="OFF is not a window with bounds"):
        session_bounds(datetime(2026, 9, 21, 22, 0, tzinfo=UTC), Session.OFF)
