from trading_house.research.packages import PromotionStage, StrategyPackage


def test_a_package_cannot_reach_live_without_a_signature() -> None:
    assert "signature_sha256" in StrategyPackage.model_fields
    assert "trial_ledger_reference" in StrategyPackage.model_fields


def test_paper_promotion_needs_no_human_but_live_does() -> None:
    assert PromotionStage.PAPER.requires_human_signature is False
    assert PromotionStage.LIVE.requires_human_signature is True
