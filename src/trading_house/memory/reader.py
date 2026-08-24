"""Point-in-time reads. A memory read that sees the future is a leak."""

from collections.abc import Sequence
from datetime import datetime

from trading_house.memory.models import ObservedFact


def read_as_of(facts: Sequence[ObservedFact], *, as_of: datetime) -> tuple[ObservedFact, ...]:
    """Return only facts that were available at the given instant (I-14)."""

    return tuple(fact for fact in facts if fact.availability_time <= as_of)


def shrink_toward_prior(
    *, observed: float, prior: float, observations: int, half_life: int
) -> float:
    """Blend an empirical estimate toward a prior on an explicit half-life.

    Memory that chases last week's regime is worse than no memory.
    """

    if observations < 0 or half_life <= 0:
        raise ValueError("observations must be non-negative and half_life positive")
    weight = observations / (observations + half_life)
    return prior + weight * (observed - prior)
