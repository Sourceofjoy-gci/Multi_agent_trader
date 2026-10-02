"""A generated strategy is data until it is signed."""

from enum import Enum
from typing import Self

from pydantic import model_validator

from trading_house.core.values import AssetClass, BookId, CanonicalModel, Horizon, NonEmptyStr


class PromotionStage(Enum):
    """Auto-deploy reaches paper. Only a human signature reaches capital."""

    requires_human_signature: bool  # declared for mypy strict; set in __init__

    SANDBOX = ("sandbox", False)
    PAPER = ("paper", False)
    LIVE = ("live", True)

    def __init__(self, wire_value: str, requires_human_signature: bool) -> None:
        self._value_ = wire_value
        self.requires_human_signature = requires_human_signature


class StrategySpec(CanonicalModel):
    spec_id: NonEmptyStr
    hypothesis: NonEmptyStr
    book: BookId
    horizon: Horizon
    asset_classes: tuple[AssetClass, ...]


class StrategyPackage(CanonicalModel):
    package_id: NonEmptyStr
    spec: StrategySpec
    source_sha256: NonEmptyStr
    trial_ledger_reference: NonEmptyStr
    validation_report_sha256: NonEmptyStr
    signature_sha256: NonEmptyStr | None
    stage: PromotionStage
    authorization_ref: NonEmptyStr | None = None
    """A reference to a person's paper authorization. Nothing here checks that one was made."""
    capital_authorization_ref: NonEmptyStr | None = None
    """A reference to a separate capital authorization, required for ``LIVE`` and distinct
    from the paper one. A reference only, like ``signature_sha256``."""

    @model_validator(mode="after")
    def only_a_human_signature_reaches_capital(self) -> Self:
        if self.stage.requires_human_signature and self.signature_sha256 is None:
            raise ValueError(
                f"stage {self.stage.value!r} requires a human signature before it can be reached"
            )
        return self

    @model_validator(mode="after")
    def the_authorizations_match_the_stage(self) -> Self:
        held = (self.authorization_ref, self.capital_authorization_ref, self.signature_sha256)
        if self.stage is PromotionStage.SANDBOX and any(ref is not None for ref in held):
            raise ValueError("a sandbox package carries no authorization and no signature")
        if self.stage is not PromotionStage.SANDBOX and self.authorization_ref is None:
            raise ValueError(f"stage {self.stage.value!r} requires a paper authorization reference")
        if self.stage is PromotionStage.LIVE and self.capital_authorization_ref is None:
            raise ValueError("stage 'live' requires a capital authorization reference")
        return self
