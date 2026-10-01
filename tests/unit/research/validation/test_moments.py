"""Phase 8C2: the measurement type and the sample moments.

The moment literals for the long series come from the standalone reference script in
``test_psr.py``'s docstring (math and statistics only); the tiny series is worked out by
hand below.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from pydantic import ValidationError

from trading_house.core.errors import StatisticalInputError
from trading_house.research.validation.measurement import Measurement
from trading_house.research.validation.moments import Moments, NoMoments, moments

DIGEST = "e" * 64


def _refused(error: pytest.ExceptionInfo[StatisticalInputError], fragment: str) -> None:
    assert str(error.value) == StatisticalInputError.public_message
    cause = error.value.__cause__
    assert isinstance(cause, ValueError)
    assert fragment in str(cause)


def _wave(n: int, drift: float, scale: float) -> np.ndarray:
    return np.array(
        [drift + scale * (math.sin(1.7 * i) + 0.5 * math.cos(0.23 * i * i)) for i in range(n)],
        dtype=np.float64,
    )


def test_the_moments_of_a_tiny_series_are_the_hand_computed_ones() -> None:
    """x = 1, 2, 3, 4, 10.  n = 5, mean = 20/5 = 4.

    deviations -3 -2 -1 0 6; squares 9 4 1 0 36 (sum 50); cubes -27 -8 -1 0 216 (sum 180);
    fourth powers 81 16 1 0 1296 (sum 1394).
    m2 = 50/5 = 10, m3 = 180/5 = 36, m4 = 1394/5 = 278.8   (population moments)
    std (ddof=1) = sqrt(50/4) = sqrt(12.5)           sharpe = 4 / sqrt(12.5)
    skew = 36 / 10**1.5 = 36 / (10 sqrt(10))          kurt = 278.8 / 100 = 2.788
    """

    found = moments(np.array([1.0, 2.0, 3.0, 4.0, 10.0]))

    assert isinstance(found, Moments)
    assert found.n == 5
    assert found.mean == 4.0
    assert found.std == pytest.approx(math.sqrt(12.5), rel=1e-14)
    assert found.sharpe == pytest.approx(4 / math.sqrt(12.5), rel=1e-14)
    assert found.skew == pytest.approx(36 / (10 * math.sqrt(10)), rel=1e-14)
    assert found.kurt == pytest.approx(2.788, rel=1e-14)


def test_the_skew_changes_sign_with_the_tail() -> None:
    right = moments(np.array([1.0, 2.0, 3.0, 4.0, 10.0]))
    left = moments(np.array([-1.0, -2.0, -3.0, -4.0, -10.0]))

    assert isinstance(right, Moments)
    assert isinstance(left, Moments)
    assert left.skew == pytest.approx(-right.skew, rel=1e-14)
    assert left.kurt == pytest.approx(right.kurt, rel=1e-14)


def test_the_moments_of_a_long_series_match_the_independent_reference() -> None:
    a = moments(_wave(500, 0.0004, 0.004))

    assert isinstance(a, Moments)
    assert a.mean == pytest.approx(0.00032496136956875225, rel=1e-12)
    assert a.std == pytest.approx(0.0031438823079299556, rel=1e-12)
    assert a.sharpe == pytest.approx(0.10336308351908963, rel=1e-12)
    assert a.skew == pytest.approx(-0.011051149545646299, rel=1e-9)
    assert a.kurt == pytest.approx(1.9713519508800625, rel=1e-12)


@pytest.mark.parametrize(
    ("values", "fragment"),
    [
        (np.array([], dtype=np.float64), "holds 0 values"),
        (np.array([1.0]), "holds 1 values"),
        (np.array([0.5, 0.5, 0.5]), "constant"),
        (np.array([0.1] * 30), "constant"),
        (np.array([0.0, 1e-200, 0.0, 1e-200]), "underflows"),
        (np.array([1e200, -1e200, 1e200, -1e200]), "not finite"),
    ],
)
def test_a_sample_the_moments_cannot_describe_is_undefined_with_its_reason(
    values: np.ndarray, fragment: str
) -> None:
    found = moments(values)

    assert isinstance(found, NoMoments)
    assert fragment in found.reason


@pytest.mark.parametrize(
    ("values", "fragment"),
    [
        ([1.0, 2.0, 3.0], "one-dimensional float64 array"),
        (np.array([1, 2, 3]), "one-dimensional float64 array"),
        (np.zeros((3, 3)), "one-dimensional float64 array"),
        (np.array([1.0, np.nan, 3.0]), "finite"),
        (np.array([1.0, np.inf, 3.0]), "finite"),
    ],
)
def test_malformed_input_is_refused_not_described(values: object, fragment: str) -> None:
    with pytest.raises(StatisticalInputError) as error:
        moments(values)  # type: ignore[arg-type]

    _refused(error, fragment)


# --- the measurement type -----------------------------------------------------------


def test_a_defined_measurement_holds_a_value_and_its_evidence() -> None:
    item = Measurement.defined("psr", 0.5, evidence_sha256=[DIGEST], basis_is_mark_to_market=True)

    assert (item.name, item.value, item.undefined_reason) == ("psr", 0.5, None)
    assert item.evidence_sha256 == (DIGEST,)
    assert item.basis_is_mark_to_market is True


def test_an_undefined_measurement_holds_a_reason_and_no_value() -> None:
    item = Measurement.undefined(
        "psr", "too short", evidence_sha256=[DIGEST], basis_is_mark_to_market=False
    )

    assert (item.value, item.undefined_reason) == (None, "too short")
    assert item.basis_is_mark_to_market is False


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_a_non_finite_value_never_becomes_a_measurement(bad: float) -> None:
    with pytest.raises(ValidationError):
        Measurement.defined("psr", bad, evidence_sha256=[DIGEST], basis_is_mark_to_market=True)


def test_a_measurement_holds_a_value_or_a_reason_and_exactly_one() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        Measurement(
            name="psr",
            value=0.5,
            undefined_reason="both",
            evidence_sha256=(DIGEST,),
            basis_is_mark_to_market=True,
        )
    with pytest.raises(ValidationError, match="exactly one"):
        Measurement(
            name="psr",
            value=None,
            undefined_reason=None,
            evidence_sha256=(DIGEST,),
            basis_is_mark_to_market=True,
        )


def test_a_measurement_without_evidence_is_refused() -> None:
    with pytest.raises(ValidationError, match="evidence"):
        Measurement.defined("psr", 0.5, evidence_sha256=[], basis_is_mark_to_market=True)


def test_a_measurement_is_frozen() -> None:
    item = Measurement.defined("psr", 0.5, evidence_sha256=[DIGEST], basis_is_mark_to_market=True)

    with pytest.raises(ValidationError):
        item.value = 0.9  # type: ignore[misc]
