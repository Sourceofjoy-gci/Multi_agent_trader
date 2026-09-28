"""Phase 8B2a acceptance: the per-trade cost split is sealed, and the summary is
checked against it.

8B1 put the mark-to-market series in the bundle for one reason: the daily returns
beside it are a *reduction* of that series, and a reduction whose input the
evidence store does not hold cannot be re-derived. Task 3 makes the same argument
for the cost attribution, and then goes one step further than 8B1 did. Until now
``CostSummary`` was an independently asserted total -- four numbers the command
wrote, each checked only against itself. It is now a *checked aggregate*: a
bundle whose summary says ``COMPLETE`` carries the per-trade split that produced
every one of those four numbers, and a bundle cannot claim a completeness the
summary denies.

Seven cases, each through a real command or the real legacy importer rather than a
hand-built document:

1. the coupling holds in **both** directions -- a ``COMPLETE`` summary must carry
   the attribution it aggregates, and a ``PARTIAL`` one must not;
2. an attribution that does not describe the carried result is refused, in
   ``costs_attribution.attribution_disagreement``'s own words, because the bundle
   **calls that predicate** rather than restating it;
3. and so is a split shifted *consistently*, which every other rule in the bundle
   is satisfied by -- the one defect here that only the call can see;
4. a summary whose totals are numbers the trades behind them do not produce is
   refused: the aggregate is checked, not trusted;
5. and that holds for a **legacy** ``PARTIAL`` bundle too, where commission and
   swap are checked against its trades and spread and slippage are not -- the two
   the early return used to skip along with them;
6. the sealed detail is the engine's own split, trade for trade, rather than a
   recomputation of it;
7. and every refusal is one edit from a document that validates, so each is the
   rule rather than an accident of the fixtures.

Every ``match=`` below is the whole clause its own rule speaks in. A ``match``
that several refusals satisfy is a test that keeps passing when the rule it
names is removed, and the point of naming a rule here is that removing it turns
something red.

The helpers are 8B1's, imported rather than re-created: ``_run_command`` and
``_bundle_of`` are a real ``backtest run`` over in-memory bars and the JSON path
every reader takes, ``_sealed_legacy_bundle`` is the production importer and the
content-addressed store, and ``_outcome`` is the same factory the CLI itself
calls. A second copy of any of them would be a second thing that can drift from
the one under test. The one property this file shares rather than states is the
v1 bytes, and it is asserted in ``test_phase8b1.py`` beside
``mark_to_market``'s: one document, one stored-address check, both keys absent.

What is deliberately **not** asserted here: that the bundle's ``spec_sha256`` is
cross-checked against the sealed ``PREREGISTERED`` event, and anything about
promotion. 8B2a gates nothing -- a bundle that verifies is a bundle that was
recorded, not one that passed.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from tests.acceptance.test_phase8b1 import (
    _bundle_of,
    _outcome,
    _run_command,
    _sealed_legacy_bundle,
    _session_ramp,
)
from trading_house.research.backtest.costs_attribution import (
    CostAttribution,
    TradeCostAttribution,
)
from trading_house.research.evidence import EvidenceBundle
from trading_house.research.trial_ledger import CostAttributionStatus


def _prospective_attribution() -> CostAttribution:
    """A well-formed attribution for a run this bundle did not produce.

    A single trade whose split reconstructs its own total, which is all the
    "must not carry one" case needs: the coupling rule is checked before any
    total, so what a summary would have to aggregate never comes into it.
    """

    return CostAttribution(
        trades=(
            TradeCostAttribution(
                proposal_id="p-1",
                market_pnl=Decimal(0),
                spread_cost=Decimal(0),
                slippage_cost=Decimal(0),
                post_fill_gross=Decimal(0),
            ),
        )
    )


def _revised(bundle: EvidenceBundle, **changes: object) -> EvidenceBundle:
    """The same document with named fields changed, revalidated the way a reader does.

    ``model_copy`` would validate nothing at all, which is the opposite of what
    these cases need -- a refusal that never runs is not a refusal. So the
    document goes out as JSON, the named keys are replaced, and it comes back
    through ``model_validate_json``: the same path ``EvidenceStore.read`` takes
    and the same path the command's own output is read over. Dropping a key
    (``None``) rather than writing it as ``null`` is also deliberate -- ``null``
    is a shape nothing here can produce, since every write goes through
    ``canonical_bytes`` and an absent field is excluded.
    """

    document = json.loads(bundle.model_dump_json())
    for name, value in changes.items():
        if value is None:
            del document[name]
        elif isinstance(value, BaseModel):
            document[name] = value.model_dump(mode="json")
        else:
            document[name] = value
    return EvidenceBundle.model_validate_json(json.dumps(document))


def _bumped(amount: Decimal) -> Decimal:
    """One unit away from a measured total, whatever that total was.

    Written as a shift rather than a literal so the case cannot become vacuous
    on a run whose commission happens to equal the literal -- a refusal that
    fires because the number moved is the claim, and a refusal that fires
    because the number was already wrong is an accident.
    """

    return amount + 1


def test_a_complete_cost_summary_must_carry_the_attribution_it_aggregates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The claim ``COMPLETE`` makes, enforced in both spellings.

    8B2a split ``gross_pnl`` into ``market_pnl - spread_cost - slippage_cost``
    and left the decomposition in a command's memory. A summary that says
    ``COMPLETE`` asserts that breakdown whether or not anyone kept it, so a
    ``COMPLETE`` bundle with nothing behind the four numbers is a claim about a
    breakdown it does not carry.
    """

    bundle = _bundle_of(_run_command(monkeypatch, tmp_path, marked=True))

    assert bundle.costs.status is CostAttributionStatus.COMPLETE
    assert bundle.cost_attribution is not None
    with pytest.raises(ValidationError, match="a complete cost summary must carry the attribution"):
        _revised(bundle, cost_attribution=None)


def test_a_partial_summary_must_not_carry_one(tmp_path: Path) -> None:
    """The other direction, through the real importer.

    A v1 legacy bundle is ``PARTIAL`` and carries no attribution. Attaching one
    would claim a completeness the summary explicitly denies, so the bundle must
    refuse rather than seal a document whose two halves disagree -- and the
    attribution it is offered is perfectly well formed, so the coupling is what
    fires.

    The wording is the coupling validator's own, whole. This fixture is a
    one-trade legacy bundle and the attribution offered is one well-formed
    split, which is exactly the shape ``attribution_disagreement`` also objects
    to (``the attribution must be in result order``), so a looser substring
    would be satisfied by that rule instead -- and the test would still be green
    with the coupling deleted. A ``match`` several refusals satisfy is a test
    that keeps passing when the rule it names is removed.
    """

    _store, legacy = _sealed_legacy_bundle(tmp_path)

    assert legacy.costs.status is CostAttributionStatus.PARTIAL
    assert legacy.cost_attribution is None
    with pytest.raises(ValidationError, match="only a complete cost summary may carry"):
        _revised(legacy, cost_attribution=_prospective_attribution())


def test_an_attribution_that_disagrees_with_its_trades_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The two surfaces refuse the same defect in the same words.

    ``attribution_disagreement`` is the rule, and ``BacktestOutcome`` and
    ``EvidenceBundle`` both *call* it. A bundle that restated the three checks
    would drift the moment one copy was edited and the other was not, and the
    drift would be invisible until the two surfaces disagreed about the same
    document in front of a reader. So the wording asserted here is the
    predicate's, and the case is chosen to reach it: dropping a trade leaves a
    tuple that is still internally consistent -- every split still reconstructs
    its own total -- and only the predicate can notice that it no longer covers
    the result.

    The doctored ``post_fill_gross`` beside it is the other half, caught one
    layer down by ``CostAttribution``'s own decomposition rule. Both are
    refusals; naming both is what says the layers are separate.
    """

    bundle = _bundle_of(_run_command(monkeypatch, tmp_path, marked=True))
    assert bundle.cost_attribution is not None
    trades = bundle.cost_attribution.trades
    if not trades:
        pytest.skip("this fixture produced no trades; the run-level checks below still cover it")

    shortened = bundle.cost_attribution.model_copy(update={"trades": trades[:-1]})
    with pytest.raises(ValidationError, match="cover every trade exactly once"):
        _revised(bundle, cost_attribution=shortened)

    tampered = trades[0].model_copy(update={"post_fill_gross": Decimal("999")})
    with pytest.raises(ValidationError, match="must equal its post-fill gross"):
        _revised(
            bundle,
            cost_attribution=bundle.cost_attribution.model_copy(
                update={"trades": (tampered, *trades[1:])}
            ),
        )


def test_a_consistently_shifted_split_is_refused_by_the_shared_predicate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one defect in this file that no other rule can see.

    ``market_pnl`` and ``post_fill_gross`` are moved *together*, so the split
    still reconstructs its own total -- which is all
    ``CostAttribution.each_split_reconstructs_its_own_total`` checks, and it
    runs on the sub-model, before any bundle validator is reached. The two
    charges are left alone, so both of the sums the bundle derives from the
    split are unchanged; and ``result.trades`` is untouched, so the commission
    and swap sums are unchanged too. Every rule in ``evidence.py`` apart from one
    is therefore satisfied by a document that reports one dollar more market move
    than the run made, and what refuses it is
    ``attribution_disagreement``'s comparison of the split's ``post_fill_gross``
    against the trade's own ``gross_pnl``.

    That is what makes it a test of its own rather than a third case in the test
    above, and it is what this task's one structural decision rests on. Remove the
    bundle's *call* and this document is accepted -- verified, by deleting the
    call and watching only the two predicate tests go red. The limit of the claim
    is worth stating too, because it is the same one every test in this file has:
    an inline that restated all three checks faithfully would still pass, since
    it would catch the same document. What no bundle-side test can catch is the
    *drift* the sharing exists to prevent -- two copies of three messages edited
    in different commits -- and that is a property of the code, not of a case.
    What this guards is the check itself: that the bundle has a per-trade
    comparison against the trade's own ``gross_pnl`` at all, rather than only the
    sums and the sub-model's decomposition, neither of which can see it.

    The assertions before the refusal are what keep the case from being
    vacuous: each names something the shift was chosen *not* to disturb.
    """

    bundle = _bundle_of(_run_command(monkeypatch, tmp_path, marked=True))
    assert bundle.cost_attribution is not None
    trades = bundle.cost_attribution.trades
    assert trades, "an attribution with no trades cannot carry a doctored split"
    split = trades[0]

    shifted = split.model_copy(
        update={"market_pnl": split.market_pnl + 1, "post_fill_gross": split.post_fill_gross + 1}
    )
    # The sub-model's own rule, satisfied -- the layer that catches the flat
    # ``999`` in the test above, and that this case walks straight past.
    assert (
        shifted.market_pnl - shifted.spread_cost - shifted.slippage_cost == shifted.post_fill_gross
    )
    # The two sums the bundle derives from the split, both unmoved.
    assert shifted.spread_cost == split.spread_cost
    assert shifted.slippage_cost == split.slippage_cost

    with pytest.raises(ValidationError, match="post-fill gross must equal its trade's gross PnL"):
        _revised(
            bundle,
            cost_attribution=bundle.cost_attribution.model_copy(update={"trades": (shifted,)}),
        )


def test_a_summary_that_disagrees_with_its_attribution_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The aggregate is checked, not trusted.

    ``CostSummary``'s own validator already refuses a ``COMPLETE`` with a
    ``None``; this is the other direction -- a ``COMPLETE`` whose four fields are
    all present and whose totals are numbers the trades behind them do not
    produce. A summary that can be written without reference to the split is a
    total this framework cannot check, which is the whole defect 8B2a closes.

    Every ``match`` is the whole clause its rule speaks in, and no two rules
    here share a clause. ``the summary's commission must equal the sum of the
    trades'`` is the only one any of these edits can produce, so dropping the
    check that speaks it turns this test red instead of leaving a refusal from
    somewhere else standing in for it.
    """

    bundle = _bundle_of(_run_command(monkeypatch, tmp_path, marked=True))

    with pytest.raises(ValidationError, match="the summary's spread cost"):
        _revised(bundle, costs=bundle.costs.model_copy(update={"spread_cost": Decimal("12345")}))

    with pytest.raises(ValidationError, match="the summary's slippage cost"):
        _revised(bundle, costs=bundle.costs.model_copy(update={"slippage_cost": Decimal("12345")}))

    with pytest.raises(ValidationError, match="the summary's commission"):
        _revised(
            bundle,
            costs=bundle.costs.model_copy(update={"commission": _bumped(bundle.costs.commission)}),
        )

    with pytest.raises(ValidationError, match="the summary's swap"):
        _revised(bundle, costs=bundle.costs.model_copy(update={"swap": _bumped(bundle.costs.swap)}))

    # The control: with every component untouched, this bundle validates. Without
    # it the four refusals above would not prove the validator is what fired.
    assert EvidenceBundle.model_validate_json(bundle.model_dump_json()) == bundle


def test_a_legacy_summary_that_disagrees_with_its_own_trades_is_refused(tmp_path: Path) -> None:
    """The two totals a ``PARTIAL`` summary can still be checked against.

    ``PARTIAL`` is a statement about spread and slippage -- the two terms a
    Phase 7 artifact cannot separate out of ``gross_pnl`` -- and it says nothing
    about commission or swap. Both are sums over ``result.trades``, which the
    legacy bundle carries whole, so a legacy document that claims a commission
    its own trades do not produce is refused exactly as a prospective one is.

    This is the case that made the check worth hoisting: the sum lives below an
    early return that every bundle without an attribution hits, and every legacy
    bundle is a bundle without one. Left there, the two numbers 8B1 had no split
    to check them against stayed asserted rather than derived for precisely the
    three artifacts that cannot be regenerated and re-verified -- which is the
    defect this slice exists to remove, not to leave standing in its own gap.
    """

    _store, legacy = _sealed_legacy_bundle(tmp_path)

    assert legacy.costs.status is CostAttributionStatus.PARTIAL
    assert legacy.cost_attribution is None
    with pytest.raises(ValidationError, match="the summary's commission"):
        _revised(
            legacy,
            costs=legacy.costs.model_copy(update={"commission": _bumped(legacy.costs.commission)}),
        )
    with pytest.raises(ValidationError, match="the summary's swap"):
        _revised(
            legacy,
            costs=legacy.costs.model_copy(update={"swap": _bumped(legacy.costs.swap)}),
        )

    # The control: the sealed document itself is refused by neither, so the two
    # refusals above are the totals and not a legacy bundle that never validated.
    assert EvidenceBundle.model_validate_json(legacy.model_dump_json()) == legacy


def test_the_sealed_attribution_is_the_engines_own_split(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The detail is sealed, not recomputed on the way out.

    ``CostSummary``'s four numbers could be re-derived from the trades by anyone
    who wanted to; the per-trade split could not. A bundle that carried a
    *recomputed* attribution would pass every equality below while answering a
    question nobody asked, so the assertion is against a second, independent run
    of the same window through the same factory the CLI calls -- and it is
    ``market_pnl`` that has to match, not the gross, because a gross that
    matched would only say the decomposition round-tripped.
    """

    bundle = _bundle_of(_run_command(monkeypatch, tmp_path, marked=True))
    engine = _outcome(_session_ramp())

    assert bundle.cost_attribution is not None
    assert bundle.cost_attribution == engine.attribution
    assert [split.proposal_id for split in bundle.cost_attribution.trades] == [
        trade.proposal_id for trade in bundle.result.trades
    ]
    assert all(
        split.market_pnl - split.spread_cost - split.slippage_cost == split.post_fill_gross
        for split in bundle.cost_attribution.trades
    )
