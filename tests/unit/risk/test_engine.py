"""The gates, the sizing decision, and the margin seam.

Every hand-worked expected value here is worked longhand in a comment so a
reader can verify it without running the code.
"""

from datetime import timedelta
from decimal import Decimal

from tests.unit.risk.conftest import NOW, _contract, _facts, _proposal
from trading_house.constitution.models import Constitution
from trading_house.core.clock import FixedClock
from trading_house.core.schemas import RejectedRiskDecision
from trading_house.core.values import AssetClass
from trading_house.risk.engine import RejectionReason, RiskEngine


def _engine(constitution: Constitution) -> RiskEngine:
    return RiskEngine(constitution, FixedClock(NOW))


def _assert_risk_money_is_the_loss_at_the_emitted_stop(
    decision, proposal, contract, constitution
) -> None:
    """D-8 and I-19, checked against the stop the decision actually carries.

    Every other assertion in this file compares ``risk_money`` to a figure
    worked from the pre-quantisation distance, which is exactly the number a
    broken engine reports. This one compares it to the loss at
    ``decision.stop_loss_price``, which is the price the broker will fill.
    """

    ticks = abs(proposal.entry_price_ref - decision.stop_loss_price) / contract.price_increment
    realised = ticks * contract.value_per_price_increment * decision.approved_quantity.amount
    assert decision.risk_money == realised

    book = constitution.books[proposal.book]
    book_equity = _facts()["firm_equity"] * book.capital_fraction
    assert decision.risk_money <= book_equity * book.risk_per_trade_pct / Decimal(100)


def test_an_unknown_book_is_rejected_before_anything_is_computed(constitution) -> None:
    decision = _engine(constitution).evaluate(
        _proposal(book="does_not_exist"), contract=_contract(), **_facts()
    )

    assert isinstance(decision, RejectedRiskDecision)
    assert RejectionReason.UNKNOWN_BOOK in decision.reasons


def test_a_contract_for_a_different_instrument_is_rejected(constitution) -> None:
    """Sizing against the wrong contract would use the wrong tick value and
    silently produce a position sized for a different instrument."""

    decision = _engine(constitution).evaluate(
        _proposal(), contract=_contract(instrument_id="fx.gbpusd"), **_facts()
    )

    assert RejectionReason.INSTRUMENT_MISMATCH in decision.reasons


def test_a_long_proposal_on_a_short_only_symbol_is_rejected(constitution) -> None:
    decision = _engine(constitution).evaluate(
        _proposal(), contract=_contract(can_open_long=False), **_facts()
    )

    assert RejectionReason.SIDE_NOT_PERMITTED in decision.reasons


def test_an_asset_class_outside_the_books_mandate_is_rejected(constitution) -> None:
    """fx_scalp declares asset_classes [fx, metal]. An equity CFD is not its
    business even when every other check passes."""

    decision = _engine(constitution).evaluate(
        _proposal(instrument_id="equity_cfd.aapl"),
        contract=_contract(instrument_id="equity_cfd.aapl", asset_class=AssetClass.EQUITY_CFD),
        **_facts(),
    )

    assert RejectionReason.ASSET_CLASS_NOT_PERMITTED in decision.reasons


def test_a_spread_above_the_ceiling_is_rejected(constitution) -> None:
    """fx_scalp is a scalp book: safe_mode_triggers.scalp allows 3.0x the
    median and ScalpLimits.max_spread_multiple_at_entry allows 1.5x. The
    tighter of the two binds, so 20 points against a 10-point median (2.0x)
    must be rejected even though it is inside the 3.0x safe-mode trigger."""

    decision = _engine(constitution).evaluate(
        _proposal(), contract=_contract(), **_facts(tick_spread_points=Decimal("20"))
    )

    assert RejectionReason.SPREAD_EXCEEDS_CEILING in decision.reasons


def test_a_swing_book_uses_only_the_safe_mode_multiple(constitution) -> None:
    """SwingLimits has no entry multiple, so 2.0x passes on fx_swing under the
    3.0x safe-mode trigger alone.

    This does NOT prove the scalp ``min()`` branch is live: with a swing
    book, ``max_spread_multiple_of_median`` is 3.0, so 2.0x passes whether or
    not that branch exists. The branch is proven by
    ``test_a_spread_above_the_ceiling_is_rejected``, where the scalp ceiling
    (1.5x) drops below the safe-mode multiple (3.0x) and rejects a spread the
    safe-mode multiple alone would have passed.
    """

    decision = _engine(constitution).evaluate(
        _proposal(book="fx_swing"),
        contract=_contract(),
        **_facts(tick_spread_points=Decimal("20")),
    )

    assert decision.verdict == "APPROVED"
    assert RejectionReason.SPREAD_EXCEEDS_CEILING not in decision.reasons


def test_a_zero_median_spread_disables_the_ratio_gate(constitution) -> None:
    """A raw-spread account can genuinely report a zero median. Comparing
    against 3.0 x 0 would reject every trade on that account."""

    decision = _engine(constitution).evaluate(
        _proposal(),
        contract=_contract(),
        **_facts(median_spread_points=Decimal("0"), tick_spread_points=Decimal("2")),
    )

    assert decision.verdict == "APPROVED"
    assert RejectionReason.SPREAD_EXCEEDS_CEILING not in decision.reasons


def test_a_zero_median_no_longer_admits_an_enormous_spread(constitution) -> None:
    """R-8, carried from Phase 3 on condition it closed before any order could
    be placed. Before this gate, median 0 with a 100000-point tick spread was
    APPROVED at full size, because the ratio gate compares against the median
    and a multiple of zero admits everything."""

    decision = _engine(constitution).evaluate(
        _proposal(),
        contract=_contract(),
        **_facts(median_spread_points=Decimal("0"), tick_spread_points=Decimal("100000")),
    )

    assert RejectionReason.SPREAD_EXCEEDS_STOP_FRACTION in decision.reasons


def test_an_ordinary_spread_passes_the_fraction_gate(constitution) -> None:
    """Guard the guard: a gate that rejected everything would also pass the
    test above."""

    decision = _engine(constitution).evaluate(_proposal(), contract=_contract(), **_facts())

    assert decision.verdict == "APPROVED"


def test_a_stale_tick_is_rejected(constitution) -> None:
    """safe_mode_triggers.scalp allows 2 seconds."""

    decision = _engine(constitution).evaluate(
        _proposal(),
        contract=_contract(),
        **_facts(tick_time=NOW - timedelta(seconds=3)),
    )

    assert RejectionReason.TICK_STALE in decision.reasons


def test_a_tick_inside_the_age_limit_passes(constitution) -> None:
    decision = _engine(constitution).evaluate(
        _proposal(),
        contract=_contract(),
        **_facts(tick_time=NOW - timedelta(seconds=1)),
    )

    assert decision.verdict == "APPROVED"
    assert RejectionReason.TICK_STALE not in decision.reasons


def test_a_tick_stamped_beyond_the_clock_drift_allowance_is_rejected(constitution) -> None:
    """An age bounded only from above accepts a tick stamped an hour ahead, and
    an hour-ahead tick is no more usable than an hour-old one.
    ``safe_mode_triggers.scalp.max_clock_drift_ms`` is 500, so 400ms of forward
    jitter is tolerated and one second ahead is not."""

    inside_drift = _engine(constitution).evaluate(
        _proposal(),
        contract=_contract(),
        **_facts(tick_time=NOW + timedelta(milliseconds=400)),
    )
    ahead_of_the_clock = _engine(constitution).evaluate(
        _proposal(),
        contract=_contract(),
        **_facts(tick_time=NOW + timedelta(seconds=1)),
    )

    assert inside_drift.verdict == "APPROVED"
    assert RejectionReason.TICK_STALE in ahead_of_the_clock.reasons


def test_every_failing_gate_contributes_its_own_reason(constitution) -> None:
    """One rejection listing three faults is one diagnosis; three sequential
    rejections are three round trips."""

    decision = _engine(constitution).evaluate(
        _proposal(),
        contract=_contract(can_open_long=False),
        **_facts(tick_spread_points=Decimal("99"), tick_time=NOW - timedelta(seconds=30)),
    )

    assert RejectionReason.SIDE_NOT_PERMITTED in decision.reasons
    assert RejectionReason.SPREAD_EXCEEDS_CEILING in decision.reasons
    assert RejectionReason.TICK_STALE in decision.reasons


def test_a_proposal_below_the_books_edge_floor_is_refused(constitution) -> None:
    """fx_swing's floor is 2.0 bps after cost. A proposal declaring 5.0 of
    return against 4.0 of cost clears 1.0 bps, which is under it."""

    decision = _engine(constitution).evaluate(
        _proposal(book="fx_swing", expected_return_bps=5.0, expected_cost_bps=4.0),
        contract=_contract(),
        **_facts(),
    )

    assert isinstance(decision, RejectedRiskDecision)
    assert RejectionReason.EDGE_BELOW_FLOOR in decision.reasons


def test_a_proposal_exactly_at_the_edge_floor_is_permitted(constitution) -> None:
    """The boundary belongs to the permitted side, and asserting it is what
    stops the comparison drifting between < and <=."""

    decision = _engine(constitution).evaluate(
        _proposal(book="fx_swing", expected_return_bps=6.0, expected_cost_bps=4.0),
        contract=_contract(),
        **_facts(),
    )

    assert RejectionReason.EDGE_BELOW_FLOOR in decision.checks_passed


def test_a_scalp_proposal_holding_longer_than_its_book_permits_is_refused(constitution) -> None:
    """fx_scalp caps a position at 300 seconds."""

    decision = _engine(constitution).evaluate(
        _proposal(book="fx_scalp", max_holding_seconds=600),
        contract=_contract(),
        **_facts(),
    )

    assert isinstance(decision, RejectedRiskDecision)
    assert RejectionReason.HOLDING_EXCEEDS_BOOK_LIMIT in decision.reasons


def test_a_scalp_proposal_holding_exactly_at_its_book_limit_is_permitted(constitution) -> None:
    """fx_scalp caps a position at 300 seconds. max_holding_seconds is set to
    that cap explicitly here, rather than relying on it matching whatever
    ``_proposal()``'s own default happens to be -- the boundary must stay
    protected even if that fixture default ever changes."""

    decision = _engine(constitution).evaluate(
        _proposal(book="fx_scalp", max_holding_seconds=300),
        contract=_contract(),
        **_facts(),
    )

    assert RejectionReason.HOLDING_EXCEEDS_BOOK_LIMIT in decision.checks_passed


def test_a_swing_proposal_whose_swap_eats_its_edge_is_refused(constitution) -> None:
    """fx_swing permits swap up to 20% of expected edge. 2.0 bps of swap
    against 6.0 of return and 4.0 of cost is 2.0 over an edge of 2.0 -- 100%."""

    decision = _engine(constitution).evaluate(
        _proposal(
            book="fx_swing",
            expected_return_bps=6.0,
            expected_cost_bps=4.0,
            expected_swap_cost_bps=2.0,
        ),
        contract=_contract(),
        **_facts(),
    )

    assert isinstance(decision, RejectedRiskDecision)
    assert RejectionReason.SWAP_EXCEEDS_EDGE_FRACTION in decision.reasons


def test_a_swing_proposal_whose_swap_is_exactly_at_the_edge_fraction_cap_is_permitted(
    constitution,
) -> None:
    """fx_swing permits swap up to 20% of expected edge. 0.4 bps of swap
    against an edge of 2.0 (6.0 of return, 4.0 of cost) is exactly 20% --
    the boundary belongs to the permitted side, same as the edge floor.

    Mutation found this boundary was untested: flipping <= to < in
    _swap_within_edge_fraction left every existing test passing, because
    none of them landed exactly on the cap.
    """

    decision = _engine(constitution).evaluate(
        _proposal(
            book="fx_swing",
            expected_return_bps=6.0,
            expected_cost_bps=4.0,
            expected_swap_cost_bps=0.4,
        ),
        contract=_contract(),
        **_facts(),
    )

    assert RejectionReason.SWAP_EXCEEDS_EDGE_FRACTION in decision.checks_passed


def test_the_duration_cap_does_not_apply_to_a_swing_book(constitution) -> None:
    """max_position_duration_seconds is declared on ScalpLimits only. A swing
    proposal holding nine hours is not refused by a limit its book does not
    have -- and a hasattr-style check would wrongly skip the scalp case too."""

    decision = _engine(constitution).evaluate(
        _proposal(book="fx_swing", max_holding_seconds=32400),
        contract=_contract(),
        **_facts(),
    )

    assert RejectionReason.HOLDING_EXCEEDS_BOOK_LIMIT not in decision.reasons


def test_an_approved_decision_carries_the_realised_risk_not_the_budget(constitution) -> None:
    """fx_scalp: capital_fraction 0.30, risk_per_trade_pct 0.25.

        book_equity   = 100_000 x 0.30              = 30_000
        budget        = 30_000 x 0.25 / 100         = 75.00
        distance      = max(k_sigma 1.2 x 0.00050   = 0.00060,
                            cost 2.0 x 10 x 0.00001  = 0.00020,
                            structural 0.00300,
                            floor 0.00001)           = 0.00300
        ticks         = 300, money_per_lot          = 300.00
        raw volume    = 75.00 / 300.00              = 0.25
        risk_money    = 300 x 1.00 x 0.25           = 75.00

    risk_money must be 75.00 -- what will actually be lost -- and not the
    budget by construction.
    """

    decision = _engine(constitution).evaluate(_proposal(), contract=_contract(), **_facts())

    assert decision.verdict == "APPROVED"
    assert decision.approved_quantity.amount == Decimal("0.25")
    assert decision.stop_loss_price == Decimal("1.09700")
    assert decision.risk_money == Decimal("75.00")


def test_an_off_grid_entry_sizes_from_the_one_tick_stop_it_emits(constitution) -> None:
    """``entry_price_ref`` carries no tick-grid constraint, and ``stop_price``
    quantises AWAY from entry, so an off-grid reference pushes the emitted stop
    further out than the distance the risk model asked for.

        entry         = 1.100005 -- half a tick off the 0.00001 grid
        distance      = max(vol 0, cost 0, structural 0.000005,
                            floor 0.00001) rounded up   = 0.00001
        stop          = floor(1.100005 - 0.00001)       = 1.09999
        effective     = 1.100005 - 1.09999              = 0.000015 (1.5 ticks)
        budget        = 100_000 x 0.30 x 0.25 / 100     = 75.00
        volume        = floor((75.00 / 1.50) / 0.01)x0.01 = 50.00
        risk_money    = 1.5 x 1.00 x 50.00              = 75.00

    Sizing from the pre-quantisation 0.00001 instead approves 75 lots, whose
    loss at the emitted 1.09999 is 112.50 -- 50% over the signed budget, while
    ``risk_money`` still reports 75.00.
    """

    contract = _contract()
    proposal = _proposal(
        entry_price_ref=Decimal("1.100005"), invalidation_price=Decimal("1.100000")
    )

    decision = _engine(constitution).evaluate(
        proposal,
        contract=contract,
        # tick_spread_points is zeroed too: R-8's gate compares the current
        # spread to the emitted stop, and the default 10-point spread would
        # dwarf this test's 1-tick stop and reject it for a reason unrelated
        # to what is under test here.
        **_facts(
            atr=Decimal("0"),
            median_spread_points=Decimal("0"),
            tick_spread_points=Decimal("0"),
        ),
    )

    assert decision.verdict == "APPROVED"
    assert decision.stop_loss_price == Decimal("1.09999")
    _assert_risk_money_is_the_loss_at_the_emitted_stop(decision, proposal, contract, constitution)
    assert decision.approved_quantity.amount == Decimal("50.00")


def test_an_off_grid_entry_sizes_from_the_wide_stop_it_emits(constitution) -> None:
    """The same defect at a realistic stop width, where it is a 0.17% overshoot
    rather than 50% -- small enough to hide, and still a breach of I-19.

        entry         = 1.100005, invalidation 1.097005
        distance      = structural 0.00300 (already on the grid)
        stop          = floor(1.100005 - 0.00300)       = 1.09700
        effective     = 1.100005 - 1.09700              = 0.003005 (300.5 ticks)
        volume        = floor((75.00 / 300.50) / 0.01)x0.01 = 0.24
        risk_money    = 300.5 x 1.00 x 0.24             = 72.12

    Sizing from 0.00300 gives 0.25 lots and reports 75.00, but the loss at the
    emitted 1.09700 is 75.125.
    """

    contract = _contract()
    proposal = _proposal(
        entry_price_ref=Decimal("1.100005"), invalidation_price=Decimal("1.097005")
    )

    decision = _engine(constitution).evaluate(proposal, contract=contract, **_facts())

    assert decision.verdict == "APPROVED"
    assert decision.stop_loss_price == Decimal("1.09700")
    _assert_risk_money_is_the_loss_at_the_emitted_stop(decision, proposal, contract, constitution)
    assert decision.approved_quantity.amount == Decimal("0.24")


def test_book_equity_is_the_books_slice_not_firm_equity(constitution) -> None:
    """sleeve holds capital_fraction 0.10. Passing firm equity straight in
    would size this position ten times too large."""

    decision = _engine(constitution).evaluate(
        _proposal(book="sleeve"), contract=_contract(), **_facts()
    )

    # book_equity = 100_000 x 0.10 = 10_000; budget = 10_000 x 1.5 / 100 = 150.00
    # ticks 300, money_per_lot 300.00, volume floor(0.5 / 0.01) x 0.01 = 0.50
    assert decision.approved_quantity.amount == Decimal("0.50")


def test_a_position_below_the_minimum_lot_is_rejected_not_rounded_up(constitution) -> None:
    """Rounding a sub-minimum position up to quantity_min would take more risk
    than the constitution allows, on the trades where the budget was smallest."""

    decision = _engine(constitution).evaluate(
        _proposal(),
        contract=_contract(quantity_min=Decimal("5"), quantity_increment=Decimal("5")),
        **_facts(),
    )

    assert RejectionReason.BELOW_MIN_LOT in decision.reasons
    assert decision.approved_quantity.amount == Decimal("0")
    assert decision.risk_money == Decimal("0")


def test_the_minimum_lot_boundary_admits_as_well_as_refuses(constitution) -> None:
    """The raw volume for these facts is exactly 0.25, so a minimum of 0.25
    must be approved and 0.26 must not. Without the approving half, an engine
    that rejected every proposal would pass the test above."""

    at_the_boundary = _engine(constitution).evaluate(
        _proposal(), contract=_contract(quantity_min=Decimal("0.25")), **_facts()
    )
    one_step_beyond = _engine(constitution).evaluate(
        _proposal(), contract=_contract(quantity_min=Decimal("0.26")), **_facts()
    )

    assert at_the_boundary.verdict == "APPROVED"
    assert at_the_boundary.approved_quantity.amount == Decimal("0.25")
    assert RejectionReason.BELOW_MIN_LOT in one_step_beyond.reasons


def test_a_position_above_the_maximum_lot_is_clamped_and_marked_resized(
    constitution,
) -> None:
    decision = _engine(constitution).evaluate(
        _proposal(), contract=_contract(quantity_max=Decimal("0.10")), **_facts()
    )

    assert decision.verdict == "RESIZED"
    assert decision.approved_quantity.amount == Decimal("0.10")
    # risk_money follows the clamped size: 300 x 1.00 x 0.10 = 30.00
    assert decision.risk_money == Decimal("30.00")


def test_a_resized_volume_lands_on_the_lot_grid(constitution) -> None:
    """quantity_max is never validated as a grid multiple (unlike
    quantity_min), so a broker-supplied max of 0.15 against a 0.1 increment
    is off-grid. Clamping straight to it would emit a volume MT5 rejects
    outright; flooring to the grid must yield 0.1 instead."""

    contract = _contract(
        quantity_increment=Decimal("0.1"),
        quantity_min=Decimal("0.1"),
        quantity_max=Decimal("0.15"),
    )

    decision = _engine(constitution).evaluate(_proposal(), contract=contract, **_facts())

    assert decision.verdict == "RESIZED"
    assert decision.approved_quantity.amount == Decimal("0.1")
    assert decision.approved_quantity.amount % contract.quantity_increment == 0


def test_a_stop_that_cannot_land_above_zero_is_rejected(constitution) -> None:
    decision = _engine(constitution).evaluate(
        _proposal(entry_price_ref=Decimal("0.00200"), invalidation_price=Decimal("0.00100")),
        contract=_contract(),
        **_facts(atr=Decimal("1")),
    )

    assert RejectionReason.STOP_PRICE_NOT_POSITIVE in decision.reasons


class _Margin:
    def __init__(self, free: str, required: str) -> None:
        self._free = Decimal(free)
        self._required = Decimal(required)

    def free_margin(self) -> Decimal:
        return self._free

    def required_margin(self, *, instrument_id, side, quantity, price) -> Decimal:
        return self._required


def test_execution_path_rejects_when_headroom_is_below_two_times(constitution) -> None:
    """The master spec's 8.1 requires 2x headroom so that one adverse move
    cannot cascade into forced liquidation across the book."""

    decision = _engine(constitution).evaluate_for_execution(
        _proposal(),
        margin=_Margin(free="1900", required="1000"),
        contract=_contract(),
        **_facts(),
    )

    assert RejectionReason.INSUFFICIENT_FREE_MARGIN_HEADROOM in decision.reasons


def test_a_margin_rejection_keeps_the_gates_that_had_already_passed(constitution) -> None:
    """The margin rule runs on an already-approved decision, so its rejection
    must carry that decision's ``checks_passed`` forward. Dropping them would
    make the audit record claim no gate was ever evaluated."""

    decision = _engine(constitution).evaluate_for_execution(
        _proposal(),
        margin=_Margin(free="1900", required="1000"),
        contract=_contract(),
        **_facts(),
    )

    assert RejectionReason.INSUFFICIENT_FREE_MARGIN_HEADROOM in decision.reasons
    assert decision.checks_passed
    for cleared in (
        RejectionReason.UNKNOWN_BOOK,
        RejectionReason.SPREAD_EXCEEDS_CEILING,
        RejectionReason.TICK_STALE,
        RejectionReason.STOP_PRICE_NOT_POSITIVE,
        RejectionReason.BELOW_MIN_LOT,
    ):
        assert cleared in decision.checks_passed


def test_execution_path_approves_at_exactly_two_times(constitution) -> None:
    decision = _engine(constitution).evaluate_for_execution(
        _proposal(),
        margin=_Margin(free="2000", required="1000"),
        contract=_contract(),
        **_facts(),
    )

    assert decision.verdict == "APPROVED"


def test_execution_path_does_not_consult_margin_for_an_already_rejected_proposal(
    constitution,
) -> None:
    """A rejected proposal has no quantity to price, so calling the port would
    be asking the broker about a position that will never exist."""

    class _Exploding:
        def free_margin(self) -> Decimal:
            raise AssertionError("margin must not be consulted")

        def required_margin(self, **_: object) -> Decimal:
            raise AssertionError("margin must not be consulted")

    decision = _engine(constitution).evaluate_for_execution(
        _proposal(book="does_not_exist"),
        margin=_Exploding(),
        contract=_contract(),
        **_facts(),
    )

    assert RejectionReason.UNKNOWN_BOOK in decision.reasons
