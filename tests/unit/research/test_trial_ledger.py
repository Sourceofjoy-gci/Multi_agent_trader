import pytest
from pydantic import ValidationError

from trading_house.research.trial_ledger import Trial, TrialStatus, deflation_trial_count


def _trial(index: int, status: TrialStatus) -> Trial:
    return Trial(
        trial_id=f"t-{index}",
        spec_id="s-1",
        agent_run_id="r-1",
        status=status,
        sharpe=1.0 if status is TrialStatus.COMPLETED else None,
        registered_at_sequence=index,
    )


def test_abandoned_trials_still_count_toward_deflation() -> None:
    """I-12: an agent loop that only logs its winners makes every statistic fiction."""

    trials = [
        _trial(1, TrialStatus.COMPLETED),
        _trial(2, TrialStatus.ABANDONED),
        _trial(3, TrialStatus.FAILED),
    ]
    assert deflation_trial_count(trials) == 3


def test_deflation_count_is_never_the_shortlist() -> None:
    trials = [_trial(i, TrialStatus.ABANDONED) for i in range(1, 51)]
    trials.append(_trial(51, TrialStatus.COMPLETED))
    completed = [t for t in trials if t.status is TrialStatus.COMPLETED]
    assert deflation_trial_count(trials) == 51
    assert deflation_trial_count(trials) != len(completed)


def test_a_completed_trial_requires_a_sharpe() -> None:
    with pytest.raises(ValidationError, match="sharpe"):
        Trial(
            trial_id="t-1",
            spec_id="s-1",
            agent_run_id="r-1",
            status=TrialStatus.COMPLETED,
            sharpe=None,
            registered_at_sequence=1,
        )


def test_every_trial_names_the_agent_run_that_produced_it() -> None:
    assert "agent_run_id" in Trial.model_fields


@pytest.mark.parametrize("status", [TrialStatus.ABANDONED, TrialStatus.FAILED])
def test_unfinished_trials_may_have_no_sharpe(status: TrialStatus) -> None:
    """I-12 needs abandoned and failed trials recorded, and they have no result."""

    trial = Trial(
        trial_id="t-1",
        spec_id="s-1",
        agent_run_id="r-1",
        status=status,
        sharpe=None,
        registered_at_sequence=1,
    )

    assert trial.sharpe is None
    assert trial.status is status
