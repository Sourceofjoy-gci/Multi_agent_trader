"""Every trial the foundry ran, including the ones it did not like."""

from collections.abc import Iterable, Sequence
from enum import Enum
from typing import Protocol, Self

from pydantic import PositiveInt, model_validator

from trading_house.core.values import CanonicalModel, FiniteFloat, NonEmptyStr


class TrialStatus(str, Enum):  # noqa: UP042
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    FAILED = "failed"


class Trial(CanonicalModel):
    trial_id: NonEmptyStr
    spec_id: NonEmptyStr
    agent_run_id: NonEmptyStr
    status: TrialStatus
    sharpe: FiniteFloat | None
    registered_at_sequence: PositiveInt

    @model_validator(mode="after")
    def completed_trials_report_a_result(self) -> Self:
        if self.status is TrialStatus.COMPLETED and self.sharpe is None:
            raise ValueError("a completed trial requires a sharpe")
        return self


def deflation_trial_count(trials: Iterable[Trial]) -> int:
    """The denominator for DSR and PBO: every trial attempted, without exception."""

    return sum(1 for _ in trials)


class TrialLedger(Protocol):
    def register(self, trial: Trial) -> None: ...
    def all_trials(self, spec_id: str) -> Sequence[Trial]: ...
