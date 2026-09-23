"""Session windows, fixed in UTC.

Deliberately NOT timezone-aware. ``zoneinfo`` would make a backtest result
depend on the machine's tzdata version, and tzdata changes several times a
year -- so a phase whose contract is byte-reproducibility cannot define its
clock that way. The cost is that DST moves the effective local hour by one,
which the strategy spec states as a limitation rather than absorbing.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum

from trading_house.core.clock import ensure_utc


class Session(str, Enum):  # noqa: UP042
    ASIAN = "asian"
    LONDON = "london"
    NEW_YORK = "new_york"
    OFF = "off"


# Order is load-bearing: London precedes New York so the 12:00-16:00 overlap
# resolves to London, which is the window this phase's strategy trades.
_WINDOWS: tuple[tuple[Session, int, int], ...] = (
    (Session.LONDON, 7, 16),
    (Session.NEW_YORK, 12, 21),
    (Session.ASIAN, 0, 7),
)


def session_of(moment: datetime) -> Session:
    """The window containing ``moment``. Half-open: 07:00 is London, not Asian."""

    hour = ensure_utc(moment).hour
    for session, start, end in _WINDOWS:
        if start <= hour < end:
            return session
    return Session.OFF


def session_bounds(moment: datetime, session: Session) -> tuple[datetime, datetime]:
    """The bounds of ``session`` on ``moment``'s UTC date."""

    if session is Session.OFF:
        raise ValueError("OFF is not a window with bounds")
    start_hour, end_hour = next((s, e) for name, s, e in _WINDOWS if name is session)
    midnight = ensure_utc(moment).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(hours=start_hour), midnight + timedelta(hours=end_hour)


def preceding_session_window(as_of: datetime) -> tuple[Session, datetime, datetime]:
    """The window that most recently ENDED at or before ``as_of``.

    At 07:00 that is the Asian window of the same date. Looks back across the
    date boundary, so the first London open after midnight resolves correctly.
    """

    moment = ensure_utc(as_of)
    candidates: list[tuple[Session, datetime, datetime]] = []
    for offset in (0, -1):
        day = moment + timedelta(days=offset)
        for session, _start, _end in _WINDOWS:
            start, end = session_bounds(day, session)
            if end <= moment:
                candidates.append((session, start, end))
    return max(candidates, key=lambda window: window[2])
