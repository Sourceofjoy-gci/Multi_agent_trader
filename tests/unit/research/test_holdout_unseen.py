"""Phase 12: what "unseen" means for a holdout, and the prospective lock.

Built on ``test_promotion``'s chain helpers so the events are the same shapes the rest of the
holdout lifecycle tests use.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.unit.ops.test_scenarios import _preregistered_event
from tests.unit.research.test_promotion import (
    LEVELS,
    STATES,
    TRIAL,
    WINDOWS,
    _protocol,
    _sealed,
)
from trading_house.core.errors import PromotionRefusedError
from trading_house.marketdata.models import Timeframe
from trading_house.research.holdout_window import SealedWindow
from trading_house.research.promotion import derive_holdout
from trading_house.research.trial_ledger import (
    CapacitySpec,
    HoldoutCollection,
    HoldoutSpec,
    HoldoutState,
)

JAN = datetime(2026, 1, 1, tzinfo=UTC)
FEB = datetime(2026, 2, 1, tzinfo=UTC)
LOCKED_AT = datetime(2025, 12, 15, tzinfo=UTC)


def _registration(holdout: HoldoutSpec, **data_changes: object):
    protocol = _protocol((TRIAL,))
    data = protocol.data.model_copy(update=data_changes) if data_changes else protocol.data
    return _preregistered_event(protocol.model_copy(update={"holdout": holdout, "data": data}))


def _retrospective(start: datetime = JAN, end: datetime = FEB) -> HoldoutSpec:
    return HoldoutSpec(state=HoldoutState.LOCKED, start=start, end=end, dataset_sha256="9" * 64)


def _prospective(start: datetime = JAN, end: datetime = FEB) -> HoldoutSpec:
    return HoldoutSpec(
        state=HoldoutState.LOCKED,
        start=start,
        end=end,
        collection=HoldoutCollection.PROSPECTIVE,
    )


def _derive(*events, windows=WINDOWS, registered_at=LOCKED_AT):
    return derive_holdout(
        TRIAL, events, STATES, LEVELS, sealed_windows=windows, registered_at=registered_at
    )


# --- the research window ------------------------------------------------------------------


def test_a_holdout_inside_its_own_research_window_is_contaminated() -> None:
    status = _derive(_registration(_retrospective(), start=JAN - timedelta(days=30), end=FEB))

    assert status.state is HoldoutState.CONTAMINATED
    assert "reaches into the research window" in status.reason


def test_the_research_windows_last_bar_is_the_edge() -> None:
    """Both windows are inclusive bar open times (``replay_window_bars``): research whose last
    bar is the holdout's first read it; research ending one bar earlier did not."""

    touching = _registration(_retrospective(), start=JAN - timedelta(days=30), end=JAN)
    clear = _registration(
        _retrospective(), start=JAN - timedelta(days=30), end=JAN - timedelta(minutes=15)
    )

    assert _derive(touching).state is HoldoutState.CONTAMINATED
    assert _derive(clear).state is HoldoutState.LOCKED


# --- other runs in the chain --------------------------------------------------------------

DIGEST = "e" * 64


def _windows(**window: object) -> dict[str, SealedWindow]:
    fields: dict[str, object] = {
        "instrument_id": "fx.eurusd",
        "timeframe": Timeframe.H1,
        "start": datetime(2025, 6, 1, tzinfo=UTC),
        "end": JAN + timedelta(days=3),
        "opened": False,
    }
    fields.update(window)
    return {**WINDOWS, DIGEST: SealedWindow(**fields)}


def test_any_run_on_the_instrument_that_read_the_window_contaminates_it() -> None:
    status = _derive(
        _registration(_retrospective()), _sealed(DIGEST, trial_id="research-x"), windows=_windows()
    )

    assert status.state is HoldoutState.CONTAMINATED
    assert "trial research-x's sealed run read fx.eurusd H1 bars" in status.reason


def test_a_run_on_another_instrument_does_not() -> None:
    status = _derive(
        _registration(_retrospective()),
        _sealed(DIGEST, trial_id="research-x"),
        windows=_windows(instrument_id="metal.xauusd"),
    )

    assert status.state is HoldoutState.LOCKED


def test_a_sealed_run_with_no_known_window_is_refused_not_guessed() -> None:
    with pytest.raises(PromotionRefusedError):
        _derive(_registration(_retrospective()), _sealed(DIGEST, trial_id="research-x"))


# --- prospective ---------------------------------------------------------------------------


def test_a_prospective_holdout_locked_before_its_data_is_locked() -> None:
    assert _derive(_registration(_prospective())).state is HoldoutState.LOCKED


def test_a_prospective_holdout_that_starts_before_its_lock_is_contaminated() -> None:
    status = _derive(_registration(_prospective()), registered_at=JAN + timedelta(hours=1))

    assert status.state is HoldoutState.CONTAMINATED
    assert "before its lock was recorded" in status.reason


def test_a_prospective_holdout_without_its_lock_time_is_refused() -> None:
    with pytest.raises(PromotionRefusedError):
        _derive(_registration(_prospective()), registered_at=None)


@pytest.mark.parametrize(
    "fields",
    [
        {"dataset_sha256": "9" * 64},
        {"start": None, "end": None},
        {"state": HoldoutState.NOT_DEFINED},
        {"start": FEB, "end": JAN},
    ],
    ids=["a-hash-of-future-data", "no-window", "not-locked", "backwards"],
)
def test_a_prospective_holdout_that_cannot_mean_anything_is_refused(fields: dict) -> None:
    base: dict[str, object] = {
        "state": HoldoutState.LOCKED,
        "start": JAN,
        "end": FEB,
        "collection": HoldoutCollection.PROSPECTIVE,
    }
    base.update(fields)

    with pytest.raises(ValidationError):
        HoldoutSpec(**base)


def test_old_protocols_keep_their_bytes() -> None:
    """Neither new field writes a key when absent, so a protocol registered before Phase 12
    keeps its canonical digest."""

    dumped = _protocol((TRIAL,)).model_dump(mode="json")

    assert "capacity" not in dumped
    assert "collection" not in dumped["holdout"]
    with_capacity = _protocol((TRIAL,)).model_copy(
        update={
            "capacity": CapacitySpec(
                model="tick_volume_participation_v1",
                lots_per_tick=Decimal(1),
                target_equity=Decimal(1),
                max_participation=Decimal(1),
                impact_points_at_full_participation=Decimal(0),
                max_impact_fraction_of_edge=Decimal(1),
            )
        }
    )
    assert "capacity" in with_capacity.model_dump(mode="json")
