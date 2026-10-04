"""The deterministic risk gate and its one point of broker contact.

``evaluate`` is pure over its arguments: the same proposal and the same market
facts always yield the same decision, so a backtester replays it exactly.
``evaluate_for_execution`` adds the margin-headroom check, which needs a live
account and therefore cannot appear in the pure path.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Protocol

from trading_house.constitution.models import BookLimits, Constitution, ScalpLimits, SwingLimits
from trading_house.core.clock import Clock, ensure_utc
from trading_house.core.instruments import InstrumentContract
from trading_house.core.schemas import (
    RISK_DECISION_ADAPTER,
    RejectedRiskDecision,
    RiskDecision,
    Side,
    TradeProposal,
)
from trading_house.core.values import BookId, PositiveQuantity, Quantity
from trading_house.risk.portfolio import (
    PortfolioState,
    cluster_keys,
    loss_to_stop,
    notional_money,
)
from trading_house.risk.sizing import (
    compute_stop_distance,
    compute_volume,
    quantise_down,
    quantise_up,
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
    TARGET_PRICE_NOT_POSITIVE = "target_price_not_positive"
    INSUFFICIENT_FREE_MARGIN_HEADROOM = "insufficient_free_margin_headroom"
    EDGE_BELOW_FLOOR = "edge_below_floor"
    HOLDING_EXCEEDS_BOOK_LIMIT = "holding_exceeds_book_limit"
    SWAP_EXCEEDS_EDGE_FRACTION = "swap_exceeds_edge_fraction"
    # Phase 10: the portfolio the new order joins. The first six need only the
    # state; the rest need the candidate's risk and notional, so they run after
    # sizing on the volume actually approved.
    UNMEASURED_OPEN_RISK = "unmeasured_open_risk"
    PNL_UNAVAILABLE = "pnl_unavailable"
    CONSECUTIVE_REJECTS_EXCEEDED = "consecutive_rejects_exceeded"
    ORDER_RATE_EXCEEDED = "order_rate_exceeded"
    MAX_CONCURRENT_POSITIONS = "max_concurrent_positions"
    DAILY_LOSS_STOP = "daily_loss_stop"
    BOOK_DRAWDOWN_HALT = "book_drawdown_halt"
    FIRM_DRAWDOWN_HALT = "firm_drawdown_halt"
    AGGREGATE_OPEN_RISK = "aggregate_open_risk"
    SINGLE_INSTRUMENT_RISK = "single_instrument_risk"
    CORRELATED_CLUSTER_RISK = "correlated_cluster_risk"
    BOOK_GROSS_LEVERAGE = "book_gross_leverage"
    FIRM_GROSS_LEVERAGE = "firm_gross_leverage"


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
        portfolio: PortfolioState,
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
        _record(
            self._edge_clears_floor(book, proposal),
            RejectionReason.EDGE_BELOW_FLOOR,
            reasons,
            passed,
        )
        _record(
            self._holding_within_book_limit(book, proposal),
            RejectionReason.HOLDING_EXCEEDS_BOOK_LIMIT,
            reasons,
            passed,
        )
        _record(
            self._swap_within_edge_fraction(book, proposal),
            RejectionReason.SWAP_EXCEEDS_EDGE_FRACTION,
            reasons,
            passed,
        )
        for ok, reason in self._state_checks(proposal.book, book, portfolio):
            _record(ok, reason, reasons, passed)
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

        # The fixed target, priced off the stop distance actually emitted
        # above -- never off proposal.invalidation_price, which is the
        # strategy's own idea of where it is wrong, not the stop the
        # constitution's multipliers and the contract's grid actually
        # produced. None stays reachable when the strategy declares no
        # multiple, which the baseline arm of a later A/B needs.
        target_price: Decimal | None = None
        if proposal.target_r_multiple is not None:
            target_distance = effective_distance * proposal.target_r_multiple
            raw_target_price = (
                proposal.entry_price_ref + target_distance
                if proposal.side is Side.BUY
                else proposal.entry_price_ref - target_distance
            )
            # Checked here rather than inside quantise_up/quantise_down, so an
            # unrepresentable target is a rejection the caller can read, not
            # an exception escaping evaluate() -- the same shape as
            # STOP_PRICE_NOT_POSITIVE above and for the same reason: this
            # engine is pure over its arguments, and a crash is not a
            # decision. target_r_multiple is gt=0, so only an oversized
            # multiple on a SELL can drive this negative; a BUY target only
            # ever moves further from zero.
            if raw_target_price < contract.price_increment:
                return self._reject(proposal, [RejectionReason.TARGET_PRICE_NOT_POSITIVE], passed)
            passed.append(RejectionReason.TARGET_PRICE_NOT_POSITIVE)
            # Rounded away from entry, the mirror image of stop_price's own
            # rule: up for a BUY (harder to reach), down for a SELL -- the
            # same two helpers stop_loss_price is rounded with, never a
            # rounding that would make the target easier to hit than the R
            # multiple declared.
            target_price = (
                quantise_up(raw_target_price, contract.price_increment)
                if proposal.side is Side.BUY
                else quantise_down(raw_target_price, contract.price_increment)
            )

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

        # Phase 10: gross leverage is a ceiling on size, like quantity_max, so
        # it resizes rather than rejects. A tight stop sizes a large position
        # from the risk budget alone, and on a single trade that can already
        # exceed the book's signed leverage with nothing else open. Floored to
        # the grid, so the result never exceeds either headroom; if what is
        # left is below the minimum lot, the binding limit names the refusal.
        leverage_cap = self._leverage_cap(
            proposal.book, book, portfolio, contract, firm_equity, proposal.entry_price_ref
        )
        if volume > leverage_cap.volume:
            if leverage_cap.volume < contract.quantity_min:
                return self._reject(proposal, list(leverage_cap.binding), passed)
            volume = leverage_cap.volume
            resized = True

        # Not necessarily a whole number of ticks: an off-grid entry leaves a
        # fractional remainder, and the loss is proportional to the true price
        # distance, not to a tidied one.
        ticks = effective_distance / contract.price_increment
        risk_money = ticks * contract.value_per_price_increment * volume

        for ok, reason in self._exposure_checks(
            proposal.book,
            book,
            portfolio,
            contract=contract,
            firm_equity=firm_equity,
            risk_money=risk_money,
            notional=notional_money(volume, proposal.entry_price_ref, contract),
        ):
            _record(ok, reason, reasons, passed)
        if reasons:
            return self._reject(proposal, reasons, passed)
        return RISK_DECISION_ADAPTER.validate_python(
            {
                "verdict": "RESIZED" if resized else "APPROVED",
                "proposal_id": proposal.proposal_id,
                "reasons": (),
                "checks_passed": tuple(check.value for check in passed),
                "constitution_version": self._constitution.version,
                "approved_quantity": PositiveQuantity(amount=volume, unit="lots"),
                "stop_loss_price": stop_loss_price,
                "take_profit_price": target_price,
                "risk_money": risk_money,
                "risk_pct_of_book": risk_money / book_equity * Decimal(100),
            }
        )

    @staticmethod
    def _side_permitted(side: Side, contract: InstrumentContract) -> bool:
        return contract.can_open_long if side is Side.BUY else contract.can_open_short

    @staticmethod
    def _expected_edge_bps(proposal: TradeProposal) -> Decimal:
        """Declared return minus declared cost, in bps.

        The bps fields on TradeProposal are FiniteFloat (analytics); the
        constitution's limits are Decimal (money-adjacent). Converting
        through str rather than comparing float to Decimal directly avoids
        binary floating-point artefacts leaking into a money-adjacent
        comparison. A single helper, rather than this expression repeated at
        each call site, is what makes the two callers agree by construction
        instead of by both remembering the same rule -- the shape of a
        Critical this phase already shipped once, in the session/snapshot
        split.
        """

        return Decimal(str(proposal.expected_return_bps)) - Decimal(str(proposal.expected_cost_bps))

    @classmethod
    def _edge_clears_floor(cls, book: BookLimits, proposal: TradeProposal) -> bool:
        """The expected edge, against the book's floor.

        The first gate in this system to read a proposal's declared economics.
        Until Phase 7 a strategy could claim any expected return and nothing
        looked -- which is why the numbers had no reason to be honest.
        """

        return cls._expected_edge_bps(proposal) >= book.limits.min_expected_edge_after_cost_bps

    @staticmethod
    def _holding_within_book_limit(book: BookLimits, proposal: TradeProposal) -> bool:
        """Scalp books cap a position's duration; swing books do not declare
        one, so an isinstance guard -- not hasattr -- is what lets a swing
        proposal pass a limit its book was never given."""

        if not isinstance(book.limits, ScalpLimits):
            return True
        return proposal.max_holding_seconds <= book.limits.max_position_duration_seconds

    @classmethod
    def _swap_within_edge_fraction(cls, book: BookLimits, proposal: TradeProposal) -> bool:
        """Swing books cap swap as a percentage of expected edge.

        See _expected_edge_bps for why the conversion goes through str.
        """

        if not isinstance(book.limits, SwingLimits):
            return True
        edge = cls._expected_edge_bps(proposal)
        if edge <= 0:
            return False
        swap = Decimal(str(proposal.expected_swap_cost_bps))
        return swap / edge * Decimal(100) <= book.limits.max_swap_cost_pct_of_expected_edge

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
        portfolio: PortfolioState,
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
            portfolio=portfolio,
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

    def recheck_portfolio(
        self,
        decision: RiskDecision,
        *,
        book_id: BookId,
        side: Side,
        contract: InstrumentContract,
        firm_equity: Decimal,
        price: Decimal,
        portfolio: PortfolioState,
    ) -> RiskDecision:
        """The portfolio gates alone, for a decision made earlier.

        ``order submit`` is handed a decision rather than a proposal, and the
        portfolio may have moved since it was made -- ten decision files sized
        while flat would otherwise open ten positions. Risk is the larger of
        the decision's own and the loss from ``price`` (the quote it would fill
        at) to its stop, so a quote that drifted toward the stop cannot lower
        the risk the decision was sized for. Returns the decision unchanged, or
        a rejection naming every portfolio gate that refused.
        """

        if isinstance(decision, RejectedRiskDecision):
            return decision
        book = self._constitution.books.get(book_id)
        if book is None:
            return self._reject_decision(decision, [RejectionReason.UNKNOWN_BOOK], [])
        if firm_equity <= 0:
            return self._reject_decision(decision, [RejectionReason.NON_POSITIVE_EQUITY], [])
        quantity = decision.approved_quantity.amount
        risk_money = max(
            decision.risk_money,
            loss_to_stop(
                quantity=quantity,
                from_price=price,
                stop=decision.stop_loss_price,
                is_buy=side is Side.BUY,
                contract=contract,
            ),
        )
        reasons: list[RejectionReason] = []
        passed: list[RejectionReason] = []
        checks = [
            *self._state_checks(book_id, book, portfolio),
            *self._exposure_checks(
                book_id,
                book,
                portfolio,
                contract=contract,
                firm_equity=firm_equity,
                risk_money=risk_money,
                notional=notional_money(quantity, price, contract),
            ),
        ]
        for ok, reason in checks:
            _record(ok, reason, reasons, passed)
        if reasons:
            return self._reject_decision(decision, reasons, passed)
        return decision

    def _state_checks(
        self, book_id: BookId, book: BookLimits, portfolio: PortfolioState
    ) -> list[tuple[bool, RejectionReason]]:
        """The gates that need only what is already open, lost and sent."""

        firm = self._constitution.firm
        now = self._clock.now()
        pnl_known = (
            portfolio.book_pnl is not None
            and book_id in portfolio.book_pnl
            and portfolio.firm_drawdown is not None
        )
        rate_ok = portfolio.orders_in_window(now) < firm.max_orders_per_minute
        if isinstance(book.limits, ScalpLimits):
            rate_ok = rate_ok and (
                portfolio.orders_in_window(now, book=book_id) < book.limits.max_orders_per_minute
            )
        return [
            (portfolio.unmeasured_positions == 0, RejectionReason.UNMEASURED_OPEN_RISK),
            (pnl_known, RejectionReason.PNL_UNAVAILABLE),
            (
                portfolio.consecutive_rejects < firm.max_consecutive_rejects,
                RejectionReason.CONSECUTIVE_REJECTS_EXCEEDED,
            ),
            (rate_ok, RejectionReason.ORDER_RATE_EXCEEDED),
            (
                len(portfolio.book_exposures(book_id)) < book.max_concurrent_positions,
                RejectionReason.MAX_CONCURRENT_POSITIONS,
            ),
        ]

    def _exposure_checks(
        self,
        book_id: BookId,
        book: BookLimits,
        portfolio: PortfolioState,
        *,
        contract: InstrumentContract,
        firm_equity: Decimal,
        risk_money: Decimal,
        notional: Decimal,
    ) -> list[tuple[bool, RejectionReason]]:
        """The gates that need the candidate's own risk and notional.

        Each asks whether the candidate could be the order that breaches its
        limit, and each holds at equality. The daily and drawdown halts count
        every open position's risk to its stop as well as what is already lost,
        which is what makes them ceilings rather than triggers that fire after
        the damage (Phase 10 design, section 4).
        """

        firm = self._constitution.firm
        hundred = Decimal(100)
        book_equity = firm_equity * book.capital_fraction
        book_open = portfolio.open_risk(book=book_id)
        firm_open = portfolio.open_risk()

        def within(amount: Decimal, pct: Decimal, equity: Decimal) -> bool:
            return amount * hundred <= pct * equity

        checks: list[tuple[bool, RejectionReason]] = []
        pnl = None if portfolio.book_pnl is None else portfolio.book_pnl.get(book_id)
        # Unknown P&L is already a refusal (PNL_UNAVAILABLE); the halts that
        # read it are not evaluated against a number nobody computed.
        if pnl is not None:
            checks.append(
                (
                    within(
                        pnl.loss_today() + book_open + risk_money,
                        book.daily_loss_stop_pct,
                        book_equity,
                    ),
                    RejectionReason.DAILY_LOSS_STOP,
                )
            )
            checks.append(
                (
                    within(
                        pnl.drawdown + book_open + risk_money,
                        book.max_drawdown_halt_pct,
                        book_equity,
                    ),
                    RejectionReason.BOOK_DRAWDOWN_HALT,
                )
            )
        if portfolio.firm_drawdown is not None:
            checks.append(
                (
                    within(
                        portfolio.firm_drawdown + firm_open + risk_money,
                        firm.max_total_drawdown_halt_pct,
                        firm_equity,
                    ),
                    RejectionReason.FIRM_DRAWDOWN_HALT,
                )
            )
        checks.extend(
            [
                (
                    within(firm_open + risk_money, firm.max_aggregate_open_risk_pct, firm_equity),
                    RejectionReason.AGGREGATE_OPEN_RISK,
                ),
                (
                    within(
                        portfolio.instrument_risk(contract.instrument_id) + risk_money,
                        firm.max_single_instrument_risk_pct,
                        firm_equity,
                    ),
                    RejectionReason.SINGLE_INSTRUMENT_RISK,
                ),
                (
                    all(
                        within(
                            portfolio.cluster_risk(key) + risk_money,
                            firm.max_correlated_cluster_risk_pct,
                            firm_equity,
                        )
                        for key in sorted(cluster_keys(contract))
                    ),
                    RejectionReason.CORRELATED_CLUSTER_RISK,
                ),
                (
                    portfolio.notional(book=book_id) + notional
                    <= book.max_gross_leverage * book_equity,
                    RejectionReason.BOOK_GROSS_LEVERAGE,
                ),
                (
                    portfolio.notional() + notional <= firm.max_gross_leverage * firm_equity,
                    RejectionReason.FIRM_GROSS_LEVERAGE,
                ),
            ]
        )
        return checks

    def _reject_decision(
        self,
        decision: RiskDecision,
        reasons: Sequence[RejectionReason],
        passed: Sequence[RejectionReason],
    ) -> RejectedRiskDecision:
        return RejectedRiskDecision(
            verdict="REJECTED",
            proposal_id=decision.proposal_id,
            reasons=tuple(reason.value for reason in reasons),
            checks_passed=tuple(check.value for check in passed),
            constitution_version=self._constitution.version,
            approved_quantity=Quantity(amount=Decimal(0), unit="lots"),
            risk_money=Decimal(0),
            risk_pct_of_book=Decimal(0),
        )

    def _leverage_cap(
        self,
        book_id: BookId,
        book: BookLimits,
        portfolio: PortfolioState,
        contract: InstrumentContract,
        firm_equity: Decimal,
        price: Decimal,
    ) -> _LeverageCap:
        """The largest on-grid volume both gross-leverage limits still admit."""

        per_lot = notional_money(Decimal(1), price, contract)
        headrooms = (
            (
                book.max_gross_leverage * firm_equity * book.capital_fraction
                - portfolio.notional(book=book_id),
                RejectionReason.BOOK_GROSS_LEVERAGE,
            ),
            (
                self._constitution.firm.max_gross_leverage * firm_equity - portfolio.notional(),
                RejectionReason.FIRM_GROSS_LEVERAGE,
            ),
        )
        volumes = {
            reason: (
                quantise_down(headroom / per_lot, contract.quantity_increment)
                if headroom > 0
                else Decimal(0)
            )
            for headroom, reason in headrooms
        }
        cap = min(volumes.values())
        binding = tuple(
            reason for reason, volume in volumes.items() if volume < contract.quantity_min
        )
        return _LeverageCap(volume=cap, binding=binding)


@dataclass(frozen=True, slots=True)
class _LeverageCap:
    volume: Decimal
    binding: tuple[RejectionReason, ...]
