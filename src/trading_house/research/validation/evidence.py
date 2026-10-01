"""Every statistical measurement of one candidate, in one typed document.

Phase 8C3 (spec section 1, S-12). ``StatisticalEvidence`` holds what 8C measured over a
trial's sealed bundles, each as a finite value or a reason, each naming the digests it
came from. It carries no gate, no verdict and no decision: whether any figure is good
enough is 8D's question, and 8D fixes its own limits before it looks.

Nothing here reads a ledger or a store; ``ops/validate.py`` assembles it from inputs that
have already been read.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import NonNegativeInt, PositiveInt

from trading_house.core.values import CanonicalModel, NonEmptyStr
from trading_house.research.trial_ledger import TrialCounters
from trading_house.research.validation.bootstrap import BootstrapResult
from trading_house.research.validation.capacity import CapacityDiagnostic
from trading_house.research.validation.coverage import CoverageResult, CpcvP5Result
from trading_house.research.validation.measurement import Measurement
from trading_house.research.validation.montecarlo import McResult
from trading_house.research.validation.pbo import PboResult
from trading_house.research.validation.psr import DsrResult


class EvidenceDigests(CanonicalModel):
    """The documents the measurements were derived from, by the digest the chain names them."""

    baseline: NonEmptyStr
    compounding: NonEmptyStr | None
    pbo_candidates: tuple[NonEmptyStr, ...]
    """The ranked candidates' baselines, in candidate id order."""
    chain_head: NonEmptyStr
    """The event hash of the chain's last record when the counters were read."""


class ScenarioMeasurement(CanonicalModel):
    multiplier: Decimal
    evidence_sha256: NonEmptyStr | None
    """The stressed run's digest; ``None`` when no run is sealed at this level."""
    expectancy: Measurement
    """Net expectancy over the full sealed sample of that run: NOT a locked out-of-sample one."""


class StatisticalEvidence(CanonicalModel):
    trial_id: NonEmptyStr
    spec_sha256: NonEmptyStr
    attempt_id: NonEmptyStr
    """The baseline run's attempt, which seeds the bootstrap and the Monte Carlo."""
    basis_is_mark_to_market: bool
    """The baseline series' basis: a fact about the input, not a property of the candidate."""
    evidence: EvidenceDigests
    counters: TrialCounters
    first_day: date
    last_day: date
    series_days: PositiveInt
    purge_days: NonNegativeInt
    embargo_days: NonNegativeInt
    cpcv_folds: PositiveInt
    wfa_folds: Measurement
    """The number of walk-forward folds that fit, or why none does."""
    psr: Measurement
    sharpe_annualised: Measurement
    dsr: DsrResult
    pbo: PboResult
    bootstrap: BootstrapResult
    monte_carlo: McResult
    max_drawdown_baseline: Measurement
    max_drawdown_cpcv_paths: tuple[Measurement, ...]
    max_drawdown_compounding: Measurement
    cpcv_p5: CpcvP5Result
    coverage: CoverageResult
    scenario_expectancy: tuple[ScenarioMeasurement, ...]
    capacity: CapacityDiagnostic
