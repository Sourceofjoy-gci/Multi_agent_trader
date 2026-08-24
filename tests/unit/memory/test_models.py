from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from trading_house.memory.models import AgentBelief, MemoryStore, ObservedFact, WriterKind

WHEN = datetime(2026, 8, 23, 9, 0, tzinfo=UTC)


def test_a_fact_may_only_be_written_by_deterministic_code() -> None:
    """I-13: an agent that can write a fact can launder a belief into the hot path."""

    with pytest.raises(ValidationError, match="deterministic"):
        ObservedFact(
            fact_id="f-1",
            store=MemoryStore.A,
            written_by=WriterKind.AGENT,
            instrument_id="fx.eurusd",
            metric="slippage_bps",
            value=1.5,
            observed_at=WHEN,
            availability_time=WHEN,
        )


def test_a_deterministic_writer_is_accepted() -> None:
    fact = ObservedFact(
        fact_id="f-1",
        store=MemoryStore.A,
        written_by=WriterKind.DETERMINISTIC,
        instrument_id="fx.eurusd",
        metric="slippage_bps",
        value=1.5,
        observed_at=WHEN,
        availability_time=WHEN,
    )
    assert fact.store is MemoryStore.A


def test_a_belief_always_names_its_generating_run() -> None:
    with pytest.raises(ValidationError):
        AgentBelief(
            belief_id="b-1",
            store=MemoryStore.B,
            agent_run_id=None,  # type: ignore[arg-type]
            claim="EURUSD trends after London open",
            availability_time=WHEN,
        )


def test_a_belief_can_never_be_stored_in_store_a() -> None:
    with pytest.raises(ValidationError, match="store"):
        AgentBelief(
            belief_id="b-1",
            store=MemoryStore.A,  # type: ignore[arg-type]
            agent_run_id="r-1",
            claim="x",
            availability_time=WHEN,
        )


def test_a_fact_rejects_a_naive_observed_at() -> None:
    """I-10: every canonical datetime field must be UTC-aware, including observed_at."""

    with pytest.raises(ValidationError):
        ObservedFact(
            fact_id="f-1",
            store=MemoryStore.A,
            written_by=WriterKind.DETERMINISTIC,
            instrument_id="fx.eurusd",
            metric="slippage_bps",
            value=1.5,
            observed_at=WHEN.replace(tzinfo=None),
            availability_time=WHEN,
        )


def test_a_fact_rejects_a_naive_availability_time() -> None:
    with pytest.raises(ValidationError):
        ObservedFact(
            fact_id="f-1",
            store=MemoryStore.A,
            written_by=WriterKind.DETERMINISTIC,
            instrument_id="fx.eurusd",
            metric="slippage_bps",
            value=1.5,
            observed_at=WHEN,
            availability_time=WHEN.replace(tzinfo=None),
        )


def test_a_fact_rejects_availability_time_before_observed_at() -> None:
    """The DDL mirrors this as facts_availability_not_before_observation."""

    with pytest.raises(ValidationError, match="availability_time"):
        ObservedFact(
            fact_id="f-1",
            store=MemoryStore.A,
            written_by=WriterKind.DETERMINISTIC,
            instrument_id="fx.eurusd",
            metric="slippage_bps",
            value=1.5,
            observed_at=WHEN,
            availability_time=WHEN - timedelta(seconds=1),
        )
