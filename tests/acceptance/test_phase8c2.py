"""Phase 8C2 acceptance: PSR, DSR, PBO and the bootstrap over real sealed bundles.

8C2 adds no command (8C3's ``validate`` assembles the measurements), so this file is the
interface. It claims three things, against real PostgreSQL and a real evidence store:

1. **every measurement is finite or says why not, and names its evidence.** The series
   and trade sample are read out of the trial's own sealed 1.0x bundle through the
   evidence store, the counters and chain head from the chain, and nothing is printed or
   mocked. The 36-day run is short and nearly flat, so some statistics may legitimately be
   undefined; what must never happen is a non-finite number, a value with a reason, or a
   measurement that names no bundle.
2. **PBO ranks the sealed candidates and lists the rest.** With one of the protocol's two
   candidates sealed it is undefined and the other is listed as excluded; with both sealed
   it is a fraction of splits or an undefined with a reason, and names both bundles.
3. **no decision vocabulary and the README says what is true.**

Fixtures are the 8C1 acceptance's own (a real 35-day bar store and the real orchestrator).
"""

# ruff: noqa: F811
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from tests.acceptance.test_phase8b2b import _NO_VERDICT_HELP, _names_and_text
from tests.acceptance.test_phase8c1 import SERIES_DAYS, _flow, long_seeded  # noqa: F401
from tests.integration.research.conftest import research_env  # noqa: F401
from tests.integration.research.test_backtest_evidence import Fixture
from tests.integration.research.test_scenarios import (
    OTHER_TRIAL_ID,
    TRIAL_ID,
    _register,
    _scenarios,
)
from tests.integration.research.test_trial_cli import _ledger, _store
from tests.unit.ops.test_scenarios import _NO_VERDICT
from trading_house.core.errors import ScenarioEvidenceError
from trading_house.ops.compounding import sealed_baseline
from trading_house.ops.scenarios import registered_protocol, sealed_bundles
from trading_house.research.evidence import EvidenceBundle, EvidenceStore
from trading_house.research.ledger_store import PostgresTrialLedger
from trading_house.research.validation.bootstrap import bootstrap_mean
from trading_house.research.validation.measurement import Measurement
from trading_house.research.validation.pbo import PboResult, pbo
from trading_house.research.validation.psr import dsr, psr, sharpe_annualised
from trading_house.research.validation.series import ReturnSeries, TradeSample
from trading_house.research.validation.splits import (
    cpcv_folds,
    cpcv_splits,
    hours_to_days,
)

README = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("isolated_research_ledger")]


def _sealed(
    trial_id: str, ledger: PostgresTrialLedger, store: EvidenceStore
) -> tuple[str, EvidenceBundle]:
    return sealed_baseline(sealed_bundles(ledger.events_for(trial_id), store.read), trial_id)


def _assert_honest(item: Measurement, digests: tuple[str, ...]) -> None:
    """Finite, or a reason; never both and never neither; always the bundle's digest."""

    assert item.evidence_sha256 == digests
    assert (item.value is None) != (item.undefined_reason is None)
    if item.value is not None:
        assert math.isfinite(item.value)
    else:
        assert item.undefined_reason


def _pbo_over(
    trial_ids: tuple[str, ...],
    long_seeded: Fixture,
    ledger: PostgresTrialLedger,
    store: EvidenceStore,
) -> tuple[PboResult, tuple[str, ...]]:
    """Every candidate the protocol declares, ranked if sealed and excluded if not."""

    protocol = registered_protocol(ledger.replay(), trial_ids[0])
    policy = protocol.validation
    ranked: dict[str, TradeSample] = {}
    digests: list[str] = []
    excluded: list[str] = []
    series: ReturnSeries | None = None
    for spec in protocol.candidates:
        try:
            digest, bundle = _sealed(spec.trial_id, ledger, store)
        except ScenarioEvidenceError:
            excluded.append(spec.trial_id)
            continue
        ranked[spec.trial_id] = TradeSample.from_bundle(bundle)
        digests.append(digest)
        series = ReturnSeries.from_bundle(bundle, digest)
    assert series is not None
    folds = cpcv_folds(series.days, policy.cpcv_folds)
    result = pbo(
        ranked,
        excluded,
        series.days,
        folds,
        cpcv_splits(policy.cpcv_folds),
        purge_days=hours_to_days(policy.purge_hours),
        embargo_days=hours_to_days(policy.embargo_hours),
        primary_metric=policy.primary_metric,
        evidence_sha256=digests,
        basis_is_mark_to_market=series.promotion_grade,
    )
    return result, tuple(digests)


def test_every_measurement_over_a_real_sealed_run_is_finite_or_says_why(
    long_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    _flow(long_seeded, tmp_path)
    ledger, store = _ledger(research_ledger_dsn), _store(research_env)
    digest, bundle = _sealed(TRIAL_ID, ledger, store)
    series = ReturnSeries.from_bundle(bundle, digest)
    counters = ledger.counters()
    head = ledger.events()[-1].event_hash
    protocol = registered_protocol(ledger.replay(), TRIAL_ID)
    spec_sha256 = bundle.spec_sha256

    assert len(series.days) == SERIES_DAYS
    assert series.promotion_grade is True
    # One candidate's grid is three audit attempts but one selection lottery and one
    # effective specification (umbrella 5.6): the denominator is 1, not 3.
    assert (
        counters.audit_attempts,
        counters.selection_lotteries,
        counters.effective_specifications,
    ) == (3, 1, 1)

    deflated = dsr(series, trials=counters, horizon_days=1, chain_head_sha256=head)
    boot = bootstrap_mean(
        series,
        spec_sha256=spec_sha256,
        attempt_id="grid-1",
        replicates=300,
        c=float(protocol.validation.bootstrap_c),
    )
    measurements = [
        psr(series),
        sharpe_annualised(series),
        deflated.dsr,
        boot.bootstrap_lower_bound,
    ]

    for item in measurements:
        _assert_honest(item, (digest,))
        assert item.basis_is_mark_to_market is True
    assert (deflated.trials, deflated.chain_head_sha256) == (1, head)
    assert deflated.dsr.value == measurements[0].value  # N = 1 is PSR at benchmark 0
    assert deflated.horizon_days == 1
    assert boot.replicates == 300
    assert boot.block_length == 15  # 6.7 * (36 / 3) ** (1 / 3) = 15.34
    assert boot.p5 <= boot.mean <= boot.p95
    # Nothing was invented for a run that is only 36 days long: a defined statistic is a
    # probability, and the bootstrap is a re-sampling of this run's own numbers.
    for item in (measurements[0], measurements[2]):
        assert item.value is None or 0.0 <= item.value <= 1.0
    # Reading again reproduces the bootstrap exactly: the seed is the chain's, not a clock's.
    again = bootstrap_mean(
        series,
        spec_sha256=spec_sha256,
        attempt_id="grid-1",
        replicates=300,
        c=float(protocol.validation.bootstrap_c),
    )
    assert again == boot


def test_pbo_with_one_sealed_candidate_is_undefined_and_lists_the_other(
    long_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    _flow(long_seeded, tmp_path)
    ledger, store = _ledger(research_ledger_dsn), _store(research_env)

    result, digests = _pbo_over((TRIAL_ID,), long_seeded, ledger, store)

    _assert_honest(result.pbo, digests)
    assert result.pbo.value is None
    assert result.candidates == (TRIAL_ID,)
    assert result.excluded == (OTHER_TRIAL_ID,)
    assert result.splits == ()


def test_pbo_over_both_sealed_candidates_is_a_fraction_or_a_reason_and_names_both_bundles(
    long_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    protocol_path = _flow_path(long_seeded, tmp_path)
    _scenarios(long_seeded, protocol_path, trial_id=OTHER_TRIAL_ID, attempt_prefix="other")
    ledger, store = _ledger(research_ledger_dsn), _store(research_env)

    result, digests = _pbo_over((TRIAL_ID, OTHER_TRIAL_ID), long_seeded, ledger, store)

    assert len(digests) == 2
    counters = ledger.counters()
    assert (counters.selection_lotteries, counters.effective_specifications) == (2, 2)
    series = ReturnSeries.from_bundle(_sealed(TRIAL_ID, ledger, store)[1], digests[0])
    two = dsr(
        series, trials=counters, horizon_days=1, chain_head_sha256=ledger.events()[-1].event_hash
    )
    assert two.trials == 2
    assert two.expected_max_z is not None
    assert 0.0 < two.expected_max_z < 1.0
    _assert_honest(two.dsr, (digests[0],))
    _assert_honest(result.pbo, digests)
    assert result.candidates == (TRIAL_ID, OTHER_TRIAL_ID)
    assert result.excluded == ()
    if result.pbo.value is not None:
        assert 0.0 <= result.pbo.value <= 1.0
        assert len(result.splits) == 15
    else:
        assert result.splits == ()
    # the candidates differ only in their specification digest, so the replays are the same
    # run: PBO has no basis to prefer either and must say so by tying, not by inventing a rank
    assert all(s.is_best == (TRIAL_ID, OTHER_TRIAL_ID) for s in result.splits)


def _flow_path(long_seeded: Fixture, tmp_path: Path) -> Path:
    path = _register(tmp_path, long_seeded)
    _scenarios(long_seeded, path)
    return path


def test_the_measurements_carry_no_decision_vocabulary(
    long_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    _flow(long_seeded, tmp_path)
    ledger, store = _ledger(research_ledger_dsn), _store(research_env)
    digest, bundle = _sealed(TRIAL_ID, ledger, store)
    series = ReturnSeries.from_bundle(bundle, digest)
    deflated = dsr(
        series,
        trials=ledger.counters(),
        horizon_days=1,
        chain_head_sha256=ledger.events()[-1].event_hash,
    )
    boot = bootstrap_mean(
        series, spec_sha256=bundle.spec_sha256, attempt_id="grid-1", replicates=50, c=6.7
    )
    pbo_result, _ = _pbo_over((TRIAL_ID,), long_seeded, ledger, store)
    document = {
        "psr": json.loads(psr(series).model_dump_json()),
        "dsr": json.loads(deflated.model_dump_json()),
        "bootstrap": json.loads(boot.model_dump_json()),
        "pbo": json.loads(pbo_result.model_dump_json()),
    }

    names = _names_and_text(document)
    keys = _keys(document)
    assert names, "a document with no keys or values would pass vacuously"
    assert "basis_is_mark_to_market" in keys
    assert "promotion_grade" not in keys
    assert [
        key for key in keys for word in (*_NO_VERDICT_HELP, "total") if word in key.lower()
    ] == []
    assert sorted(text for text in names for word in _NO_VERDICT if word in text.lower()) == []


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(k) for k in value} | set().union(*(_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(v) for v in value))
    return set()


# --- the README --------------------------------------------------------------------


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(heading: str) -> str:
    start = README.index(heading)
    end = README.find("\n## ", start + 1)
    return README[start : end if end != -1 else len(README)]


def test_the_readme_states_what_8c2_measures_and_what_it_resolved() -> None:
    section = _flat(_section("## Phase 8C2"))

    assert README.index("## Phase 8C1") < README.index("## Phase 8C2")
    for sentence in (
        "It adds **no gate, no verdict and no threshold decision**",
        "**no command**: 8C3's `validate` assembles every measurement",
        "NumPy remains the only numerical dependency",
        "`NaN` and `inf` are refused at construction",
        "never the audit count",
        "Candidates with no sealed baseline are listed as excluded and never ranked.",
        "**R-1 (PBO direction).**",
        "**ascending (1 = worst)**",
        "`lambda = (rank - 1) / (N - 1)`",
        "`lambda <= 1/2` (`logit <= 0`)",
        "**R-2 (per-day Sharpe).**",
        "would scale `z` by about 19",
        "`sharpe_annualised`",
        "**R-5 (DSR benchmark).**",
        "`z = sqrt(n - 1) * sr_d / sqrt(variance_term) - E[max Z]`",
        "equals PSR at `N = 1`",
        "**R-6 (bootstrap percentiles).**",
        "the `lower` and the 95th the `higher` neighbouring replicate mean",
        "**DSR's horizon sentence is honoured only for `horizon_days == 1`.**",
        "**`N` is chain-global.**",
        "`8c-sb-1`",
    ):
        assert sentence in section, sentence
