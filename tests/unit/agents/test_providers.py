import pytest
from pydantic import ValidationError

from trading_house.agents.providers.base import (
    AgentProvider,
    AgentRun,
    AgentTask,
    Plane,
    ProviderCapabilities,
    RunBudget,
    RunOutcome,
    SandboxHandle,
    may_execute_agent_code,
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


def test_a_sandbox_may_not_be_granted_network_egress() -> None:
    """Spec: the research sandbox has no network egress except a package
    mirror, which this phase does not model -- so it stays unrepresentable."""

    with pytest.raises(ValidationError, match="egress"):
        SandboxHandle(
            sandbox_id="s-1",
            plane=Plane.RESEARCH_SANDBOX,
            network_egress_allowed=True,
            has_broker_credentials=False,
        )


def test_a_hot_plane_task_is_unrepresentable() -> None:
    """Spec: the hot path hosts no agents at all."""

    with pytest.raises(ValidationError, match="hot"):
        AgentTask(task_id="t-1", plane=Plane.HOT, instruction_sha256="h-1")


@pytest.mark.parametrize(
    ("plane", "can_run_shell", "expected"),
    [
        (Plane.RESEARCH_SANDBOX, True, True),
        (Plane.RESEARCH_SANDBOX, False, True),
        (Plane.CONTROL, False, True),
        (Plane.CONTROL, True, False),
        (Plane.HOT, False, True),
        (Plane.HOT, True, False),
    ],
)
def test_may_execute_agent_code_matches_the_plane_capability_matrix(
    plane: Plane, can_run_shell: bool, expected: bool
) -> None:
    """A shell-capable provider paired with a non-shell plane is the only
    forbidden combination (I-11)."""

    capabilities = ProviderCapabilities(
        can_write_code=True,
        can_run_shell=can_run_shell,
        supports_tools=True,
        max_context=1000,
        deterministic_seed=True,
    )
    sandbox = SandboxHandle(
        sandbox_id="s-1",
        plane=plane,
        network_egress_allowed=False,
        has_broker_credentials=False,
    )

    assert may_execute_agent_code(capabilities, sandbox) is expected
