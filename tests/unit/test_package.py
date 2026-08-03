import importlib.util
import tomllib
from pathlib import Path


def test_package_exposes_version() -> None:
    import trading_house

    assert trading_house.__version__ == "0.1.0"


def test_console_entry_point_matches_cli_module_availability() -> None:
    project_root = Path(__file__).parents[2]
    metadata = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = metadata["project"].get("scripts", {})

    try:
        cli_available = importlib.util.find_spec("trading_house.cli") is not None
    except ModuleNotFoundError:
        cli_available = False

    if not cli_available:
        assert "trading-house" not in scripts
    else:
        assert scripts.get("trading-house") == "trading_house.cli:app"
