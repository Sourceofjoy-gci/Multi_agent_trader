from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_house.core.schemas import (
    AgentOpinion,
    RegimeAssessment,
    Side,
    Stamped,
    TradeProposal,
)
from trading_house.core.values import PositiveQuantity


@pytest.fixture
def stamp() -> dict[str, object]:
    instant = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    return {
        "event_time": instant,
        "availability_time": instant + timedelta(microseconds=1),
        "processing_time": instant + timedelta(microseconds=2),
        "source": "market-data",
    }


@pytest.fixture
def valid_proposal(stamp: dict[str, object]) -> dict[str, object]:
    return {
        **stamp,
        "proposal_id": "proposal-1",
        "strategy_id": "momentum",
        "strategy_version": "1.0.0",
        "book": "fx_scalp",
        "instrument_id": "fx.eurusd",
        "side": Side.BUY,
        "horizon_seconds": 60,
        "entry_condition": "breakout",
        "entry_price_ref": Decimal("1.1"),
        "invalidation_price": Decimal("1.09"),
        "max_holding_seconds": 3600,
        "expected_return_bps": 10.0,
        "expected_return_stdev_bps": 5.0,
        "expected_cost_bps": 1.0,
        "expected_swap_cost_bps": 0.0,
        "win_probability": 0.6,
        "calibration_id": "calibration-1",
        "required_liquidity": PositiveQuantity(amount=Decimal("0.1"), unit="lots"),
        "regime_ref": "regime-1",
        "features_snapshot_id": "features-1",
    }


def test_stamped_rejects_out_of_order_availability(stamp: dict[str, object]) -> None:
    stamp["availability_time"] = stamp["event_time"] - timedelta(microseconds=1)  # type: ignore[operator]
    with pytest.raises(ValidationError, match="event_time"):
        Stamped(**stamp)


def test_stamped_normalizes_offset_aware_timestamps_and_serializes_utc(
    stamp: dict[str, object],
) -> None:
    stamp["event_time"] = datetime(2026, 8, 3, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    model = Stamped(**stamp)
    assert model.event_time == datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    assert '"event_time":"2026-08-03T12:00:00Z"' in model.model_dump_json()


def test_stamped_is_frozen_and_forbids_unknown_fields(stamp: dict[str, object]) -> None:
    model = Stamped(**stamp)
    with pytest.raises(ValidationError, match="frozen"):
        model.source = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="Extra inputs"):
        Stamped(**stamp, unexpected="value")


@pytest.mark.parametrize("identifier", ["", "   "])
def test_stamped_requires_non_empty_source(stamp: dict[str, object], identifier: str) -> None:
    stamp["source"] = identifier
    with pytest.raises(ValidationError):
        Stamped(**stamp)


def test_trade_proposal_requires_an_explicit_swap_declaration(
    valid_proposal: dict[str, object],
) -> None:
    payload = {
        key: value for key, value in valid_proposal.items() if key != "expected_swap_cost_bps"
    }

    with pytest.raises(ValidationError, match="expected_swap_cost_bps"):
        TradeProposal(**payload)


@pytest.mark.parametrize("swap", [1.0, 0.25])
def test_trade_proposal_accepts_swap_included_in_total_cost(
    valid_proposal: dict[str, object], swap: float
) -> None:
    proposal = TradeProposal(**{**valid_proposal, "expected_swap_cost_bps": swap})

    assert proposal.expected_swap_cost_bps == swap


def test_trade_proposal_rejects_swap_above_total_cost(valid_proposal: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="expected_swap_cost_bps"):
        TradeProposal(**{**valid_proposal, "expected_swap_cost_bps": 1.01})


def test_trade_proposal_forbids_volume(valid_proposal: dict[str, object]) -> None:
    valid_proposal["volume"] = 5.0
    with pytest.raises(ValidationError, match="Extra inputs"):
        TradeProposal(**valid_proposal)


@pytest.mark.parametrize("probability", [0.0, 1.0])
def test_trade_proposal_rejects_degenerate_probability(
    valid_proposal: dict[str, object], probability: float
) -> None:
    valid_proposal["win_probability"] = probability
    with pytest.raises(ValidationError, match="degenerate"):
        TradeProposal(**valid_proposal)


def test_buy_proposal_requires_invalidation_below_entry(
    valid_proposal: dict[str, object],
) -> None:
    valid_proposal["invalidation_price"] = valid_proposal["entry_price_ref"]
    with pytest.raises(ValidationError, match="BUY invalidation"):
        TradeProposal(**valid_proposal)


def test_sell_proposal_requires_invalidation_above_entry(
    valid_proposal: dict[str, object],
) -> None:
    valid_proposal["side"] = Side.SELL
    valid_proposal["invalidation_price"] = Decimal("1.1")
    with pytest.raises(ValidationError, match="SELL invalidation"):
        TradeProposal(**valid_proposal)


def test_regime_probabilities_sum_to_one(stamp: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="sum to 1"):
        RegimeAssessment(
            **stamp,
            symbol="EURUSD",
            volatility_state="normal",
            trend_state="range",
            liquidity_state="deep",
            probabilities={"up": 0.8, "down": 0.8},
            uncertainty=0.2,
        )


def test_regime_accepts_probabilities_within_tolerance(stamp: dict[str, object]) -> None:
    model = RegimeAssessment(
        **stamp,
        symbol="EURUSD",
        volatility_state="normal",
        trend_state="range",
        liquidity_state="deep",
        probabilities={"up": 0.5000000004, "down": 0.4999999996},
        uncertainty=0.2,
    )
    assert model.probabilities["up"] == 0.5000000004


def test_agent_opinion_defaults_evidence_to_immutable_tuples(stamp: dict[str, object]) -> None:
    opinion = AgentOpinion(
        **stamp,
        agent_role="critic",
        subject_id="proposal-1",
        stance="ABSTAIN",
        confidence=0.5,
    )
    assert opinion.evidence_for == ()
    assert opinion.evidence_against == ()
    assert opinion.missing_information == ()
