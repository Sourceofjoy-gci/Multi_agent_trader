"""Phase 1.5 acceptance: market-data reads cannot outrun what was knowable.

Import confinement for MetaTrader5 -- that it is reachable from exactly one
module, and never from ``marketdata/`` -- is owned by ``test_architecture.py``,
which also carries the guard-the-guard tests proving those checks can still
fail. This file restates the marketdata-specific rule at phase level and adds
the two guarantees that are this phase's own: I-17's availability filter, and
that no float ever reaches a stored price.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from trading_house.marketdata.models import Bar, BarQuality, Timeframe

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MARKETDATA_ROOT = PROJECT_ROOT / "src" / "trading_house" / "marketdata"
STORE_MODULE = MARKETDATA_ROOT / "store.py"

# The two consumer-facing bar queries in store.py -- the clean-only default
# and the include_defective variant BarStore.bars() chooses between. Both
# must carry an availability_time predicate against the caller's as_of.
#
# Two other module-level SQL constants in store.py are deliberately NOT
# bound by this rule, and must never be added to the tuple below:
#   - _COVERAGE_SQL backs Coverage, which reports what the store *holds*,
#     not what is *knowable*. That distinction is exactly what
#     Coverage.latest_availability_time exists to expose separately.
#   - _MISSING_KEYS_SQL is internal write-path machinery append_bars() uses
#     to classify a duplicate from a conflict. No consumer API can reach it.
BAR_RETURNING_QUERY_NAMES = ("_BARS_SQL", "_BARS_SQL_CLEAN_ONLY")


def _module_string_constants(module: Path) -> dict[str, str]:
    """Every top-level ``NAME = "..."`` assignment in ``module``."""

    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    constants: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            constants[node.targets[0].id] = node.value.value
    return constants


def test_no_consumer_read_path_can_skip_the_availability_filter() -> None:
    """I-17. Both bar-returning queries -- clean-only and include_defective --
    carry an ``availability_time <=`` predicate. ``coverage()`` and the
    internal duplicate/conflict lookup are named above as deliberate
    exemptions, not oversights."""

    constants = _module_string_constants(STORE_MODULE)

    for name in BAR_RETURNING_QUERY_NAMES:
        assert name in constants, f"expected a module-level constant named {name} in store.py"
        assert "availability_time <=" in constants[name], name


def _bar(**overrides: object) -> Bar:
    kwargs: dict[str, object] = {
        "instrument_id": "fx.eurusd",
        "timeframe": Timeframe.M1,
        "event_time": datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
        "availability_time": datetime(2026, 8, 25, 9, 1, tzinfo=UTC),
        "open": Decimal("1.10000"),
        "high": Decimal("1.10050"),
        "low": Decimal("1.09950"),
        "close": Decimal("1.10020"),
        "tick_volume": 42,
        "spread": 9,
        "real_volume": 0,
        "quality": BarQuality.OK,
    }
    kwargs.update(overrides)
    return Bar(**kwargs)  # type: ignore[arg-type]


def test_no_float_reaches_a_stored_price() -> None:
    """Bar's OHLC fields are Decimal, and CanonicalModel is strict, so a
    float is a ValidationError rather than a silent binary artifact."""

    with pytest.raises(ValidationError):
        _bar(open=1.1)


def test_marketdata_never_imports_metatrader5() -> None:
    """Phase-level restatement; test_architecture.py owns the general rule
    and its guard-the-guard companion."""

    offenders = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in sorted(MARKETDATA_ROOT.rglob("*.py"))
        if "MetaTrader5"
        in {
            alias.name.split(".")[0]
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
            if isinstance(node, ast.Import)
            for alias in node.names
        }
    ]

    assert offenders == []
