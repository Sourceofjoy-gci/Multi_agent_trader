"""Halts: the states in which the system sends no new orders.

Three kinds, one shape (Phase 11 design):

- ``SAFE_MODE`` -- firm-wide, entered by the system when it can no longer
  vouch for itself (a guard escalation, a venue refusal that names the
  account's authority, a reject streak) or by an operator. No new orders;
  broker-side stops are relied on.
- ``KILL`` -- an operator's switch at one of four scopes: a strategy, an
  instrument, a book, or the firm (master spec 13.1).
- ``DRAWDOWN_HALT`` -- a book or the firm past its signed drawdown halt. The
  constitution says it "requires human unlock", so it latches.

Every halt ends the same way: a person clears it. Nothing in the system
clears one on its own, because the condition that entered it going away is
not the same as somebody having looked.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import Enum
from typing import Self

from pydantic import field_validator, model_validator

from trading_house.core.clock import ensure_utc
from trading_house.core.errors import TimestampError
from trading_house.core.values import CanonicalModel, NonEmptyStr


class HaltKind(str, Enum):  # noqa: UP042
    SAFE_MODE = "safe_mode"
    KILL = "kill"
    DRAWDOWN_HALT = "drawdown_halt"


class HaltScope(str, Enum):  # noqa: UP042
    FIRM = "firm"
    BOOK = "book"
    INSTRUMENT = "instrument"
    STRATEGY = "strategy"


_SCOPES_BY_KIND: dict[HaltKind, frozenset[HaltScope]] = {
    HaltKind.SAFE_MODE: frozenset({HaltScope.FIRM}),
    HaltKind.KILL: frozenset(HaltScope),
    HaltKind.DRAWDOWN_HALT: frozenset({HaltScope.FIRM, HaltScope.BOOK}),
}

SYSTEM_ACTOR = "system"
"""Who entered a halt the system entered for itself. An operator's own name is
anything else; nothing checks it is a person, it is what they said they are."""


class HaltRequest(CanonicalModel):
    """A halt someone asked for. ``reason`` is a short code a system trigger
    sets (``guard_escalation``) or the operator's own words."""

    kind: HaltKind
    scope: HaltScope
    target: NonEmptyStr | None
    reason: NonEmptyStr
    actor: NonEmptyStr

    @model_validator(mode="after")
    def scope_fits_the_kind_and_names_a_target(self) -> Self:
        if self.scope not in _SCOPES_BY_KIND[self.kind]:
            raise ValueError(f"a {self.kind.value} halt cannot have {self.scope.value} scope")
        if (self.scope is HaltScope.FIRM) != (self.target is None):
            raise ValueError("a firm halt names no target, and every other scope names one")
        return self

    def same_switch(self, other: HaltRequest) -> bool:
        """Whether ``other`` is already this halt: the same kind on the same thing."""

        return (self.kind, self.scope, self.target) == (other.kind, other.scope, other.target)


class Halt(HaltRequest):
    halt_id: NonEmptyStr
    entered_at: datetime

    @field_validator("entered_at")
    @classmethod
    def normalize(cls, value: datetime) -> datetime:
        try:
            return ensure_utc(value)
        except TimestampError as error:
            raise ValueError(str(error)) from error

    def blocks(self, *, book: str, instrument_id: str, strategy_id: str) -> bool:
        """Whether this halt stops a new order for that book, instrument and strategy."""

        if self.scope is HaltScope.FIRM:
            return True
        subject = {
            HaltScope.BOOK: book,
            HaltScope.INSTRUMENT: instrument_id,
            HaltScope.STRATEGY: strategy_id,
        }[self.scope]
        return self.target == subject


def blocking(
    halts: Iterable[Halt], *, book: str, instrument_id: str, strategy_id: str
) -> tuple[Halt, ...]:
    return tuple(
        halt
        for halt in halts
        if halt.blocks(book=book, instrument_id=instrument_id, strategy_id=strategy_id)
    )
