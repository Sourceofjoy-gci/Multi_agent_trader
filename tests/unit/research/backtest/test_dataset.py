"""The digest of a replay's bars and the one read that names them (Phase 8E, Task 1).

The expected values here come from outside the code under test: a known-answer literal derived
by a standalone script, and a reference implementation below that uses only ``hashlib`` and
``json``. Neither imports anything from the repository, so a rule changed in
``research/backtest/dataset.py`` moves the code and not its witnesses.

The known answer was produced by this script, run on its own (no repository import)::

    import hashlib, json
    bars = [
        {"instrument_id": "fx.eurusd", "timeframe": "M15",
         "event_time": "2026-09-21T00:00:00Z", "availability_time": "2026-09-21T00:15:00Z",
         "open": "1.10000", "high": "1.10010", "low": "1.09990", "close": "1.10005",
         "tick_volume": 100, "spread": 10, "real_volume": 0, "quality": "OK"},
        {"instrument_id": "fx.eurusd", "timeframe": "M15",
         "event_time": "2026-09-21T00:15:00Z", "availability_time": "2026-09-21T00:30:00Z",
         "open": "1.10005", "high": "1.09000", "low": "1.10020", "close": "1.10015",
         "tick_volume": 120, "spread": 12, "real_volume": 5, "quality": "OHLC_INCOHERENT"},
    ]
    payload = json.dumps(bars, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    print(hashlib.sha256(b"trading-house:dataset:v1" + payload.encode("utf-8")).hexdigest())
    # 4cab67865daf1ada0c0ced051cd1cee087c5500bcaa53d08bff77403f75f69c6

The second bar is defective on purpose: a defective bar is part of the dataset.

The sub-second known answer (``KNOWN_ANSWER_SUBSECOND``) was produced the same way, standalone,
for one bar whose event time is 00:00:00.250000 (availability 00:15:00.250000)::

    import hashlib, json
    bars = [
        {"instrument_id": "fx.eurusd", "timeframe": "M15",
         "event_time": "2026-09-21T00:00:00.250000Z",
         "availability_time": "2026-09-21T00:15:00.250000Z",
         "open": "1.10000", "high": "1.10010", "low": "1.09990", "close": "1.10005",
         "tick_volume": 100, "spread": 10, "real_volume": 0, "quality": "OK"},
    ]
    payload = json.dumps(bars, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    print(hashlib.sha256(b"trading-house:dataset:v1" + payload.encode("utf-8")).hexdigest())
    # 5d26663a5d0ba0a01f91d1e1c40379400f47f77ae5ad626f6842cdb85f8f6638
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from tests.unit.research.backtest.conftest import FakeBarReader, _session_ramp
from trading_house.core.errors import CoverageError, StatisticalInputError
from trading_house.features.engine import MaterializedBarReader
from trading_house.marketdata.models import Bar, BarQuality, Timeframe, duration
from trading_house.research.backtest.dataset import DATASET_DOMAIN, dataset_sha256
from trading_house.research.backtest.engine import replay_window_bars

KNOWN_ANSWER = "4cab67865daf1ada0c0ced051cd1cee087c5500bcaa53d08bff77403f75f69c6"
KNOWN_ANSWER_SUBSECOND = "5d26663a5d0ba0a01f91d1e1c40379400f47f77ae5ad626f6842cdb85f8f6638"
ORIGIN = datetime(2026, 9, 21, tzinfo=UTC)


def _bar(index: int, **change: Any) -> Bar:
    """One valid M15 bar. ``change`` is applied through ``Bar``'s own validation."""

    fields: dict[str, Any] = {
        "instrument_id": "fx.eurusd",
        "timeframe": Timeframe.M15,
        "event_time": ORIGIN + timedelta(minutes=15 * index),
        "availability_time": ORIGIN + timedelta(minutes=15 * (index + 1)),
        "open": Decimal("1.10000"),
        "high": Decimal("1.10010"),
        "low": Decimal("1.09990"),
        "close": Decimal("1.10005"),
        "tick_volume": 100,
        "spread": 10,
        "real_volume": 0,
        "quality": BarQuality.OK,
    }
    return Bar(**{**fields, **change})


def _known_bars() -> tuple[Bar, Bar]:
    return (
        _bar(0),
        _bar(
            1,
            open=Decimal("1.10005"),
            high=Decimal("1.09000"),
            low=Decimal("1.10020"),
            close=Decimal("1.10015"),
            tick_volume=120,
            spread=12,
            real_volume=5,
            quality=BarQuality.OHLC_INCOHERENT,
        ),
    )


def _payload(bars: list[Bar] | tuple[Bar, ...]) -> bytes:
    """The canonical bytes, restated with ``json`` alone, from the spec's words."""

    def stamp(value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    records = [
        {
            "instrument_id": bar.instrument_id,
            "timeframe": bar.timeframe.value,
            "event_time": stamp(bar.event_time),
            "availability_time": stamp(bar.availability_time),
            "open": format(bar.open, "f"),
            "high": format(bar.high, "f"),
            "low": format(bar.low, "f"),
            "close": format(bar.close, "f"),
            "tick_volume": bar.tick_volume,
            "spread": bar.spread,
            "real_volume": bar.real_volume,
            "quality": bar.quality.value,
        }
        for bar in bars
    ]
    return json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _reference(bars: list[Bar] | tuple[Bar, ...]) -> str:
    return hashlib.sha256(b"trading-house:dataset:v1" + _payload(bars)).hexdigest()


# --- the rule ------------------------------------------------------------------------------


def test_the_digest_of_a_known_bar_set_is_the_independently_derived_literal() -> None:
    assert dataset_sha256(_known_bars()) == KNOWN_ANSWER


def test_a_sub_second_event_time_keeps_its_microseconds_in_the_digest() -> None:
    event = ORIGIN + timedelta(milliseconds=250)
    bar = _bar(0, event_time=event, availability_time=event + timedelta(minutes=15))

    assert dataset_sha256([bar]) == KNOWN_ANSWER_SUBSECOND
    assert _reference([bar]) == KNOWN_ANSWER_SUBSECOND
    assert dataset_sha256([bar]) != dataset_sha256([_bar(0)])


def test_the_digest_equals_an_independent_restatement_of_the_rule_over_a_real_series() -> None:
    bars = list(_session_ramp(65))

    assert dataset_sha256(bars) == _reference(bars)


def test_the_domain_separator_is_part_of_the_hash() -> None:
    """The literal is the digest WITH the domain in front. Without it, or with the research
    chain's, the same bytes hash to something else."""

    assert DATASET_DOMAIN == b"trading-house:dataset:v1"
    payload = _payload(_known_bars())
    assert hashlib.sha256(payload).hexdigest() != KNOWN_ANSWER
    assert hashlib.sha256(b"trading-house:research:v1" + payload).hexdigest() != KNOWN_ANSWER
    assert hashlib.sha256(b"trading-house:dataset:v1" + payload).hexdigest() == KNOWN_ANSWER


def test_a_defective_bar_is_part_of_the_dataset() -> None:
    clean, defective = _known_bars()

    assert dataset_sha256([clean, defective]) != dataset_sha256([clean])
    assert dataset_sha256([clean, defective]) == KNOWN_ANSWER


_CHANGES: dict[str, Any] = {
    "instrument_id": "fx.gbpusd",
    "timeframe": Timeframe.M5,
    "event_time": ORIGIN + timedelta(seconds=1),
    "availability_time": ORIGIN + timedelta(minutes=15, seconds=1),
    "open": Decimal("1.10001"),
    "high": Decimal("1.10011"),
    "low": Decimal("1.09991"),
    "close": Decimal("1.10006"),
    "tick_volume": 101,
    "spread": 11,
    "real_volume": 1,
    "quality": BarQuality.NEGATIVE_SPREAD,
}


@pytest.mark.parametrize("field", sorted(_CHANGES))
def test_changing_any_one_field_of_a_bar_changes_the_digest(field: str) -> None:
    """All twelve, one at a time. ``model_copy`` skips validation, so a field is changed
    ALONE (a timeframe change with its availability left where it was is not a valid bar, and
    that is the point: the digest must still see it)."""

    bar = _bar(0)
    changed = bar.model_copy(update={field: _CHANGES[field]})

    assert getattr(changed, field) != getattr(bar, field)
    assert dataset_sha256([changed]) != dataset_sha256([bar])


def test_the_twelve_fields_are_every_field_a_bar_has() -> None:
    assert set(_CHANGES) == set(Bar.model_fields)
    assert len(_CHANGES) == 12


def test_a_decimal_is_hashed_as_plain_digits_and_an_instant_as_utc() -> None:
    plain = _bar(0).model_copy(update={"open": Decimal("100")})
    exponent = _bar(0).model_copy(update={"open": Decimal("1E+2")})
    assert str(exponent.open) != str(plain.open)
    assert dataset_sha256([exponent]) == dataset_sha256([plain])

    east = timezone(timedelta(hours=3))
    shifted = _bar(0).model_copy(update={"event_time": _bar(0).event_time.astimezone(east)})
    assert dataset_sha256([shifted]) == dataset_sha256([_bar(0)])


def test_trailing_zeros_of_a_decimal_are_significant() -> None:
    """The store's own representation, not something the digest normalises."""

    assert dataset_sha256([_bar(0).model_copy(update={"open": Decimal("1.1")})]) != (
        dataset_sha256([_bar(0).model_copy(update={"open": Decimal("1.10")})])
    )


def test_dropping_or_reordering_a_bar_changes_or_refuses() -> None:
    first, second, third = (_bar(i) for i in range(3))
    whole = dataset_sha256([first, second, third])

    assert dataset_sha256([first, third]) != whole
    assert dataset_sha256([first, second]) != whole
    with pytest.raises(StatisticalInputError):
        dataset_sha256([first, third, second])


def _refused(call: Any) -> str:
    with pytest.raises(StatisticalInputError) as excinfo:
        call()
    assert str(excinfo.value) == "sealed evidence is not a usable statistical input"
    return str(excinfo.value.__cause__)


def test_an_empty_series_is_refused() -> None:
    assert "at least one bar" in _refused(lambda: dataset_sha256([]))


def test_a_duplicated_bar_is_refused() -> None:
    assert "strictly increase" in _refused(lambda: dataset_sha256([_bar(0), _bar(0)]))


def test_a_series_of_two_instruments_or_two_timeframes_is_refused() -> None:
    other_instrument = _bar(1, instrument_id="fx.gbpusd")
    other_timeframe = _bar(
        1,
        timeframe=Timeframe.M5,
        availability_time=ORIGIN + timedelta(minutes=15, seconds=300),
    )

    assert "one instrument and one timeframe" in _refused(
        lambda: dataset_sha256([_bar(0), other_instrument])
    )
    assert "one instrument and one timeframe" in _refused(
        lambda: dataset_sha256([_bar(0), other_timeframe])
    )


def test_the_digest_is_the_same_in_another_process_and_under_another_hash_seed() -> None:
    script = (
        "from datetime import UTC, datetime, timedelta\n"
        "from decimal import Decimal\n"
        "from trading_house.marketdata.models import Bar, BarQuality, Timeframe\n"
        "from trading_house.research.backtest.dataset import dataset_sha256\n"
        "origin = datetime(2026, 9, 21, tzinfo=UTC)\n"
        "bars = [Bar(instrument_id='fx.eurusd', timeframe=Timeframe.M15,\n"
        "    event_time=origin + timedelta(minutes=15 * i),\n"
        "    availability_time=origin + timedelta(minutes=15 * (i + 1)),\n"
        "    open=Decimal('1.10000'), high=Decimal('1.10010'), low=Decimal('1.09990'),\n"
        "    close=Decimal('1.10005'), tick_volume=100, spread=10, real_volume=0,\n"
        "    quality=BarQuality.OK) for i in range(5)]\n"
        "print(dataset_sha256(bars))\n"
    )
    bars = [_bar(i) for i in range(5)]
    seen = set()
    for seed in ("0", "12345"):
        completed = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env={**_environment(), "PYTHONHASHSEED": seed},
        )
        seen.add(completed.stdout.strip())

    assert seen == {dataset_sha256(bars)}


def _environment() -> dict[str, str]:
    import os

    return {key: value for key, value in os.environ.items() if key != "PYTHONHASHSEED"}


# --- the one read --------------------------------------------------------------------------


def _held() -> tuple[Bar, ...]:
    """Ten bars with a defective one at index 4 and a gap where index 7 would be."""

    bars = [_bar(i) for i in range(10) if i != 7]
    bars[4] = bars[4].model_copy(update={"quality": BarQuality.OHLC_INCOHERENT})
    return tuple(bars)


def _window(reader: Any, first: int, last: int) -> tuple[Bar, ...]:
    return replay_window_bars(
        reader,
        "fx.eurusd",
        Timeframe.M15,
        ORIGIN + timedelta(minutes=15 * first),
        ORIGIN + timedelta(minutes=15 * last),
    )


def test_the_window_includes_the_bar_at_end_and_stops_before_the_next_one() -> None:
    reader = FakeBarReader(_held())

    got = _window(reader, 2, 5)

    assert [bar.event_time for bar in got] == [
        ORIGIN + timedelta(minutes=15 * i) for i in range(2, 6)
    ]
    assert dataset_sha256(got) == dataset_sha256(_held()[2:6])
    assert dataset_sha256(got) != dataset_sha256(_held()[2:5])  # the bar at ``end`` is in
    assert dataset_sha256(got) != dataset_sha256(_held()[2:7])  # the bar after it is out


def test_the_window_includes_the_bar_at_start_and_not_the_one_before() -> None:
    got = _window(FakeBarReader(_held()), 2, 3)

    assert got[0].event_time == ORIGIN + timedelta(minutes=30)
    assert len(got) == 2


def test_the_window_reads_defective_bars() -> None:
    got = _window(FakeBarReader(_held()), 3, 5)

    assert [bar.quality for bar in got] == [
        BarQuality.OK,
        BarQuality.OHLC_INCOHERENT,
        BarQuality.OK,
    ]


def test_the_window_reads_to_one_bar_past_end_as_of_that_instant() -> None:
    """``end`` and ``as_of`` are both ``end + one bar``: the shape the engine always read."""

    calls: list[dict[str, Any]] = []

    class Recording:
        def bars(self, instrument_id: str, timeframe: Timeframe, **kwargs: Any) -> tuple[Bar, ...]:
            calls.append(kwargs)
            return ()

    end = ORIGIN + timedelta(hours=1)
    replay_window_bars(Recording(), "fx.eurusd", Timeframe.M15, ORIGIN, end)  # type: ignore[arg-type]

    horizon = end + duration(Timeframe.M15)
    assert calls == [{"start": ORIGIN, "end": horizon, "as_of": horizon, "include_defective": True}]


def test_a_start_before_anything_is_held_is_the_stores_coverage_refusal() -> None:
    with pytest.raises(CoverageError):
        replay_window_bars(
            FakeBarReader(_held()),
            "fx.eurusd",
            Timeframe.M15,
            ORIGIN - timedelta(minutes=15),
            ORIGIN,
        )


@pytest.mark.parametrize(("first", "last"), [(0, 9), (2, 5), (3, 8), (4, 4), (6, 6), (5, 9)])
def test_the_engines_reader_and_the_raw_store_reader_read_the_same_window(
    first: int, last: int
) -> None:
    """``Backtester.run`` reads through a ``MaterializedBarReader`` and the pre-flight through the
    store itself. Inside the stored range they return the same bars, defective ones and a gap
    included, so a digest taken either way is one digest."""

    raw = FakeBarReader(_held())
    materialized = MaterializedBarReader(raw, raw.coverage("fx.eurusd", Timeframe.M15))

    assert _window(materialized, first, last) == _window(raw, first, last)
    if _window(raw, first, last):
        assert dataset_sha256(_window(materialized, first, last)) == dataset_sha256(
            _window(raw, first, last)
        )
