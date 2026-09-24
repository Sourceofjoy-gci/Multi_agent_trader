from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from trading_house.research.backtest.strategy import (
    ChandelierPolicy,
    ExitPolicy,
    FixedTargetPolicy,
    NoExitPolicy,
)

EXIT_POLICY = TypeAdapter(ExitPolicy)


def test_the_union_rejects_a_kind_that_names_no_arm() -> None:
    """Phase 6 shipped a single-member ``TrailPolicy`` and pinned it closed so
    a quiet widening would fail. The union is wider now -- three declared
    arms -- and still closed: a fourth arm is a decision, not a typo."""

    with pytest.raises(ValidationError, match="kind"):
        EXIT_POLICY.validate_python({"kind": "fast"})


def test_each_kind_discriminates_to_its_own_variant() -> None:
    """The discriminator is what makes a stored or hand-written arm round-trip
    to the right type. Wrong, a ``fixed_target`` deserialises as something with
    no ``r_multiple`` and the A/B silently runs two baselines."""

    assert EXIT_POLICY.validate_python({"kind": "none"}) == NoExitPolicy(kind="none")
    assert EXIT_POLICY.validate_python(
        {"kind": "fixed_target", "r_multiple": Decimal("1.0")}
    ) == FixedTargetPolicy(kind="fixed_target", r_multiple=Decimal("1.0"))
    assert EXIT_POLICY.validate_python(
        {"kind": "chandelier", "atr_multiple": Decimal(3), "min_step_points": Decimal(0)}
    ) == ChandelierPolicy(kind="chandelier", atr_multiple=Decimal(3), min_step_points=Decimal(0))


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "fixed_target", "r_multiple": Decimal(0)},
        {"kind": "chandelier", "atr_multiple": Decimal(0), "min_step_points": Decimal(1)},
        {"kind": "chandelier", "atr_multiple": Decimal(3), "min_step_points": Decimal(-1)},
    ],
)
def test_a_degenerate_parameter_is_refused_rather_than_run(payload: dict[str, object]) -> None:
    """A zero R multiple targets the entry price and a zero ATR multiple trails
    to the bar's own extreme: both are arms that cannot be interpreted, and an
    A/B that quietly ran one would report a number for a policy nobody chose.
    A negative step is not a band at all."""

    with pytest.raises(ValidationError):
        EXIT_POLICY.validate_python(payload)
