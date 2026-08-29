"""Neutral classification of MetaTrader 5 trade return codes.

Unrecognised codes fail closed to ``RejectReason.UNKNOWN``, which is classified
``AUTHORITY`` and therefore enters safe mode. Guessing an unknown code as
transient would let recovery retry it forever.

Codes with no clean neutral equivalent are deliberately absent. ``10015
INVALID_PRICE`` is the clearest: it means the price we sent was malformed,
which is contractual rather than transient, and ``RejectReason`` has no
``INVALID_PRICE`` member. Adding one later is additive and cheap;
mis-classifying it now is a live retry loop.
"""

from trading_house.core.venue import RejectReason

RETCODE_REJECT_REASON: dict[int, RejectReason] = {
    10004: RejectReason.REQUOTE,
    10014: RejectReason.INVALID_QUANTITY,
    10016: RejectReason.INVALID_STOPS,
    10018: RejectReason.MARKET_CLOSED,
    10019: RejectReason.INSUFFICIENT_FUNDS,
    10020: RejectReason.PRICE_CHANGED,
    10024: RejectReason.TIMEOUT,
    10026: RejectReason.TRADE_DISABLED,
    10027: RejectReason.TRADE_DISABLED,
    10030: RejectReason.UNSUPPORTED_FILL,
    10031: RejectReason.DISCONNECTED,
}

SUCCESS_RETCODES = frozenset({10008, 10009})


def reject_reason_for(retcode: int) -> RejectReason:
    """Classify a retcode, failing closed on anything unrecognised."""

    return RETCODE_REJECT_REASON.get(retcode, RejectReason.UNKNOWN)
