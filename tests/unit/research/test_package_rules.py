"""The pure package rules of Phase 8D2: what a decision does and does not license.

Every rule is checked on both sides, and every refusal is ``PromotionRefusedError`` (exit 21 at
the CLI) with an opaque public message. The reports are built through ``ValidationReport``'s own
validator wherever a legal one exists; the two rules that guard against a report whose validator
was bypassed (nine passing gates, no blocking reason) are checked on ``model_construct`` objects,
which is the only way to hold such a report at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.unit.research.test_promotion import _facts, _holdout, _report, _vector
from trading_house.core.errors import PromotionRefusedError
from trading_house.core.values import AssetClass, Horizon
from trading_house.research.packages import PromotionStage, StrategyPackage, StrategySpec
from trading_house.research.promotion import (
    Decision,
    GateStatus,
    ValidationReport,
    check_package,
    create_package,
    require_paper_approved,
)
from trading_house.research.trial_ledger import HoldoutState

SPEC = StrategySpec(
    spec_id="spec-1",
    hypothesis="declared before any result existed",
    book="fx_scalp",
    horizon=Horizon.SCALP,
    asset_classes=(AssetClass.FX,),
)


def _approved(**change: Any) -> ValidationReport:
    return _report(
        holdout=_holdout(HoldoutState.OPENED),
        facts=_facts(),
        gates=_vector({}),
        blocking_reasons=(),
        decision=Decision.PAPER_APPROVED,
        **change,
    )


def _research_passed() -> ValidationReport:
    return _report(
        holdout=_holdout(HoldoutState.LOCKED),
        facts=_facts(),
        gates=_vector({2: GateStatus.UNAVAILABLE}),
        blocking_reasons=(),
        decision=Decision.RESEARCH_PASSED,
    )


def _rejected() -> ValidationReport:
    return _report()


def _unchecked(report: ValidationReport, **change: Any) -> ValidationReport:
    """The report with fields replaced and NO validator run: a report that cannot exist."""

    return ValidationReport.model_construct(**{**dict(report), **change})


def _make(
    report: ValidationReport, **change: Any
) -> StrategyPackage:  # a package for ``report`` at the stage in ``change``
    arguments: dict[str, Any] = {
        "package_id": "pkg-1",
        "spec": SPEC,
        "source_sha256": "s" * 64,
        "trial_ledger_reference": "h" * 64,
    }
    return create_package(report, report.digest(), **{**arguments, **change})


def _paper(report: ValidationReport) -> StrategyPackage:
    return _make(report, stage=PromotionStage.PAPER, authorization_ref="paper-auth")


def _live(report: ValidationReport, paper: StrategyPackage, **change: Any) -> StrategyPackage:
    return _make(
        report,
        **{
            "stage": PromotionStage.LIVE,
            "authorization_ref": "paper-auth",
            "capital_authorization_ref": "capital-auth",
            "signature_sha256": "signature",
            "paper_package": paper,
            **change,
        },
    )


def _refused(excinfo: pytest.ExceptionInfo[PromotionRefusedError], needle: str) -> None:
    assert str(excinfo.value) == "promotion step refused"
    assert needle in str(excinfo.value.__cause__)


# --- the stage a decision alone yields ------------------------------------------------------


def test_a_decision_alone_yields_a_sandbox_package_whatever_it_decided() -> None:
    for report in (_rejected(), _research_passed(), _approved()):
        package = _make(report)

        assert package.stage is PromotionStage.SANDBOX
        assert (
            package.authorization_ref,
            package.capital_authorization_ref,
            package.signature_sha256,
        ) == (None, None, None)
        assert package.validation_report_sha256 == report.digest()


def test_a_sandbox_package_that_claims_an_authorization_is_refused() -> None:
    with pytest.raises(PromotionRefusedError):
        _make(_rejected(), authorization_ref="paper-auth")


def test_a_package_naming_another_report_than_the_one_supplied_is_refused() -> None:
    report = _approved()
    package = _paper(report)

    with pytest.raises(PromotionRefusedError) as excinfo:
        check_package(package, _rejected(), _rejected().digest())

    _refused(excinfo, "another report")
    check_package(package, report, report.digest())


# --- PAPER ---------------------------------------------------------------------------------


def test_paper_needs_an_approved_decision_and_an_authorization_reference() -> None:
    package = _paper(_approved())

    assert package.stage is PromotionStage.PAPER
    assert package.authorization_ref == "paper-auth"
    assert package.signature_sha256 is None

    with pytest.raises(PromotionRefusedError):
        _make(_approved(), stage=PromotionStage.PAPER)  # no authorization reference


@pytest.mark.parametrize("report", [_rejected, _research_passed])
def test_paper_is_refused_for_any_decision_but_an_approval(report: Any) -> None:
    with pytest.raises(PromotionRefusedError) as excinfo:
        _make(report(), stage=PromotionStage.PAPER, authorization_ref="paper-auth")

    _refused(excinfo, "not PAPER_APPROVED")


def test_an_approval_needs_the_digest_it_is_given_to_be_the_reports_own() -> None:
    report = _approved()

    with pytest.raises(PromotionRefusedError) as excinfo:
        create_package(
            report,
            "f" * 64,
            package_id="pkg-1",
            spec=SPEC,
            source_sha256="s" * 64,
            trial_ledger_reference="h" * 64,
            stage=PromotionStage.PAPER,
            authorization_ref="paper-auth",
        )

    _refused(excinfo, "digest names")


def test_an_approval_reads_the_nine_gates_and_the_reasons_not_only_the_decision() -> None:
    """Defense in depth: a report whose validator was bypassed cannot stand in for one."""

    approved = _approved()
    one_unavailable = _unchecked(approved, gates=_vector({9: GateStatus.UNAVAILABLE}))
    one_fail = _unchecked(approved, gates=_vector({4: GateStatus.FAIL}))
    short = _unchecked(approved, gates=_vector({})[:8])
    with_reason = _unchecked(approved, blocking_reasons=("no locked unseen holdout",))

    for bad, needle in (
        (one_unavailable, "nine gates"),
        (one_fail, "nine gates"),
        (short, "nine gates"),
        (with_reason, "blocking reason"),
    ):
        with pytest.raises(PromotionRefusedError) as excinfo:
            require_paper_approved(bad, bad.digest())
        _refused(excinfo, needle)
    require_paper_approved(approved, approved.digest())


# --- LIVE ----------------------------------------------------------------------------------


def test_live_needs_the_paper_package_a_signature_and_a_distinct_capital_authorization() -> None:
    report = _approved()
    paper = _paper(report)

    package = _live(report, paper)

    assert package.stage is PromotionStage.LIVE
    assert (package.authorization_ref, package.capital_authorization_ref) == (
        "paper-auth",
        "capital-auth",
    )
    assert package.signature_sha256 == "signature"
    check_package(package, report, report.digest(), paper=paper)


def test_a_decision_never_reaches_live_by_itself() -> None:
    report = _approved()
    paper = _paper(report)

    with pytest.raises(PromotionRefusedError) as no_paper:
        _live(report, paper, paper_package=None)
    with pytest.raises(PromotionRefusedError):
        _live(report, paper, signature_sha256=None)
    with pytest.raises(PromotionRefusedError):
        _live(report, paper, capital_authorization_ref=None)
    with pytest.raises(PromotionRefusedError):
        _live(report, paper, authorization_ref=None)
    with pytest.raises(PromotionRefusedError):
        _live(_rejected(), paper)

    _refused(no_paper, "needs the PAPER package")


def test_live_is_refused_when_the_capital_authorization_is_the_paper_one() -> None:
    report = _approved()

    with pytest.raises(PromotionRefusedError) as excinfo:
        _live(report, _paper(report), capital_authorization_ref="paper-auth")

    _refused(excinfo, "must differ")


def test_live_must_carry_the_paper_authorization_it_continues() -> None:
    report = _approved()

    with pytest.raises(PromotionRefusedError) as excinfo:
        _live(report, _paper(report), authorization_ref="someone-elses-auth")

    _refused(excinfo, "carries the paper authorization")


def test_the_package_offered_as_paper_must_be_a_paper_package() -> None:
    report = _approved()
    paper = _paper(report)
    live = _live(report, paper)
    sandbox = _make(report)

    with pytest.raises(PromotionRefusedError) as as_live:
        _live(report, live)
    with pytest.raises(PromotionRefusedError) as as_sandbox:
        _live(report, sandbox)

    _refused(as_live, "not a PAPER package")
    _refused(as_sandbox, "not a PAPER package")


def test_the_paper_package_must_itself_rest_on_this_approval() -> None:
    report = _approved()
    other = _approved(attempt_id="attempt-2")
    assert other.digest() != report.digest()

    with pytest.raises(PromotionRefusedError) as excinfo:
        _live(report, _paper(other))

    _refused(excinfo, "another report")


@pytest.mark.parametrize(
    "change", [{"source_sha256": "z" * 64}, {"spec": SPEC.model_copy(update={"book": "other"})}]
)
def test_the_paper_package_must_be_of_the_same_candidate_and_source(
    change: dict[str, Any],
) -> None:
    report = _approved()
    paper = _paper(report).model_copy(update=change)

    with pytest.raises(PromotionRefusedError) as excinfo:
        _live(report, paper)

    _refused(excinfo, "another candidate or source")


def test_nothing_in_the_rules_signs_or_checks_that_a_person_authorized_anything() -> None:
    """A reference is any non-empty string: the rules compare references, never people."""

    report = _approved()
    package = _make(
        report,
        stage=PromotionStage.PAPER,
        authorization_ref="any text at all",
    )

    assert package.authorization_ref == "any text at all"
    with pytest.raises(PromotionRefusedError):
        _make(report, stage=PromotionStage.PAPER, authorization_ref="   ")
