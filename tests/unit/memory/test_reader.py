from datetime import UTC, datetime

import pytest

from trading_house.memory.models import MemoryStore, ObservedFact, WriterKind
from trading_house.memory.reader import read_as_of, shrink_toward_prior

EARLY = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
MIDDLE = datetime(2026, 8, 23, 9, 0, tzinfo=UTC)
LATE = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)


def _fact(fact_id: str, *, availability: datetime) -> ObservedFact:
    return ObservedFact(
        fact_id=fact_id,
        store=MemoryStore.A,
        written_by=WriterKind.DETERMINISTIC,
        instrument_id="fx.eurusd",
        metric="slippage_bps",
        value=1.5,
        observed_at=EARLY,
        availability_time=availability,
    )


def test_reads_never_see_the_future() -> None:
    """I-14: a backtest that reads tomorrow's slippage is not a backtest."""

    facts = [_fact("f-1", availability=EARLY), _fact("f-2", availability=LATE)]
    visible = read_as_of(facts, as_of=MIDDLE)
    assert [fact.fact_id for fact in visible] == ["f-1"]


def test_a_fact_available_exactly_at_the_instant_is_visible() -> None:
    facts = [_fact("f-1", availability=MIDDLE)]
    assert len(read_as_of(facts, as_of=MIDDLE)) == 1


def test_shrinkage_pulls_a_thin_sample_toward_the_prior() -> None:
    assert shrink_toward_prior(observed=10.0, prior=1.0, observations=1, half_life=32) < 6.0


def test_shrinkage_trusts_a_thick_sample() -> None:
    assert shrink_toward_prior(observed=10.0, prior=1.0, observations=10_000, half_life=32) > 9.5


def test_shrinkage_rejects_negative_observations() -> None:
    with pytest.raises(ValueError, match="observations"):
        shrink_toward_prior(observed=1.0, prior=1.0, observations=-1, half_life=32)


def test_shrinkage_rejects_a_non_positive_half_life() -> None:
    with pytest.raises(ValueError, match="half_life"):
        shrink_toward_prior(observed=1.0, prior=1.0, observations=1, half_life=0)
