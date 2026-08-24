import pytest
from pydantic import ValidationError

from trading_house.agents.providers.base import (
    AgentProvider,
    AgentRun,
    Plane,
    RunBudget,
    RunOutcome,
    SandboxHandle,
)


def test_shell_is_permitted_only_in_the_research_sandbox() -> None:
    """I-11: no process holding broker credentials may execute agent code."""

    assert Plane.RESEARCH_SANDBOX.shell_permitted is True
    assert Plane.CONTROL.shell_permitted is False
    assert Plane.HOT.shell_permitted is False


def test_a_sandbox_never_carries_credentials() -> None:
    handle = SandboxHandle(
        sandbox_id="s-1",
        plane=Plane.RESEARCH_SANDBOX,
        network_egress_allowed=False,
        has_broker_credentials=False,
    )
    assert handle.has_broker_credentials is False


def test_a_sandbox_with_credentials_is_unrepresentable() -> None:
    with pytest.raises(ValidationError, match="credentials"):
        SandboxHandle(
            sandbox_id="s-1",
            plane=Plane.RESEARCH_SANDBOX,
            network_egress_allowed=False,
            has_broker_credentials=True,
        )


def test_budget_ceilings_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        RunBudget(wall_clock_seconds=0, max_tokens=1, max_cost_usd_millis=1, max_tool_calls=1)


def test_a_run_records_everything_needed_to_reconstruct_it() -> None:
    required = {
        "provider_id",
        "model_id",
        "prompt_sha256",
        "transcript_sha256",
        "diff_sha256",
        "outcome",
        "tokens_used",
        "cost_usd_millis_used",
        "tool_calls_used",
        "started_at",
        "finished_at",
    }
    assert required <= set(AgentRun.model_fields)


def test_budget_exceeded_is_a_first_class_outcome() -> None:
    assert RunOutcome.BUDGET_EXCEEDED in set(RunOutcome)


def test_provider_surface_is_two_methods() -> None:
    assert {n for n in vars(AgentProvider) if not n.startswith("_")} == {"capabilities", "run"}
