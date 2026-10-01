"""Constants fixed before any Phase 8C3 output exists (spec S-11).

Each value is a literal that a test pins, so it cannot move after results do. They are
what 8D reads. Phase 8C3's measurements compare against none of them except
``MAX_DRAWDOWN`` (less ``DRAWDOWN_TOLERANCE``), the level at which the Monte Carlo counts a
replicate as halted, and
``CPCV_P5_QUANTILE``, the rank the 5th-percentile measurement reads; ``MC_POLICY_VERSION``
seeds the Monte Carlo stream.
"""

from __future__ import annotations

from typing import Final

DSR_MINIMUM: Final = 0.95
PBO_MAXIMUM: Final = 0.50
MAX_DRAWDOWN: Final = 0.10
"""The drawdown fraction of the running peak that halts a Monte Carlo replicate."""
DRAWDOWN_TOLERANCE: Final = 1e-12
"""Float rounding allowance at ``MAX_DRAWDOWN``: an exact 10% fall measures
0.09999999999999998 from a flat start. A drawdown counts as AT the limit when it is
``>= MAX_DRAWDOWN - DRAWDOWN_TOLERANCE``. The future gate passes only when the drawdown is
``< MAX_DRAWDOWN - DRAWDOWN_TOLERANCE``: stricter by one part in 10**12, so rounding can
never let a true 10% fall through."""
MIN_OOS_TRADES: Final = 30
MIN_REGIMES: Final = 2
CPCV_P5_QUANTILE: Final = 0.05
MIN_WFA_FOLDS: Final = 1
MC_POLICY_VERSION: Final = "8c-mc-1"
"""Differs from the bootstrap's ``8c-sb-1`` so the two seeded streams are independent."""
