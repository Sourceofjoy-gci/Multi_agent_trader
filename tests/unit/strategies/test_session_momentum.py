import inspect
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_house.core.exits import (
    ChandelierPolicy,
    FixedTargetPolicy,
    NoExitPolicy,
)
from trading_house.core.schemas import Side
from trading_house.core.snapshot import FeatureSnapshot
from trading_house.features.sessions import Session, session_of
from trading_house.marketdata.models import Bar, BarQuality, Timeframe, duration
from trading_house.strategies.impl.session_momentum import (
    SESSION_MOMENTUM_ID,
    SessionMomentum,
)


def _event_time(session: Session) -> datetime:
    if session is Session.ASIAN:
        return datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
    if session is Session.LONDON:
        return datetime(2026, 9, 21, 7, 0, tzinfo=UTC)
    if session is Session.NEW_YORK:
        return datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    return datetime(2026, 9, 21, 22, 0, tzinfo=UTC)


def _snapshot(
    *,
    session: Session = Session.LONDON,
    bars_since_session_open: int = 0,
    prior_session_return: Decimal | None = Decimal("0.0012"),
    timeframe: Timeframe = Timeframe.M15,
    instrument_id: str = "fx.eurusd",
    event_time: datetime | None = None,
) -> FeatureSnapshot:
    resolved_event_time = event_time or _event_time(session)
    resolved_as_of = resolved_event_time + duration(timeframe)
    bar = Bar(
        instrument_id=instrument_id,
        timeframe=timeframe,
        event_time=resolved_event_time,
        availability_time=resolved_as_of,
        open=Decimal("1.10000"),
        high=Decimal("1.10050"),
        low=Decimal("1.09950"),
        close=Decimal("1.10020"),
        tick_volume=100,
        spread=10,
        real_volume=0,
        quality=BarQuality.OK,
    )
    return FeatureSnapshot(
        as_of=resolved_as_of,
        instrument_id=instrument_id,
        timeframe=timeframe,
        bar=bar,
        atr=Decimal("0.00050"),
        median_spread_points=Decimal(10),
        tick_spread_points=Decimal(10),
        tick_time=resolved_as_of,
        session=session_of(resolved_event_time),
        prior_session_return=prior_session_return,
        session_open_price=Decimal("1.10000"),
        bars_since_session_open=bars_since_session_open,
    )


@pytest.fixture
def snapshot_fixture() -> FeatureSnapshot:
    return _snapshot()


def test_a_proposal_is_made_at_the_london_open_in_the_direction_of_the_night() -> None:
    proposal = SessionMomentum().evaluate(
        _snapshot(
            event_time=datetime(2026, 9, 21, 7, 0, tzinfo=UTC),
            session=Session.LONDON,
            bars_since_session_open=0,
            prior_session_return=Decimal("0.0012"),
            timeframe=Timeframe.M15,
            instrument_id="fx.eurusd",
        )
    )

    assert proposal is not None
    assert proposal.side is Side.BUY
    assert proposal.max_holding_seconds == 31_500
    assert proposal.invalidation_price == Decimal("1.09920")
    assert proposal.invalidation_price < proposal.entry_price_ref


def test_a_negative_night_proposes_a_sell() -> None:
    proposal = SessionMomentum().evaluate(
        _snapshot(
            event_time=datetime(2026, 9, 21, 7, 0, tzinfo=UTC),
            session=Session.LONDON,
            bars_since_session_open=0,
            prior_session_return=Decimal("-0.0012"),
        )
    )

    assert proposal is not None
    assert proposal.side is Side.SELL
    assert proposal.invalidation_price == Decimal("1.10120")
    assert proposal.invalidation_price > proposal.entry_price_ref


@pytest.mark.parametrize(
    ("session", "bars_since", "prior"),
    [
        (Session.ASIAN, 0, Decimal("0.0012")),
        (Session.LONDON, 1, Decimal("0.0012")),
        (Session.LONDON, 0, Decimal(0)),
        (Session.LONDON, 0, None),
    ],
)
def test_no_proposal_outside_the_entry_condition(
    session: Session, bars_since: int, prior: Decimal | None
) -> None:
    assert (
        SessionMomentum().evaluate(
            _snapshot(
                session=session,
                bars_since_session_open=bars_since,
                prior_session_return=prior,
            )
        )
        is None
    )


@pytest.mark.parametrize(
    ("timeframe", "instrument_id"),
    [
        (Timeframe.M5, "fx.eurusd"),
        (Timeframe.M15, "fx.gbpusd"),
    ],
)
def test_no_proposal_outside_the_declared_scope(timeframe: Timeframe, instrument_id: str) -> None:
    assert (
        SessionMomentum().evaluate(
            _snapshot(
                timeframe=timeframe,
                instrument_id=instrument_id,
            )
        )
        is None
    )


def test_evaluate_is_pure(snapshot_fixture: FeatureSnapshot) -> None:
    strategy = SessionMomentum()

    first = strategy.evaluate(snapshot_fixture)
    second = strategy.evaluate(snapshot_fixture)

    assert first is not None
    assert second is not None
    assert first == second


def test_the_strategy_declares_its_identity_book_and_baseline_exit() -> None:
    strategy = SessionMomentum()

    assert strategy.id == SESSION_MOMENTUM_ID
    assert strategy.book == "fx_swing"
    assert strategy.exit_policy() == NoExitPolicy(kind="none")


@pytest.mark.parametrize(
    ("policy", "expected_target"),
    [
        (NoExitPolicy(kind="none"), None),
        (FixedTargetPolicy(kind="fixed_target", r_multiple=Decimal("1.0")), Decimal("1.0")),
        (
            ChandelierPolicy(
                kind="chandelier", atr_multiple=Decimal("3.0"), min_step_points=Decimal(10)
            ),
            None,
        ),
    ],
)
def test_the_selected_arm_and_proposal_target_agree(
    policy: NoExitPolicy | FixedTargetPolicy | ChandelierPolicy,
    expected_target: Decimal | None,
) -> None:
    assert "exit_policy" in inspect.signature(SessionMomentum).parameters

    strategy = SessionMomentum(exit_policy=policy)
    proposal = strategy.evaluate(_snapshot())

    assert strategy.exit_policy() == policy
    assert proposal is not None
    assert proposal.target_r_multiple == expected_target
