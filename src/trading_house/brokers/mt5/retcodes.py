"""Neutral classification of MetaTrader 5 trade return codes.

Unrecognised codes fail closed to ``RejectReason.UNKNOWN``, which is classified
``AUTHORITY`` and therefore enters safe mode. Guessing an unknown code as
transient would let recovery retry it forever.

Codes with no clean neutral equivalent are deliberately absent. ``10015
INVALID_PRICE`` is the clearest: it means the price we sent was malformed,
which is contractual rather than transient, and ``RejectReason`` has no
``INVALID_PRICE`` member. Adding one later is additive and cheap;
mis-classifying it now is a live retry loop.

Every entry below carries the documented MT5 constant name as a comment so a
future reader can check this table against the MT5 docs directly, without
cross-referencing anything else.
"""

from trading_house.core.venue import RejectReason

RETCODE_REJECT_REASON: dict[int, RejectReason] = {
    10004: RejectReason.REQUOTE,  # TRADE_RETCODE_REQUOTE
    10012: RejectReason.TIMEOUT,  # TRADE_RETCODE_TIMEOUT
    10014: RejectReason.INVALID_QUANTITY,  # TRADE_RETCODE_INVALID_VOLUME
    10016: RejectReason.INVALID_STOPS,  # TRADE_RETCODE_INVALID_STOPS
    10017: RejectReason.TRADE_DISABLED,  # TRADE_RETCODE_TRADE_DISABLED
    10018: RejectReason.MARKET_CLOSED,  # TRADE_RETCODE_MARKET_CLOSED
    10019: RejectReason.INSUFFICIENT_FUNDS,  # TRADE_RETCODE_NO_MONEY
    10020: RejectReason.PRICE_CHANGED,  # TRADE_RETCODE_PRICE_CHANGED
    # TRADE_RETCODE_TOO_MANY_REQUESTS, not a timeout: this is client-side rate
    # limiting. Mapped to TIMEOUT anyway because both are TRANSIENT and call
    # for the same response — back off and retry. A dedicated RATE_LIMITED
    # reason is a Phase 3 consideration once real submission makes the
    # distinction actionable.
    10024: RejectReason.TIMEOUT,  # TRADE_RETCODE_TOO_MANY_REQUESTS
    10026: RejectReason.TRADE_DISABLED,  # TRADE_RETCODE_SERVER_DISABLES_AT
    10027: RejectReason.TRADE_DISABLED,  # TRADE_RETCODE_CLIENT_DISABLES_AT
    10030: RejectReason.UNSUPPORTED_FILL,  # TRADE_RETCODE_INVALID_FILL
    10031: RejectReason.DISCONNECTED,  # TRADE_RETCODE_CONNECTION
}

# TRADE_RETCODE_PLACED (10008) and TRADE_RETCODE_DONE (10009) are outright
# successes. TRADE_RETCODE_DONE_PARTIAL (10010) is a partial fill, not a
# rejection, so it belongs here too; real handling of partial fills is a
# Phase 3 concern once orders are actually submitted.
SUCCESS_RETCODES = frozenset({10008, 10009, 10010})

# ``order_check`` does not share that vocabulary. It reports a request that
# would be accepted as retcode 0 with comment "Done"; the 1000x codes above are
# submission reply codes and never appear in a check result. Reusing the
# submission set here would classify every acceptable order as an unrecognised
# rejection, which fails closed into safe mode rather than trading -- safe, but
# it would mean precheck never returns True against a real broker.
ORDER_CHECK_PASSED = 0


def check_passed(retcode: int) -> bool:
    """Whether an ``order_check`` simulation reports the request acceptable."""

    return retcode == ORDER_CHECK_PASSED


def reject_reason_for(retcode: int) -> RejectReason:
    """Classify a retcode, failing closed on anything unrecognised."""

    return RETCODE_REJECT_REASON.get(retcode, RejectReason.UNKNOWN)
