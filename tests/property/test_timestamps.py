"""Invariant I-10: every canonical timestamp is aware, UTC, and ordered."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from trading_house.core.clock import FixedClock, ensure_utc
from trading_house.core.errors import TimestampError
from trading_house.core.schemas import RegimeAssessment

NAIVE = st.datetimes(min_value=datetime(1900, 1, 1), max_value=datetime(2200, 1, 1))
OFFSETS = st.integers(min_value=-1439, max_value=1439).map(
    lambda minutes: timezone(timedelta(minutes=minutes))
)
AWARE = st.builds(lambda value, zone: value.replace(tzinfo=zone), NAIVE, OFFSETS)


def _assessment(event: datetime, availability: datetime, processing: datetime) -> RegimeAssessment:
    return RegimeAssessment(
        event_time=event,
        availability_time=availability,
        processing_time=processing,
        source="property-suite",
        symbol="EURUSD",
        volatility_state="normal",
        trend_state="up",
        liquidity_state="normal",
        probabilities={"up": 1.0},
        uncertainty=0.5,
    )


@given(AWARE)
def test_utc_normalization_preserves_the_instant(value: datetime) -> None:
    normalized = ensure_utc(value)

    assert normalized.timestamp() == value.timestamp()
    assert normalized.tzinfo is UTC
    assert normalized.utcoffset() == timedelta(0)


@given(AWARE)
def test_utc_normalization_is_idempotent(value: datetime) -> None:
    once = ensure_utc(value)

    assert ensure_utc(once) == once


@given(NAIVE)
def test_naive_datetimes_are_always_rejected(value: datetime) -> None:
    with pytest.raises(TimestampError):
        ensure_utc(value)


@given(AWARE)
def test_fixed_clock_always_reports_utc(value: datetime) -> None:
    assert FixedClock(value).now().tzinfo is UTC


@given(NAIVE)
def test_fixed_clock_rejects_naive_instants(value: datetime) -> None:
    with pytest.raises(TimestampError):
        FixedClock(value)


@given(st.lists(AWARE, min_size=3, max_size=3))
def test_ordered_triples_are_accepted_in_any_timezone(values: list[datetime]) -> None:
    event, availability, processing = sorted(values)

    model = _assessment(event, availability, processing)

    assert model.event_time <= model.availability_time <= model.processing_time
    assert model.event_time.tzinfo is UTC


@given(st.lists(AWARE, min_size=3, max_size=3))
def test_inverted_availability_is_always_rejected(values: list[datetime]) -> None:
    event, availability, processing = sorted(values)
    if availability == event:
        return

    with pytest.raises(ValidationError, match="availability_time"):
        _assessment(availability, event, processing)


@given(st.lists(AWARE, min_size=3, max_size=3))
def test_inverted_processing_is_always_rejected(values: list[datetime]) -> None:
    event, availability, processing = sorted(values)
    if processing == availability:
        return

    with pytest.raises(ValidationError, match="processing_time"):
        _assessment(event, processing, availability)


@given(AWARE)
def test_stamped_normalizes_every_timestamp_to_utc(value: datetime) -> None:
    model = _assessment(value, value, value)

    assert model.event_time.tzinfo is UTC
    assert model.availability_time.tzinfo is UTC
    assert model.processing_time.tzinfo is UTC
    assert model.event_time.timestamp() == value.timestamp()
