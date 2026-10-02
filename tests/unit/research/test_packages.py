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
        "authorization_ref": "paper-auth-1",
    }
    payload.update(overrides)
    return payload


def _live(**overrides: object) -> dict[str, object]:
    return _package(
        **{
            "stage": PromotionStage.LIVE,
            "signature_sha256": "a-real-signature",
            "capital_authorization_ref": "capital-auth-1",
            **overrides,
        }
    )


def test_a_package_cannot_reach_live_without_a_signature() -> None:
    with pytest.raises(ValidationError, match="signature"):
        StrategyPackage(**_live(signature_sha256=None))


def test_a_signed_and_authorized_package_can_reach_live() -> None:
    package = StrategyPackage(**_live())
    assert package.stage is PromotionStage.LIVE


def test_a_package_cannot_reach_live_without_a_capital_authorization() -> None:
    with pytest.raises(ValidationError, match="capital authorization"):
        StrategyPackage(**_live(capital_authorization_ref=None))


@pytest.mark.parametrize("stage", [PromotionStage.PAPER, PromotionStage.LIVE])
def test_a_paper_or_live_package_needs_the_paper_authorization(stage: PromotionStage) -> None:
    overrides = _live() if stage is PromotionStage.LIVE else _package()

    with pytest.raises(ValidationError, match="paper authorization"):
        StrategyPackage(**{**overrides, "authorization_ref": None})


@pytest.mark.parametrize(
    "claim",
    [
        {"authorization_ref": "paper-auth-1"},
        {"capital_authorization_ref": "capital-auth-1"},
        {"signature_sha256": "a-real-signature"},
    ],
)
def test_a_sandbox_package_carries_no_authorization_and_no_signature(
    claim: dict[str, object],
) -> None:
    clean = _package(stage=PromotionStage.SANDBOX, authorization_ref=None)
    assert StrategyPackage(**clean).stage is PromotionStage.SANDBOX

    with pytest.raises(ValidationError, match="sandbox"):
        StrategyPackage(**{**clean, **claim})


@pytest.mark.parametrize(
    "extra", [{"capital_authorization_ref": "capital-auth-1"}, {"signature_sha256": "a-signature"}]
)
def test_a_paper_package_carries_neither_a_capital_authorization_nor_a_signature(
    extra: dict[str, object],
) -> None:
    assert StrategyPackage(**_package()).stage is PromotionStage.PAPER

    with pytest.raises(ValidationError, match="paper package carries no capital"):
        StrategyPackage(**_package(**extra))


def test_a_package_survives_its_own_json_round_trip() -> None:
    package = StrategyPackage(**_live())

    assert StrategyPackage.model_validate_json(package.model_dump_json()) == package


def test_an_unsigned_package_can_reach_paper() -> None:
    package = StrategyPackage(**_package(stage=PromotionStage.PAPER, signature_sha256=None))
    assert package.stage is PromotionStage.PAPER


def test_paper_promotion_needs_no_human_but_live_does() -> None:
    assert PromotionStage.PAPER.requires_human_signature is False
    assert PromotionStage.LIVE.requires_human_signature is True
