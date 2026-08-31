import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "broker_audit", PROJECT_ROOT / "scripts" / "broker_audit.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The module must be registered before exec so that dataclasses' own
    # annotation-resolution machinery (frozen=True, slots=True with
    # `from __future__ import annotations`) can find it via
    # sys.modules[cls.__module__] — CPython 3.12 raises AttributeError
    # on a bare module_from_spec()/exec_module() otherwise.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_report_renders_every_field_the_spec_requires() -> None:
    """Spec 17.2 names the exact fields that decide strategy feasibility."""

    module = _load_module()
    row = module.AuditRow(
        server_symbol="EURUSD",
        trade_mode=4,
        trade_exemode=2,
        filling_mode=2,
        trade_stops_level=0,
        trade_freeze_level=0,
        volume_min=0.01,
        volume_step=0.01,
        volume_max=200.0,
        trade_tick_value_loss=1.0,
        trade_tick_size=1e-05,
        digits=5,
        swap_long=-0.5,
        swap_short=0.2,
        median_spread_points=8.0,
    )

    report = module.format_audit_report([row])

    for token in (
        "EURUSD",
        "trade_mode",
        "trade_exemode",
        "filling_mode",
        "trade_stops_level",
        "trade_freeze_level",
        "volume_min",
        "volume_step",
        "volume_max",
        "trade_tick_value_loss",
        "swap_long",
        "swap_short",
        "median_spread_points",
    ):
        assert token in report


def test_report_flags_a_zero_stops_level() -> None:
    """stops_level == 0 is the case that breaks a PositiveDecimal contract."""

    module = _load_module()
    row = module.AuditRow(
        server_symbol="EURUSD",
        trade_mode=4,
        trade_exemode=2,
        filling_mode=2,
        trade_stops_level=0,
        trade_freeze_level=0,
        volume_min=0.01,
        volume_step=0.01,
        volume_max=200.0,
        trade_tick_value_loss=1.0,
        trade_tick_size=1e-05,
        digits=5,
        swap_long=0.0,
        swap_short=0.0,
        median_spread_points=8.0,
    )

    assert "zero stops_level" in module.format_audit_report([row]).lower()
