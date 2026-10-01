"""Phase 8C3: the bootstrap Monte Carlo of the drawdown halt and of a loss.

The exact cases are hand-derived. Where the stationary bootstrap's randomness would hide
an answer, the case is either one whose every resample is the same (a constant series)
or is fed hand-written index paths to ``_simulate``, the streaming core the public function
wraps: then each replicate is one path written out and its drawdown worked from first
principles in the comment above it.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from trading_house.core.errors import StatisticalInputError
from trading_house.research.trial_ledger import ReturnSeriesBasis
from trading_house.research.validation.bootstrap import POLICY_VERSION, bootstrap_seed
from trading_house.research.validation.montecarlo import (
    McResult,
    _fractions,
    _simulate,
    drawdown_loss_probabilities,
    mc_seed,
)
from trading_house.research.validation.series import ReturnSeries

PROJECT_ROOT = Path(__file__).resolve().parents[4]
DIGEST = "e" * 64


def _series(
    values: list[float], basis: ReturnSeriesBasis = ReturnSeriesBasis.MARK_TO_MARKET
) -> ReturnSeries:
    first = date(2024, 1, 1)
    return ReturnSeries(
        days=tuple(first + timedelta(days=i) for i in range(len(values))),
        values=np.array(values, dtype=np.float64),
        basis=basis,
        evidence_sha256=DIGEST,
    )


def _run(values: list[float], *, replicates: int = 20, block: int = 5, seed: int = 3) -> McResult:
    return drawdown_loss_probabilities(
        _series(values), replicates=replicates, block_length=block, seed=seed
    )


def _paths(*rows: list[int]) -> list[np.ndarray]:
    """Hand-written replicates (one row each) as the per-step index arrays ``_simulate`` takes."""

    return [np.array(step, dtype=np.int64) for step in zip(*rows, strict=True)]


# --- exact, random-free cases ----------------------------------------------------------


def test_all_zero_returns_never_halt_and_never_lose() -> None:
    # Equity stays at exactly 1.0: no fall from the peak, and ending at 1.0 is not a loss.
    result = _run([0.0] * 30)

    assert result.p_halt.value == 0.0
    assert result.p_loss.value == 0.0


def test_a_constant_minus_two_percent_day_halts_and_loses_in_every_replicate() -> None:
    """Every resample of a constant series is that constant, so there is no randomness.
    Equity after k days is 0.98**k and the fall from the peak 1.0 is 1 - 0.98**k:
    k=5 gives 0.0961 (under 10%), k=6 gives 0.1142 (over), and the horizon is 30, so every
    replicate halts; and 0.98**30 = 0.545 is below 1.0, so every replicate loses."""

    result = _run([-0.02] * 30)

    assert result.p_halt.value == 1.0
    assert result.p_loss.value == 1.0


def test_the_horizon_is_the_series_length_so_a_halt_on_the_last_day_counts() -> None:
    """q = 1 - 0.0036 = 0.9964 each day. Fall after k days is 1 - q**k (by logs: ln q =
    -0.0036065, so q**29 = exp(-0.10459) = 0.9007 and q**30 = exp(-0.10820) = 0.8975).
    After 29 days the fall is 0.0993 (under 10%), after 30 it is 0.1025 (over): the series
    halts on its LAST day, so a horizon one short would not see it."""

    result = _run([-0.0036] * 30)

    assert result.horizon_days == 30
    assert result.p_halt.value == 1.0


def test_a_halt_one_day_past_the_horizon_is_not_counted() -> None:
    """q = 0.9965: q**30 = exp(-0.105184) = 0.9002 (fall 0.0998, under 10%) and
    q**31 = exp(-0.108690) = 0.8970. Over 30 days the fall never reaches 10%, so a horizon
    one long would count a halt that the declared window does not contain."""

    result = _run([-0.0035] * 30)

    assert result.horizon_days == 30
    assert result.p_halt.value == 0.0
    assert result.p_loss.value == 1.0  # still below 1.0 at the end


def test_a_constant_gain_neither_halts_nor_loses() -> None:
    result = _run([0.01] * 30)

    assert (result.p_halt.value, result.p_loss.value) == (0.0, 0.0)


def test_the_result_states_what_was_run() -> None:
    result = _run([0.0] * 45, replicates=7, block=4, seed=99)

    assert (result.seed, result.block_length, result.replicates) == (99, 4, 7)
    assert result.horizon_days == 45
    assert result.policy_version == "8c-mc-1"
    assert result.halt_level == 0.10
    assert (result.p_halt.name, result.p_loss.name) == ("p_halt", "p_loss")
    for item in (result.p_halt, result.p_loss):
        assert item.evidence_sha256 == (DIGEST,)
        assert item.basis_is_mark_to_market is True


def test_a_realized_trades_series_is_flagged_as_such() -> None:
    result = drawdown_loss_probabilities(
        _series([0.0] * 30, ReturnSeriesBasis.REALIZED_CLOSED_TRADES),
        replicates=5,
        block_length=3,
        seed=1,
    )

    assert result.p_halt.basis_is_mark_to_market is False
    assert result.p_loss.basis_is_mark_to_market is False


def test_the_replicate_count_is_honoured() -> None:
    """Every probability is a multiple of 1 / replicates: 7 replicates of a mixed series."""

    values = [0.08 if i % 2 == 0 else -0.09 for i in range(30)]
    result = _run(values, replicates=7, block=4, seed=5)

    for item in (result.p_halt, result.p_loss):
        assert item.value is not None
        assert math.isclose(item.value * 7, round(item.value * 7), abs_tol=1e-9)
    assert result.replicates == 7
    assert result.p_loss.value is not None
    assert 0.0 < result.p_loss.value < 1.0, "an all-or-nothing case would hide the count"
    one = _run(values, replicates=1, block=4, seed=5)
    assert one.p_halt.value in (0.0, 1.0)


def test_the_simulation_runs_exactly_the_requested_replicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spy on the core: it is handed the requested count and every array it returns has it."""

    import trading_house.research.validation.montecarlo as module

    seen: list[tuple[int, int, int]] = []
    real = module._simulate

    def spy(values, steps, replicates):  # type: ignore[no-untyped-def]
        worst, final = real(values, steps, replicates)
        seen.append((replicates, len(worst), len(final)))
        return worst, final

    monkeypatch.setattr(module, "_simulate", spy)
    for count in (1, 3, 7):
        _run([0.08 if i % 2 == 0 else -0.09 for i in range(30)], replicates=count)

    assert seen == [(1, 1, 1), (3, 3, 3), (7, 7, 7)]


# --- the streaming core, with every replicate written out ------------------------------

# values: 0 -> +1.0, 1 -> -0.5, 2 -> 0.0, 3 -> -0.25, 4 -> +0.25, 5 -> +9.0, 6 -> -0.1, 7 -> -0.09
VALUES = np.array([1.0, -0.5, 0.0, -0.25, 0.25, 9.0, -0.1, -0.09], dtype=np.float64)


def test_each_replicate_of_a_hand_sized_case_has_its_hand_derived_drawdown_and_final() -> None:
    steps = _paths(
        # r0: 1 -> 2 -> 1 -> 1: peak 2, trough 1, fall 1/2; final 1.0 (not below 1.0).
        [0, 1, 2],
        # r1: 1 -> 1 -> 1 -> 0.75: fall 1/4; final 0.75.
        [2, 2, 3],
        # r2: 1 -> 0.75 -> 0.9375: fall 1/4 at the trough, final 0.9375 = 0.75 * 1.25.
        [3, 4, 2],
        # r3: 1 -> 2 -> 4 -> 4: never falls; final 4.0 (up).
        [0, 0, 2],
    )

    worst, final = _simulate(VALUES, steps, 4)

    assert worst.tolist() == [0.5, 0.25, 0.25, 0.0]
    assert final.tolist() == [1.0, 0.75, 0.9375, 4.0]


def test_the_peak_is_tracked_over_time_and_not_fixed_at_the_start() -> None:
    """+100% then -20%: 1 -> 2 -> 1.6. From the running peak 2 the fall is 0.2 (halt);
    measured from the start 1.0 the equity never falls below it (no halt)."""

    values = np.array([1.0, -0.2] + [0.0] * 28, dtype=np.float64)
    worst, final = _simulate(values, [np.array([i]) for i in range(30)], 1)

    assert worst.tolist() == [pytest.approx(0.2)]
    assert final.tolist() == [pytest.approx(1.6)]


def test_the_drawdown_is_the_deepest_fall_and_not_the_final_one() -> None:
    """-50% then +100%: 1 -> 0.5 -> 1.0. The deepest fall is 1/2 although it ends level,
    so it halts, and ending exactly at 1.0 is not a loss."""

    values = np.array([-0.5, 1.0] + [0.0] * 28, dtype=np.float64)
    worst, final = _simulate(values, [np.array([i]) for i in range(30)], 1)

    assert worst.tolist() == [0.5]
    assert final.tolist() == [1.0]


def test_a_fall_of_exactly_ten_percent_is_computed_as_that_and_counted_as_a_halt() -> None:
    """+900% then -10%: 1 -> 10 -> 9, a fall of (10 - 9) / 10 = 0.1, the float 0.1 itself
    (1/10 rounds to it). +900% then -9%: 10 -> 9.1, a fall of 0.09. The halt level is the
    signed 0.10 and at or beyond counts: worst falls of 0.1, 0.09 and 0.11 are halts for the
    first and third only, and final equity of exactly 1.0 is not a loss."""

    natural = [np.array([i]) for i in range(30)]
    exact = np.array([9.0, -0.1] + [0.0] * 28, dtype=np.float64)
    under = np.array([9.0, -0.09] + [0.0] * 28, dtype=np.float64)

    assert _simulate(exact, natural, 1)[0].tolist() == [0.1]
    assert _simulate(under, natural, 1)[0].tolist() == [pytest.approx(0.09)]
    p_halt, p_loss = _fractions(
        np.array([0.1, 0.09, 0.11, 0.0]), np.array([1.0, 0.5, 1.0000001, 0.9999999])
    )
    assert p_halt == 0.5  # 0.1 and 0.11 of four
    assert p_loss == 0.5  # 0.5 and 0.9999999 are below 1.0; 1.0 and 1.0000001 are not


def test_a_replicate_that_ends_exactly_where_it_began_is_not_a_loss() -> None:
    _worst, final = _simulate(VALUES, _paths([1, 0, 2], [0, 1, 2]), 2)

    # r0: 1 -> 0.5 -> 1.0 -> 1.0 ; r1: 1 -> 2 -> 1.0 -> 1.0 (both end exactly at 1.0)
    assert final.tolist() == [1.0, 1.0]
    assert (final < 1.0).tolist() == [False, False]


# --- the seed ----------------------------------------------------------------------------


def test_the_monte_carlo_seed_is_the_bootstrap_recipe_under_its_own_policy_version() -> None:
    # hashlib.sha256(b"spec|attempt|8c-mc-1").digest()[:8] big-endian, computed outside.
    assert mc_seed("spec", "attempt") == 11997609043945306293
    assert mc_seed("spec", "attempt") == bootstrap_seed("spec", "attempt", "8c-mc-1")


def test_the_two_streams_are_independent_because_their_seeds_differ() -> None:
    assert POLICY_VERSION == "8c-sb-1"
    assert bootstrap_seed("spec", "attempt", POLICY_VERSION) == 17572605998880585980
    assert mc_seed("spec", "attempt") != bootstrap_seed("spec", "attempt", POLICY_VERSION)


# --- fail closed ----------------------------------------------------------------------------


def test_a_daily_loss_of_everything_makes_both_undefined() -> None:
    result = _run([-1.0] + [0.0] * 29)

    for item in (result.p_halt, result.p_loss):
        assert item.value is None
        assert "-100%" in (item.undefined_reason or "")
        assert item.evidence_sha256 == (DIGEST,)


def test_equity_that_overflows_makes_both_undefined() -> None:
    result = _run([1e200] * 30)

    for item in (result.p_halt, result.p_loss):
        assert item.value is None
        assert "not finite" in (item.undefined_reason or "")


def test_a_bad_replicate_count_or_block_length_is_refused_even_for_an_undefined_series() -> None:
    for replicates, block in ((0, 5), (5, 0), (5, 31)):
        with pytest.raises(StatisticalInputError):
            _run([-1.0] + [0.0] * 29, replicates=replicates, block=block)


# --- determinism ---------------------------------------------------------------------------

_PROGRAM = """
import json, math
from datetime import date, timedelta
import numpy as np
from trading_house.research.trial_ledger import ReturnSeriesBasis
from trading_house.research.validation.montecarlo import drawdown_loss_probabilities, mc_seed
from trading_house.research.validation.series import ReturnSeries

n = 60
values = [0.0004 + 0.02 * (math.sin(1.7 * i) + 0.5 * math.cos(0.23 * i * i)) for i in range(n)]
series = ReturnSeries(
    days=tuple(date(2024, 1, 1) + timedelta(days=i) for i in range(n)),
    values=np.array(values, dtype=np.float64),
    basis=ReturnSeriesBasis.MARK_TO_MARKET,
    evidence_sha256="e" * 64,
)
result = drawdown_loss_probabilities(
    series, replicates=300, block_length=9, seed=mc_seed("spec", "attempt")
)
print(json.dumps(result.model_dump(mode="json"), sort_keys=True))
"""


def _run_in_subprocess(hash_seed: str) -> str:
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _PROGRAM],
        env={**os.environ, "PYTHONHASHSEED": hash_seed},
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_two_processes_under_different_hash_seeds_simulate_identically() -> None:
    first = _run_in_subprocess("0")
    second = _run_in_subprocess("12345")

    assert first == second
    payload = json.loads(first)
    assert payload["seed"] == 11997609043945306293
    assert payload["replicates"] == 300
    assert payload["p_halt"]["value"] is not None
    assert 0.0 < payload["p_halt"]["value"] < 1.0, "a case that is all-or-nothing proves little"
