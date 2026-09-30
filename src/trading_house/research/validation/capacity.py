"""The capacity diagnostic: a typed statement of what cannot be said.

Nothing in a protocol declares how traded volume maps to lots a book could
hold, so no capacity figure can be derived honestly. Tick volume alone is never
presented as capital capacity. A future declared model widens ``CapacityStatus``;
no speculative branch is written for it here.
"""

from __future__ import annotations

from enum import Enum

from trading_house.core.values import CanonicalModel, NonEmptyStr
from trading_house.research.trial_ledger import TrialProtocol


class CapacityStatus(str, Enum):  # noqa: UP042
    UNAVAILABLE = "unavailable"


class CapacityDiagnostic(CanonicalModel):
    status: CapacityStatus
    reason: NonEmptyStr


def capacity_diagnostic(protocol: TrialProtocol) -> CapacityDiagnostic:
    """Always unavailable: the protocol can declare no volume-to-lots model."""

    del protocol
    return CapacityDiagnostic(
        status=CapacityStatus.UNAVAILABLE,
        reason=(
            "the protocol can declare no volume-to-lots model, and tick volume alone "
            "is never presented as capital capacity"
        ),
    )
