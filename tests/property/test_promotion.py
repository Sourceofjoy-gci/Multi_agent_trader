"""The decision is a pure function of the nine outcomes, the holdout state and the reasons.

Hypothesis draws status vectors, holdout states and reason subsets (a small space: 3**9
vectors, five states, six reasons) and checks ``decide`` against an oracle written out the
long way from P-7, plus the three safety claims the spec makes about it.

The settings drop Hypothesis's per-example deadline and its ``too_slow`` health check on
purpose: building nine ``GateResult`` models per example is validation-heavy, and on a loaded
machine the default 200 ms deadline and the data-generation health check fail intermittently
for reasons unrelated to the property. Neither setting weakens an assertion.

The draws are BIASED toward the positive branches: a uniform draw over 3**9 vectors almost
never makes eight gates pass, so ``decide`` would be exercised only on REJECTED. Cases start
from an all-research-pass vector, an all-nine-pass vector or a free one, then flip at most one
gate, usually keep the reasons empty, and draw the holdout state from the one the vector
needs more often than from the rest. The test counts the decisions it saw and fails unless
both RESEARCH_PASSED and PAPER_APPROVED were produced, so it cannot go quiet by never
reaching them.
"""

from __future__ import annotations

from collections import Counter

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from trading_house.research.promotion import (
    GATE_NAMES,
    REASON_COST_NOT_ATTRIBUTED,
    REASON_NEGATIVE_EXPECTANCY,
    REASON_NO_DATASET_HASH,
    REASON_NO_LOCKED_HOLDOUT,
    REASON_NO_MARK_TO_MARKET,
    REASON_NOT_PREREGISTERED,
    Decision,
    GateResult,
    GateStatus,
    HoldoutStatus,
    decide,
)
from trading_house.research.trial_ledger import HoldoutState

REASONS = (
    REASON_NEGATIVE_EXPECTANCY,
    REASON_NO_LOCKED_HOLDOUT,
    REASON_NOT_PREREGISTERED,
    REASON_NO_DATASET_HASH,
    REASON_NO_MARK_TO_MARKET,
    REASON_COST_NOT_ATTRIBUTED,
)
SMALL = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def _results(statuses: tuple[GateStatus, ...]) -> tuple[GateResult, ...]:
    return tuple(
        GateResult(
            gate=number,
            name=GATE_NAMES[number],
            status=status,
            measured_value=None if status is GateStatus.UNAVAILABLE else 1.0,
            threshold=None,
            evidence_sha256=("d" * 64,),
            reason="r",
        )
        for number, status in enumerate(statuses, start=1)
    )


def _oracle(
    statuses: tuple[GateStatus, ...], state: HoldoutState, reasons: tuple[str, ...]
) -> Decision:
    """P-7 spelled out: a reason rejects; else approval needs nine passes and a spent
    holdout; else research success needs the eight research passes and a locked holdout."""

    if len(reasons) > 0:
        return Decision.REJECTED
    holdout_gate, research = statuses[1], statuses[:1] + statuses[2:]
    if (
        all(s is GateStatus.PASS for s in research)
        and holdout_gate is GateStatus.PASS
        and state in (HoldoutState.OPENED, HoldoutState.CONSUMED)
    ):
        return Decision.PAPER_APPROVED
    if all(s is GateStatus.PASS for s in research) and state is HoldoutState.LOCKED:
        return Decision.RESEARCH_PASSED
    return Decision.REJECTED


_STATES_BY_KIND = {
    "research": [HoldoutState.LOCKED] * 4 + list(HoldoutState),
    "paper": [HoldoutState.OPENED, HoldoutState.CONSUMED] * 3 + list(HoldoutState),
    "free": list(HoldoutState),
}


@st.composite
def _cases(
    draw: st.DrawFn,
) -> tuple[tuple[GateStatus, ...], HoldoutState, tuple[str, ...]]:
    kind = draw(st.sampled_from(["research"] * 3 + ["paper"] * 3 + ["free"] * 2))
    if kind == "research":
        statuses = [GateStatus.PASS] * 9
        statuses[1] = GateStatus.UNAVAILABLE
    elif kind == "paper":
        statuses = [GateStatus.PASS] * 9
    else:
        statuses = list(draw(st.tuples(*[st.sampled_from(GateStatus)] * 9)))
    if draw(st.booleans()):
        statuses[draw(st.integers(0, 8))] = draw(st.sampled_from(GateStatus))
    state = draw(st.sampled_from(_STATES_BY_KIND[kind]))
    reasons = draw(
        st.one_of(
            st.just(()),
            st.just(()),
            st.lists(st.sampled_from(REASONS), unique=True, min_size=1, max_size=6).map(tuple),
        )
    )
    return tuple(statuses), state, reasons


def test_the_decision_is_a_pure_function_matching_the_oracle_and_the_safety_claims() -> None:
    seen: Counter[Decision] = Counter()

    @given(case=_cases())
    @SMALL
    def check(case: tuple[tuple[GateStatus, ...], HoldoutState, tuple[str, ...]]) -> None:
        statuses, state, reasons = case
        holdout = HoldoutStatus(state=state, reason="r", opened_bundle_count=0)
        results = _results(statuses)

        decision = decide(results, holdout, reasons)
        seen[decision] += 1

        assert decision is decide(results, holdout, reasons)
        assert decision is _oracle(statuses, state, reasons)
        if decision is Decision.RESEARCH_PASSED:
            assert state is HoldoutState.LOCKED
            assert reasons == ()
            assert all(s is GateStatus.PASS for s in statuses[:1] + statuses[2:])
        if decision is Decision.PAPER_APPROVED:
            assert all(status is GateStatus.PASS for status in statuses)
            assert reasons == ()
            assert state in (HoldoutState.OPENED, HoldoutState.CONSUMED)
        if state in (HoldoutState.NOT_DEFINED, HoldoutState.CONTAMINATED):
            assert decision is Decision.REJECTED

    check()

    assert seen[Decision.RESEARCH_PASSED] > 0
    assert seen[Decision.PAPER_APPROVED] > 0
    assert seen[Decision.REJECTED] > 0
