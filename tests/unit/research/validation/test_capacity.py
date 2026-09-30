"""The capacity diagnostic is unavailable, and says why."""

from __future__ import annotations

from tests.unit.ops.test_scenarios import _protocol
from trading_house.research.validation.capacity import (
    CapacityDiagnostic,
    CapacityStatus,
    capacity_diagnostic,
)


def test_capacity_is_always_unavailable_with_a_reason() -> None:
    diagnostic = capacity_diagnostic(_protocol())
    assert isinstance(diagnostic, CapacityDiagnostic)
    assert diagnostic.status is CapacityStatus.UNAVAILABLE
    assert "volume-to-lots" in diagnostic.reason
    assert "tick volume alone is never presented as capital capacity" in diagnostic.reason


def test_unavailable_is_the_only_status() -> None:
    assert list(CapacityStatus) == [CapacityStatus.UNAVAILABLE]
