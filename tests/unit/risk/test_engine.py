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
