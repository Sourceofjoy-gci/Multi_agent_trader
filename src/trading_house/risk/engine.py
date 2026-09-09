"""The deterministic risk gate and its one point of broker contact.

``evaluate`` is pure over its arguments: the same proposal and the same market
facts always yield the same decision, so a backtester replays it exactly.
``evaluate_for_execution`` adds the margin-headroom check, which needs a live
account and therefore cannot appear in the pure path.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Protocol

from trading_house.constitution.models import BookLimits, Constitution, ScalpLimits
from trading_house.core.clock import Clock, ensure_utc
from trading_house.core.instruments import InstrumentContract
from trading_house.core.schemas import (
    RISK_DECISION_ADAPTER,
    RejectedRiskDecision,
    RiskDecision,
    Side,
    TradeProposal,
)
from trading_house.core.values import PositiveQuantity, Quantity
from trading_house.risk.sizing import (
    compute_stop_distance,
    compute_volume,
    quantise_down,
    stop_price,
)

MARGIN_HEADROOM_MULTIPLE = Decimal(2)


class RejectionReason(str, Enum):  # noqa: UP042
    """Every way this engine can refuse.

    The same values appear in a decision's ``checks_passed``, where they mean
    the failure was checked for and ruled out.
    """

    UNKNOWN_BOOK = "unknown_book"
    INSTRUMENT_MISMATCH = "instrument_mismatch"
    NON_POSITIVE_EQUITY = "non_positive_equity"
    SIDE_NOT_PERMITTED = "side_not_permitted"
    ASSET_CLASS_NOT_PERMITTED = "asset_class_not_permitted"
    SPREAD_EXCEEDS_CEILING = "spread_exceeds_ceiling"
    # R-8: the ceiling above compares the CURRENT spread to the MEDIAN, so a
    # zero median (a genuinely raw-spread account) disables it entirely. This
    # gate never references the median -- it compares the current spread to
    # the stop distance itself, which is never zero once a decision reaches
    # this point.
    SPREAD_EXCEEDS_STOP_FRACTION = "spread_exceeds_stop_fraction"
    # Covers an unusable tick timestamp in either direction: too old, or
    # stamped further ahead of the clock than max_clock_drift_ms allows.
    TICK_STALE = "tick_stale"
    BELOW_MIN_LOT = "below_min_lot"
    STOP_PRICE_NOT_POSITIVE = "stop_price_not_positive"
    INSUFFICIENT_FREE_MARGIN_HEADROOM = "insufficient_free_margin_headroom"


class MarginPort(Protocol):
    """The narrowest surface the headroom rule needs. Two methods, no more."""

    def free_margin(self) -> Decimal: ...

    def required_margin(
        self, *, instrument_id: str, side: Side, quantity: Decimal, price: Decimal
    ) -> Decimal: ...


def _record(
    ok: bool,
    reason: RejectionReason,
    reasons: list[RejectionReason],
    passed: list[RejectionReason],
) -> None:
    """Append the check's name to whichever list applies.

    This is the master spec's 7.2 ``_c(check, reasons, passed)`` helper: a name
    in ``reasons`` is a failure, and the same name in ``checks_passed`` means
    that failure was checked for and ruled out.
    """

    (passed if ok else reasons).append(reason)


class RiskEngine:
    """The deterministic gate. Holds the constitution and a clock, nothing else."""

    def __init__(self, constitution: Constitution, clock: Clock) -> None:
        self._constitution = constitution
        self._clock = clock

    def evaluate(
        self,
        proposal: TradeProposal,
        *,
        contract: InstrumentContract,
        firm_equity: Decimal,
        atr: Decimal,
        median_spread_points: Decimal,
        tick_spread_points: Decimal,
        tick_time: datetime,
    ) -> RiskDecision:
        # The prelude returns immediately: without a book, a matching contract
        # or an equity, no later check has anything to compute against.
        book = self._constitution.books.get(proposal.book)
        if book is None:
            return self._reject(proposal, [RejectionReason.UNKNOWN_BOOK], [])
        if contract.instrument_id != proposal.instrument_id:
            return self._reject(proposal, [RejectionReason.INSTRUMENT_MISMATCH], [])
        if firm_equity <= 0:
            return self._reject(proposal, [RejectionReason.NON_POSITIVE_EQUITY], [])

        # The independent gates all run; each contributes its own reason.
        reasons: list[RejectionReason] = []
        # The prelude checks above were also checked and ruled out to get
        # here, so they belong in the audit trail on every path from this
        # point on, whatever the eventual verdict.
        passed: list[RejectionReason] = [
            RejectionReason.UNKNOWN_BOOK,
            RejectionReason.INSTRUMENT_MISMATCH,
            RejectionReason.NON_POSITIVE_EQUITY,
        ]
        _record(
            self._side_permitted(proposal.side, contract),
            RejectionReason.SIDE_NOT_PERMITTED,
            reasons,
            passed,
        )
        _record(
            contract.asset_class in book.asset_classes,
            RejectionReason.ASSET_CLASS_NOT_PERMITTED,
            reasons,
            passed,
        )
        _record(
            self._spread_within_ceiling(book, median_spread_points, tick_spread_points),
            RejectionReason.SPREAD_EXCEEDS_CEILING,
            reasons,
            passed,
        )
        _record(
            self._tick_fresh(book, tick_time),
            RejectionReason.TICK_STALE,
            reasons,
            passed,
        )
        if reasons:
            return self._reject(proposal, reasons, passed)

        distance = compute_stop_distance(
            atr=atr,
            median_spread_points=median_spread_points,
            entry_price_ref=proposal.entry_price_ref,
            invalidation_price=proposal.invalidation_price,
            contract=contract,
            k_sigma=book.k_sigma,
            k_spread=book.k_spread,
        )

        # R-8: the current tick spread, priced and compared directly against
        # the stop distance -- never the median, which is the cost term's
        # input, not this gate's. Must run after distance is known.
        spread_price = tick_spread_points * contract.point_size
        if spread_price * Decimal(100) > distance * book.max_spread_fraction_of_stop:
            return self._reject(proposal, [RejectionReason.SPREAD_EXCEEDS_STOP_FRACTION], passed)
        passed.append(RejectionReason.SPREAD_EXCEEDS_STOP_FRACTION)

        # Checked here rather than inside stop_price() so an unrepresentable
        # stop is a rejection the caller can read, not an exception to catch.
        if (
            proposal.side is Side.BUY
            and proposal.entry_price_ref - distance < contract.price_increment
        ):
            return self._reject(proposal, [RejectionReason.STOP_PRICE_NOT_POSITIVE], passed)
        passed.append(RejectionReason.STOP_PRICE_NOT_POSITIVE)

        stop_loss_price = stop_price(
            side=proposal.side,
            entry_price_ref=proposal.entry_price_ref,
            stop_distance=distance,
            contract=contract,
        )
        # entry_price_ref is a strategy's reference price and carries no
        # tick-grid constraint, so stop_price's away-from-entry quantisation
        # can land the emitted stop up to one increment further out than
        # `distance`. Everything downstream must be derived from the stop that
        # is actually emitted: sizing from `distance` would let the realised
        # loss exceed the budget the volume was derived from, by half again on
        # a one-tick stop. Away-from-entry only ever widens, so this can only
        # shrink the position -- it never relaxes I-19.
        effective_distance = abs(proposal.entry_price_ref - stop_loss_price)

        # The book's own slice, not firm equity. Passing firm equity here would
        # over-risk the sleeve (capital_fraction 0.10) by ten times.
        book_equity = firm_equity * book.capital_fraction
        volume = compute_volume(
            stop_distance=effective_distance,
            book_equity=book_equity,
            risk_per_trade_pct=book.risk_per_trade_pct,
            contract=contract,
        )
        if volume < contract.quantity_min:
            return self._reject(proposal, [RejectionReason.BELOW_MIN_LOT], passed)
        passed.append(RejectionReason.BELOW_MIN_LOT)

        resized = volume > contract.quantity_max
        if resized:
            # quantity_max is not validated as a grid multiple (unlike
            # quantity_min), so clamping straight to it can land off-grid and
            # MT5 rejects the order outright. Floor to the grid instead: since
            # quantity_min is on the grid and quantity_max >= quantity_min,
            # the floored value can never drop below the minimum.
            volume = quantise_down(contract.quantity_max, contract.quantity_increment)

        # Not necessarily a whole number of ticks: an off-grid entry leaves a
        # fractional remainder, and the loss is proportional to the true price
        # distance, not to a tidied one.
        ticks = effective_distance / contract.price_increment
        risk_money = ticks * contract.value_per_price_increment * volume
        return RISK_DECISION_ADAPTER.validate_python(
            {
                "verdict": "RESIZED" if resized else "APPROVED",
                "proposal_id": proposal.proposal_id,
                "reasons": (),
                "checks_passed": tuple(check.value for check in passed),
                "constitution_version": self._constitution.version,
                "approved_quantity": PositiveQuantity(amount=volume, unit="lots"),
                "stop_loss_price": stop_loss_price,
                "take_profit_price": None,
                "risk_money": risk_money,
                "risk_pct_of_book": risk_money / book_equity * Decimal(100),
            }
        )

    @staticmethod
    def _side_permitted(side: Side, contract: InstrumentContract) -> bool:
        return contract.can_open_long if side is Side.BUY else contract.can_open_short

    def _spread_within_ceiling(
        self, book: BookLimits, median_points: Decimal, tick_points: Decimal
    ) -> bool:
        """The tightest applicable ceiling binds.

        ``max_spread_multiple_of_median`` is defined for every horizon;
        ``ScalpLimits.max_spread_multiple_at_entry`` exists only for scalp and
        is entry-specific. Honouring one and ignoring the other would leave a
        signed ceiling with no enforcement anywhere.
        """

        if median_points <= 0:
            # A raw-spread account can genuinely report a zero median, and a
            # multiple of zero would reject every trade on it.
            return True
        triggers = self._constitution.safe_mode_triggers[book.horizon]
        ceiling = triggers.max_spread_multiple_of_median
        if isinstance(book.limits, ScalpLimits):
            ceiling = min(ceiling, book.limits.max_spread_multiple_at_entry)
        return tick_points <= ceiling * median_points

    def _tick_fresh(self, book: BookLimits, tick_time: datetime) -> bool:
        """Bounded on both sides, so ``TICK_STALE`` means an unusable timestamp
        in either direction.

        An age bounded only from above accepts a tick stamped an hour into the
        future, and a tick from a clock that far out of step is no more usable
        than one an hour old. ``max_clock_drift_ms`` is the constitution's own
        tolerance for that skew.
        """

        triggers = self._constitution.safe_mode_triggers[book.horizon]
        # float() builds a duration threshold here, not a money value.
        max_age = timedelta(seconds=float(triggers.max_tick_age_seconds))
        max_drift = timedelta(milliseconds=triggers.max_clock_drift_ms)
        age = self._clock.now() - ensure_utc(tick_time)
        return -max_drift <= age <= max_age

    def _reject(
        self,
        proposal: TradeProposal,
        reasons: Sequence[RejectionReason],
        passed: Sequence[RejectionReason],
    ) -> RejectedRiskDecision:
        return RejectedRiskDecision(
            verdict="REJECTED",
            proposal_id=proposal.proposal_id,
            reasons=tuple(reason.value for reason in reasons),
            checks_passed=tuple(check.value for check in passed),
            constitution_version=self._constitution.version,
            approved_quantity=Quantity(amount=Decimal(0), unit="lots"),
            risk_money=Decimal(0),
            risk_pct_of_book=Decimal(0),
        )

    def evaluate_for_execution(
        self,
        proposal: TradeProposal,
        *,
        margin: MarginPort,
        contract: InstrumentContract,
        firm_equity: Decimal,
        atr: Decimal,
        median_spread_points: Decimal,
        tick_spread_points: Decimal,
        tick_time: datetime,
    ) -> RiskDecision:
        """``evaluate`` plus the master spec's 8.1 free-margin headroom rule.

        Two entry points rather than one so that forgetting the margin check is
        not a forgotten line but a differently named function -- and so the pure
        path stays callable from a backtester that has no terminal.
        """

        decision = self.evaluate(
            proposal,
            contract=contract,
            firm_equity=firm_equity,
            atr=atr,
            median_spread_points=median_spread_points,
            tick_spread_points=tick_spread_points,
            tick_time=tick_time,
        )
        if isinstance(decision, RejectedRiskDecision):
            return decision
        required = margin.required_margin(
            instrument_id=proposal.instrument_id,
            side=proposal.side,
            quantity=decision.approved_quantity.amount,
            price=proposal.entry_price_ref,
        )
        if required <= 0 or margin.free_margin() < required * MARGIN_HEADROOM_MULTIPLE:
            # decision.checks_passed is tuple[str, ...] on the schema; convert
            # back to RejectionReason so the gates evaluate() already cleared
            # are not dropped from the audit trail.
            checks_passed = [RejectionReason(value) for value in decision.checks_passed]
            return self._reject(
                proposal, [RejectionReason.INSUFFICIENT_FREE_MARGIN_HEADROOM], checks_passed
            )
        return decision
