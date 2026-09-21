import pytest
from pydantic import ValidationError

from trading_house.research.backtest.strategy import TrailPolicy


def test_trail_policy_rejects_any_kind_other_than_none() -> None:
    """TrailPolicy stays closed until spec section 9.2's A/B evidence exists --
    a Literal that has quietly widened is exactly what nobody would notice."""

    with pytest.raises(ValidationError, match="kind"):
        TrailPolicy(kind="fast")
