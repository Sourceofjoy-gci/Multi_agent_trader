import pytest
from pydantic import ValidationError

from trading_house.core.values import AssetClass, Horizon
from trading_house.research.packages import PromotionStage, StrategyPackage, StrategySpec


def _spec() -> StrategySpec:
    return StrategySpec(
        spec_id="spec-1",
        hypothesis="EURUSD trends after London open",
        book="fx_scalp",
        horizon=Horizon.SCALP,
        asset_classes=(AssetClass.FX,),
    )


def _package(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "package_id": "pkg-1",
        "spec": _spec(),
        "source_sha256": "source-hash",
        "trial_ledger_reference": "trial-ledger-1",
        "validation_report_sha256": "report-hash",
        "signature_sha256": None,
        "stage": PromotionStage.PAPER,
    }
    payload.update(overrides)
    return payload


def test_a_package_cannot_reach_live_without_a_signature() -> None:
    with pytest.raises(ValidationError, match="signature"):
        StrategyPackage(**_package(stage=PromotionStage.LIVE, signature_sha256=None))


def test_a_signed_package_can_reach_live() -> None:
    package = StrategyPackage(
        **_package(stage=PromotionStage.LIVE, signature_sha256="a-real-signature")
    )
    assert package.stage is PromotionStage.LIVE


def test_an_unsigned_package_can_reach_paper() -> None:
    package = StrategyPackage(**_package(stage=PromotionStage.PAPER, signature_sha256=None))
    assert package.stage is PromotionStage.PAPER


def test_paper_promotion_needs_no_human_but_live_does() -> None:
    assert PromotionStage.PAPER.requires_human_signature is False
    assert PromotionStage.LIVE.requires_human_signature is True
