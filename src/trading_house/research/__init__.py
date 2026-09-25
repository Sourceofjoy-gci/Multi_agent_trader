"""The strategy foundry boundary: what a candidate strategy is, and what it took to get there."""

from trading_house.research.canonical import canonical_bytes, canonical_sha256
from trading_house.research.packages import PromotionStage, StrategyPackage, StrategySpec
from trading_house.research.trial_ledger import (
    CostAttributionStatus,
    HoldoutState,
    LedgerEvent,
    LedgerEventType,
    LedgerIntegrityReport,
    LedgerRecord,
    RegistrationState,
    ReturnSeriesBasis,
    ScopeKind,
    Trial,
    TrialCounters,
    TrialLedger,
    TrialProtocol,
    TrialSpec,
    TrialStatus,
    deflation_trial_count,
)

__all__ = [
    "CostAttributionStatus",
    "HoldoutState",
    "LedgerEvent",
    "LedgerEventType",
    "LedgerIntegrityReport",
    "LedgerRecord",
    "PromotionStage",
    "RegistrationState",
    "ReturnSeriesBasis",
    "ScopeKind",
    "StrategyPackage",
    "StrategySpec",
    "Trial",
    "TrialCounters",
    "TrialLedger",
    "TrialProtocol",
    "TrialSpec",
    "TrialStatus",
    "canonical_bytes",
    "canonical_sha256",
    "deflation_trial_count",
]
