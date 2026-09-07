"""Guards the field names ``terminal.py``'s mapping depends on.

``Mt5Terminal.send_order`` and ``Mt5Terminal.history_deals`` read specific
attributes off the raw objects MetaTrader5 returns -- ``d.position_id``, not
``d.position``; ``result.order``, not ``result.position``, which does not
exist. Every dataclass test in this package only proves ``Mt5SendResult`` and
``Mt5Deal`` store what they are handed; nothing else exercises the mapping
itself, so a swapped or renamed field would fail nothing until reconciliation
silently stopped matching in production.

These names were verified once by hand against the installed MetaTrader5
package while writing that mapping. This test is what makes that verification
survive an MT5 upgrade that renames or drops a field, instead of trusting it
stays true forever. It never connects to a terminal and never sends
anything -- it only inspects the shape of the two result types via
introspection, the same way ``tests/live/conftest.py`` treats an unimportable
MetaTrader5 as a reason to skip rather than fail.
"""

import pytest

try:
    import MetaTrader5 as mt5
except ImportError:
    pytest.skip("MetaTrader5 is not installed", allow_module_level=True)


def test_trade_deal_exposes_position_id_not_position() -> None:
    """``history_deals()`` reads ``d.position_id``. If a future SDK renamed
    it back to ``position``, reconciliation would silently stop matching."""

    fields = dir(mt5.TradeDeal)
    assert "position_id" in fields
    assert "position" not in fields


def test_order_send_result_exposes_the_fields_send_order_reads() -> None:
    """``send_order()`` builds ``Mt5SendResult`` from exactly these fields."""

    fields = dir(mt5.OrderSendResult)
    for name in ("order", "deal", "retcode", "volume", "price", "comment"):
        assert name in fields


def test_order_send_result_has_no_position_field() -> None:
    """``OrderSendResult`` carries no ``position`` field on this build, which
    is why ``Mt5SendResult.position_ticket`` is always ``None`` -- there is
    nothing for ``send_order()`` to read it from, and nothing downstream may
    depend on it being populated."""

    assert "position" not in dir(mt5.OrderSendResult)
