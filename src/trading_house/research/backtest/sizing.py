"""How a backtest sizes its positions.

Constant notional is the canonical edge and comparability path (D-4): every
decision is sized from the initial capital. Compounding sizes from initial
capital plus realized PnL, as a separate rerun (Phase 8B3, C-2).
"""

from __future__ import annotations

from enum import Enum


class SizingMode(str, Enum):  # noqa: UP042
    CONSTANT_NOTIONAL = "constant_notional"
    COMPOUNDING = "compounding"


def is_constant_notional(value: object) -> bool:
    """``Field(exclude_if=...)``: a constant-notional outcome serializes as it
    did before this field existed, so no pinned digest moves."""

    return value is SizingMode.CONSTANT_NOTIONAL
