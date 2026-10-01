"""The one result type of the statistical measurements: a finite value or a reason.

Phase 8C2 (spec S-12). A measurement is either a finite number or an explanation of
why there is none, never a default, never ``NaN`` or ``inf``; and it names the sealed
evidence it was derived from. It states no verdict and carries no threshold: whether
a value is good enough is 8D's question.

``basis_is_mark_to_market`` states the series basis the value was measured on (the
``ReturnSeries.promotion_grade`` property); it is named for the fact, as 8C1's splits
output is, because a key containing a decision word would make this a gate rather than
a measurement.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Self

from pydantic import model_validator

from trading_house.core.values import CanonicalModel, FiniteFloat, NonEmptyStr


class Measurement(CanonicalModel):
    name: NonEmptyStr
    value: FiniteFloat | None
    undefined_reason: NonEmptyStr | None
    evidence_sha256: tuple[NonEmptyStr, ...]
    basis_is_mark_to_market: bool

    @model_validator(mode="after")
    def a_value_or_a_reason_and_evidence(self) -> Self:
        if (self.value is None) == (self.undefined_reason is None):
            raise ValueError("a measurement holds a value or a reason, and exactly one")
        if not self.evidence_sha256:
            raise ValueError("a measurement names the evidence it was derived from")
        return self

    @classmethod
    def defined(
        cls,
        name: str,
        value: float,
        *,
        evidence_sha256: Iterable[str],
        basis_is_mark_to_market: bool,
    ) -> Self:
        return cls(
            name=name,
            value=float(value),
            undefined_reason=None,
            evidence_sha256=tuple(evidence_sha256),
            basis_is_mark_to_market=basis_is_mark_to_market,
        )

    @classmethod
    def undefined(
        cls,
        name: str,
        reason: str,
        *,
        evidence_sha256: Iterable[str],
        basis_is_mark_to_market: bool,
    ) -> Self:
        return cls(
            name=name,
            value=None,
            undefined_reason=reason,
            evidence_sha256=tuple(evidence_sha256),
            basis_is_mark_to_market=basis_is_mark_to_market,
        )
