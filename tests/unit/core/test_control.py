"""Phase 11: what a halt may be, and which orders it stops."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from trading_house.core.control import (
    SYSTEM_ACTOR,
    Halt,
    HaltKind,
    HaltRequest,
    HaltScope,
    blocking,
)

AT = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def _halt(
    kind: HaltKind = HaltKind.KILL,
    scope: HaltScope = HaltScope.BOOK,
    target: str | None = "fx_scalp",
    halt_id: str = "h-1",
) -> Halt:
    return Halt(
        kind=kind,
        scope=scope,
        target=target,
        reason="test",
        actor="operator",
        halt_id=halt_id,
        entered_at=AT,
    )


@pytest.mark.parametrize(
    ("kind", "scope", "target"),
    [
        (HaltKind.SAFE_MODE, HaltScope.BOOK, "fx_scalp"),
        (HaltKind.DRAWDOWN_HALT, HaltScope.INSTRUMENT, "fx.eurusd"),
        (HaltKind.KILL, HaltScope.FIRM, "fx_scalp"),
        (HaltKind.KILL, HaltScope.BOOK, None),
    ],
    ids=[
        "safe-mode-is-firm-wide",
        "drawdown-is-book-or-firm",
        "firm-has-no-target",
        "book-needs-one",
    ],
)
def test_a_halt_that_cannot_mean_anything_is_refused(
    kind: HaltKind, scope: HaltScope, target: str | None
) -> None:
    with pytest.raises(ValidationError):
        HaltRequest(kind=kind, scope=scope, target=target, reason="r", actor=SYSTEM_ACTOR)


def test_a_firm_halt_stops_everything() -> None:
    halt = _halt(scope=HaltScope.FIRM, target=None)

    assert halt.blocks(book="sleeve", instrument_id="metal.xauusd", strategy_id="any")


@pytest.mark.parametrize(
    ("scope", "target"),
    [
        (HaltScope.BOOK, "fx_scalp"),
        (HaltScope.INSTRUMENT, "fx.eurusd"),
        (HaltScope.STRATEGY, "vol_breakout_eurusd_h1"),
    ],
)
def test_a_scoped_halt_stops_only_what_it_names(scope: HaltScope, target: str) -> None:
    halt = _halt(scope=scope, target=target)
    order = {
        "book": "fx_scalp",
        "instrument_id": "fx.eurusd",
        "strategy_id": "vol_breakout_eurusd_h1",
    }
    elsewhere = {
        "book": "fx_swing",
        "instrument_id": "metal.xauusd",
        "strategy_id": "session_momentum_eurusd",
    }

    assert halt.blocks(**order)
    assert not halt.blocks(**elsewhere)


def test_blocking_returns_only_the_halts_that_cover_the_order() -> None:
    covering = _halt(halt_id="a")
    other_book = _halt(target="sleeve", halt_id="b")

    assert blocking(
        (covering, other_book), book="fx_scalp", instrument_id="fx.eurusd", strategy_id="s"
    ) == (covering,)


def test_the_same_switch_is_kind_scope_and_target_not_reason_or_actor() -> None:
    first = HaltRequest(
        kind=HaltKind.KILL, scope=HaltScope.BOOK, target="fx_scalp", reason="a", actor="x"
    )
    again = first.model_copy(update={"reason": "b", "actor": "y"})
    other = first.model_copy(update={"target": "sleeve"})

    assert first.same_switch(again)
    assert not first.same_switch(other)
