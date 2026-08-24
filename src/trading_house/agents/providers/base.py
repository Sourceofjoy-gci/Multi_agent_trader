"""Coding and reasoning models behind one interface, exactly as brokers are."""

from datetime import datetime
from enum import Enum
from typing import Protocol, Self, runtime_checkable

from pydantic import NonNegativeInt, PositiveInt, model_validator

from trading_house.core.values import CanonicalModel, NonEmptyStr


class Plane(Enum):
    """Where an agent runs decides what it may do (invariant I-11)."""

    shell_permitted: bool  # declared for mypy strict; set in __init__

    RESEARCH_SANDBOX = ("research_sandbox", True)
    CONTROL = ("control", False)
    HOT = ("hot", False)

    def __init__(self, wire_value: str, shell_permitted: bool) -> None:
        self._value_ = wire_value
        self.shell_permitted = shell_permitted


class RunOutcome(str, Enum):  # noqa: UP042
    COMPLETED = "completed"
    FAILED = "failed"
    BUDGET_EXCEEDED = "budget_exceeded"
    REFUSED = "refused"


class ProviderCapabilities(CanonicalModel):
    can_write_code: bool
    can_run_shell: bool
    supports_tools: bool
    max_context: PositiveInt
    deterministic_seed: bool


class RunBudget(CanonicalModel):
    """Every ceiling an unattended agent loop must respect."""

    wall_clock_seconds: PositiveInt
    max_tokens: PositiveInt
    max_cost_usd_millis: PositiveInt
    max_tool_calls: PositiveInt


class SandboxHandle(CanonicalModel):
    sandbox_id: NonEmptyStr
    plane: Plane
    network_egress_allowed: bool
    has_broker_credentials: bool

    @model_validator(mode="after")
    def credentials_never_meet_agent_code(self) -> Self:
        if self.has_broker_credentials:
            raise ValueError("a sandbox may never hold broker credentials")
        return self


class AgentTask(CanonicalModel):
    task_id: NonEmptyStr
    plane: Plane
    instruction_sha256: NonEmptyStr


class AgentRun(CanonicalModel):
    """A forensic record appended to the audit ledger."""

    run_id: NonEmptyStr
    task_id: NonEmptyStr
    provider_id: NonEmptyStr
    model_id: NonEmptyStr
    prompt_sha256: NonEmptyStr
    transcript_sha256: NonEmptyStr
    diff_sha256: NonEmptyStr | None
    outcome: RunOutcome
    tokens_used: NonNegativeInt
    cost_usd_millis_used: NonNegativeInt
    tool_calls_used: NonNegativeInt
    started_at: datetime
    finished_at: datetime


@runtime_checkable
class AgentProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...
    def run(self, task: AgentTask, sandbox: SandboxHandle, budget: RunBudget) -> AgentRun: ...
