"""Phase 8 final acceptance: the whole story, the real paths, and what the README promises.

Against real PostgreSQL, a real evidence store and real bars. Two kinds of path:

* **The full story on a synthetic locked window.** Register with a LOCKED holdout, run the grid and
  the compounding rerun, ``validate``, then (because gate 9 is UNAVAILABLE for every candidate, so
  no real decision reaches RESEARCH_PASSED) a synthetic RESEARCH_PASSED written by the named test
  helper, ``open-holdout``, a real ``decide`` that evaluates gate 2, a synthetic PAPER_APPROVED,
  ``package create`` PAPER and LIVE, and ``package verify``. The two synthetic decisions are the
  only things not measured; the helper lives in ``tests/`` and no command can reach it.
* **The real paths.** A legacy-imported, Session-Momentum-shaped trial decides REJECTED with all
  six reasons and cannot produce a package; a real prospective candidate decides REJECTED (capacity
  and holdout) and cannot produce a package either. Nothing synthetic is involved in either.

The README closes the phase by saying plainly that no candidate can currently be promoted.
"""

# ruff: noqa: F811
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.acceptance.test_phase8b2b import _assert_refused, _envelope
from tests.acceptance.test_phase8d1 import SIX_REASONS, _legacy_trial
from tests.acceptance.test_phase8d2 import (
    BASE,
    _create,
    _decide,
    _holdout,
    _locked_protocol,
    _open,
    _package_file,
    _register,
    _state,
    _synthetic,
    _trial,
    _verify_package,
    opening_seeded,  # noqa: F401
)
from tests.integration.research.conftest import research_env  # noqa: F401
from tests.integration.research.test_backtest_evidence import Fixture
from tests.integration.research.test_compounding import _compounding
from tests.integration.research.test_scenarios import TRIAL_ID, _protocol, _scenarios
from tests.integration.research.test_trial_cli import _ledger, _verify
from trading_house import cli
from trading_house.research.promotion import Decision

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("isolated_research_ledger")]

README = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")


def test_the_whole_story_from_a_locked_registration_to_a_verified_live_package(
    opening_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    protocol = _locked_protocol(opening_seeded)
    path = _register(tmp_path, protocol)
    _scenarios(opening_seeded, path)
    assert _compounding(opening_seeded, path).exit_code == cli.ExitCode.OK
    assert _holdout() == ("locked", 0)
    measured = _envelope(_trial("validate", "--trial-id", TRIAL_ID))
    assert measured["capacity"]["status"] == "unavailable"

    # what the framework really says about this candidate today, before anything synthetic
    first = _envelope(_decide())
    assert first["decision"] == "REJECTED"
    assert first["gates"][1]["status"] == "UNAVAILABLE"  # the holdout is locked, not opened
    assert first["gates"][8]["status"] == "UNAVAILABLE"  # capacity
    assert first["holdout"]["state"] == "locked"
    assert "dataset-content hash is unavailable" in first["blocking_reasons"]
    refused = tmp_path / "refused.json"
    _assert_refused(_open(opening_seeded, path), cli.ExitCode.PROMOTION_REFUSED)
    _assert_refused(
        _create(*BASE, "--stage", "paper", "--authorization-ref", "A", "--out", str(refused)),
        cli.ExitCode.PROMOTION_REFUSED,
    )
    assert not refused.exists()

    # the one synthetic step before the opening: a decision no candidate can earn
    research_passed = _synthetic(research_ledger_dsn, research_env, Decision.RESEARCH_PASSED)
    assert _envelope(_trial("report", "--trial-id", TRIAL_ID))["report_sha256"] == research_passed

    opened = _open(opening_seeded, path)
    assert opened.exit_code == cli.ExitCode.OK, opened.stderr
    assert _holdout() == ("opened", 3)

    # a real decide reads the opened bundles: gate 2 evaluates and passes on this ramp; the decision
    # is still REJECTED because capacity cannot pass, and the holdout is now consumed
    decided = _envelope(_decide())
    assert decided["gates"][1]["status"] == "PASS"
    assert decided["decision"] == "REJECTED"
    assert decided["holdout"]["state"] == "opened"
    assert _holdout() == ("consumed", 3)
    _assert_refused(_open(opening_seeded, path), cli.ExitCode.PROMOTION_REFUSED)

    # the other synthetic step: an approval, and a real package from it
    approved = _synthetic(research_ledger_dsn, research_env, Decision.PAPER_APPROVED, hour=15)
    assert _envelope(_trial("report", "--trial-id", TRIAL_ID))["report"]["decision"] == (
        "PAPER_APPROVED"
    )
    before = _state(research_ledger_dsn, research_env)
    head = _ledger(research_ledger_dsn).events()[-1].event_hash
    paper_file, live_file = tmp_path / "paper.json", tmp_path / "live.json"

    paper = _create(
        *BASE, "--stage", "paper", "--authorization-ref", "PAPER-AUTH", "--out", str(paper_file)
    )
    assert paper.exit_code == cli.ExitCode.OK, paper.stderr
    assert _state(research_ledger_dsn, research_env) == before  # a file, and nothing else
    contents = _package_file(paper_file)
    assert contents["stage"] == "paper"
    assert contents["validation_report_sha256"] == approved
    assert contents["trial_ledger_reference"] == head
    assert contents["source_sha256"] == protocol.strategy_sha256
    assert contents["spec"]["spec_id"] == protocol.candidates[0].spec_id
    assert contents["signature_sha256"] is None

    assert _verify_package("--file", str(paper_file)).exit_code == cli.ExitCode.OK
    live = _create(
        *BASE,
        "--stage",
        "live",
        "--authorization-ref",
        "PAPER-AUTH",
        "--capital-authorization-ref",
        "CAPITAL-AUTH",
        "--signature-sha256",
        "SIGNATURE-REF",
        "--paper-package",
        str(paper_file),
        "--out",
        str(live_file),
    )
    assert live.exit_code == cli.ExitCode.OK, live.stderr
    verified = _verify_package("--file", str(live_file), "--paper-package", str(paper_file))
    assert verified.exit_code == cli.ExitCode.OK, verified.stderr
    assert _envelope(verified)["decision"] == "PAPER_APPROVED"
    assert _state(research_ledger_dsn, research_env) == before
    assert _verify().exit_code == cli.ExitCode.OK  # the chain and every report still verify


def test_a_legacy_trial_decides_rejected_with_all_six_reasons_and_cannot_produce_a_package(
    research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    trial_id = _legacy_trial(tmp_path)
    verdict = _envelope(_decide(trial_id))
    assert verdict["decision"] == "REJECTED"
    assert verdict["blocking_reasons"] == SIX_REASONS
    assert verdict["holdout"]["state"] == "contaminated"
    before = _state(research_ledger_dsn, research_env)
    out = tmp_path / "package.json"

    for stage in ("paper", "live"):
        refused = _create(
            "--trial-id",
            trial_id,
            "--stage",
            stage,
            "--authorization-ref",
            "AUTH-1",
            "--capital-authorization-ref",
            "CAP-1",
            "--signature-sha256",
            "SIG-1",
            "--book",
            "fx_scalp",
            "--horizon",
            "scalp",
            "--asset-class",
            "fx",
            "--out",
            str(out),
        )
        _assert_refused(refused, cli.ExitCode.PROMOTION_REFUSED)

    assert not out.exists()
    assert _state(research_ledger_dsn, research_env) == before


def test_a_real_prospective_candidate_decides_rejected_and_cannot_produce_a_package(
    opening_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    path = _register(tmp_path, _protocol(opening_seeded))  # no holdout declared at all
    _scenarios(opening_seeded, path)

    verdict = _envelope(_decide())

    assert verdict["decision"] == "REJECTED"
    assert verdict["holdout"]["state"] == "not_defined"
    assert "no locked unseen holdout" in verdict["blocking_reasons"]
    assert verdict["gates"][1]["status"] == "UNAVAILABLE"  # the holdout
    assert verdict["gates"][8]["status"] == "UNAVAILABLE"  # capacity
    assert "capacity is unavailable" in verdict["gates"][8]["reason"]
    before = _state(research_ledger_dsn, research_env)
    out = tmp_path / "package.json"
    for argv in (
        ["--stage", "paper", "--authorization-ref", "AUTH-1"],
        [
            "--stage",
            "live",
            "--authorization-ref",
            "AUTH-1",
            "--capital-authorization-ref",
            "CAP-1",
            "--signature-sha256",
            "SIG-1",
        ],
    ):
        _assert_refused(_create(*BASE, *argv, "--out", str(out)), cli.ExitCode.PROMOTION_REFUSED)
    _assert_refused(_open(opening_seeded, path), cli.ExitCode.PROMOTION_REFUSED)
    assert not out.exists()
    assert _state(research_ledger_dsn, research_env) == before
    # and a package file that merely claims the rejected report is refused by verify
    claimed = tmp_path / "claimed.json"
    claimed.write_text(
        json.dumps(
            {
                "package_id": "pkg-claimed",
                "spec": {
                    "spec_id": "spec-trial-1",
                    "hypothesis": "declared before any result existed",
                    "book": "fx_scalp",
                    "horizon": "scalp",
                    "asset_classes": ["fx"],
                },
                "source_sha256": "b" * 64,
                "trial_ledger_reference": _ledger(research_ledger_dsn).events()[-1].event_hash,
                "validation_report_sha256": verdict["report_sha256"],
                "signature_sha256": None,
                "stage": "paper",
                "authorization_ref": "AUTH-1",
                "capital_authorization_ref": None,
            }
        ),
        encoding="utf-8",
    )
    _assert_refused(_verify_package("--file", str(claimed)), cli.ExitCode.PROMOTION_REFUSED)
    assert _state(research_ledger_dsn, research_env) == before


# --- the README says what is true ----------------------------------------------------------------


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(heading: str) -> str:
    start = README.index(heading)
    end = README.find("\n## ", start + 1)
    return README[start : end if end != -1 else len(README)]


def test_the_readme_documents_the_three_new_commands_once_each_in_the_table() -> None:
    for command in (
        "research trial open-holdout",
        "research package create",
        "research package verify",
    ):
        rows = [
            line
            for line in README.splitlines()
            if line.startswith("| `") and f"trading-house {command}`" in line
        ]
        assert len(rows) == 1, command
    assert README.index("## Phase 8D1") < README.index("## Phase 8D2")
    assert README.index("## Phase 8D2") < README.index(
        "## Phase 8 - what exists, what blocks promotion, what a human must supply"
    )


def test_the_readme_states_what_the_opening_and_the_package_commands_do_and_do_not() -> None:
    section = _flat(_section("## Phase 8D2"))

    for sentence in (
        "`open-holdout` is allowed only when the holdout derived from the chain is `LOCKED` "
        "and the trial's latest recorded decision is `RESEARCH_PASSED`",
        "a second invocation: an opened, consumed or contaminated holdout is not locked",
        "The holdout dataset hash is DECLARED, not computed.",
        "Nothing in this repository can hash a bar store",
        'umbrella section 10\'s "dataset hash mismatch" check is not implemented and is not done',
        "One opening is three opened bundles.",
        "`audit_attempts` rises by three",
        "it refuses with exit 21, before the report and both events",
        "A partial opening is final.",
        "`PAPER` requires `authorization_ref`; `LIVE` requires both references and the existing "
        "`signature_sha256`; `SANDBOX` carries none of the three",
        "A decision never reaches `LIVE` by itself",
        "It appends nothing to the ledger, changes no stage and signs nothing",
        "it refuses to overwrite `--out`",
        "the trial's latest `GATE_DECIDED` must name it too",
        "Nothing here signs anything or checks that a person made an authorization.",
        "`open-holdout` is a named exception of the no-verdict help gate",
        "No real candidate can reach `RESEARCH_PASSED` or `PAPER_APPROVED` today.",
        "`tests/integration/research/synthetic_decision.py`",
        "It lives in `tests/` and is unreachable from any command.",
        "`research trial record` cannot seal an opening.",
        "the trades inside are never re-simulated",
        "A holdout opens at most once across trials.",
        "this holdout was opened by trial X",
        "hand-editing any of them is undetectable",
        "A `SANDBOX` package verifies without a `PAPER_APPROVED` decision",
        "the validator refuses one that does",
        "which invalidates a package already created",
        "a stale report does not license a package",
        "A simulator refusal at 1.0x seals nothing",
    ):
        assert sentence in section, sentence


def test_the_readme_closes_phase_8_by_saying_no_candidate_can_be_promoted() -> None:
    closing = _flat(_section("## Phase 8 - what exists, what blocks promotion, what a human"))

    for sentence in (
        "**What exists.**",
        "**What blocks promotion.** No candidate can currently be promoted.",
        "each lives outside this repository",
        "**A capacity model.**",
        "**A real locked holdout with a computable dataset hash.**",
        "**Human authorizations and signatures.**",
        "**What a human must supply.**",
        "`REJECTED`, stage `SANDBOX`, with the reasons",
        "**What a human must read before accepting a package.**",
        "R-2 (the deflated Sharpe uses the per-day Sharpe",
        "R-7 (the published expected-maximum weight",
        "2026-10-01-phase-8c-statistical-validation-design.md",
        "Regime labels are recognised, not vouched.",
        "DSR is defined only for a one-day holding horizon",
        "Opened-bundle contents are never re-simulated.",
        "The declared holdout dataset hash is never computed from any data.",
    ):
        assert sentence in closing, sentence


def test_the_readme_does_not_claim_a_candidate_can_be_promoted_today() -> None:
    text = _flat(_section("## Phase 8D2") + _section("## Phase 8 - what exists")).lower()

    for claim in (
        "can currently be promoted by",
        "is promoted to paper",
        "a candidate has been promoted",
        "reaches live",
        "signs the package",
        "verifies the signature",
        "verifies that a person",
    ):
        assert claim not in text, claim
