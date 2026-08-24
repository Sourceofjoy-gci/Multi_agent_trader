"""Two stores, split by epistemic status. Facts may reach the hot path; beliefs may not."""

from datetime import datetime
from enum import Enum
from typing import Literal, Self

from pydantic import field_validator, model_validator

from trading_house.core.clock import ensure_utc
from trading_house.core.errors import TimestampError
from trading_house.core.values import CanonicalModel, FiniteFloat, InstrumentId, NonEmptyStr


class MemoryStore(str, Enum):  # noqa: UP042
    A = "observed_facts"
    B = "agent_beliefs"


class WriterKind(str, Enum):  # noqa: UP042
    DETERMINISTIC = "deterministic"
    AGENT = "agent"


class _PointInTime(CanonicalModel):
    availability_time: datetime

    @field_validator("availability_time")
    @classmethod
    def normalize(cls, value: datetime) -> datetime:
        try:
            return ensure_utc(value)
        except TimestampError as error:
            raise ValueError(str(error)) from error


class ObservedFact(_PointInTime):
    """Store A (I-13): writable only by deterministic post-trade code."""

    fact_id: NonEmptyStr
    store: Literal[MemoryStore.A]
    written_by: WriterKind
    instrument_id: InstrumentId
    metric: NonEmptyStr
    value: FiniteFloat
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        try:
            return ensure_utc(value)
        except TimestampError as error:
            raise ValueError(str(error)) from error

    @model_validator(mode="after")
    def only_deterministic_code_writes_facts(self) -> Self:
        if self.written_by is not WriterKind.DETERMINISTIC:
            raise ValueError("only deterministic post-trade code may write a fact")
        return self

    @model_validator(mode="after")
    def availability_not_before_observation(self) -> Self:
        if self.availability_time < self.observed_at:
            raise ValueError("availability_time must not precede observed_at")
        return self


class AgentBelief(_PointInTime):
    """Store B: an agent's opinion. Never eligible for Store A."""

    belief_id: NonEmptyStr
    store: Literal[MemoryStore.B]
    agent_run_id: NonEmptyStr
    claim: NonEmptyStr
