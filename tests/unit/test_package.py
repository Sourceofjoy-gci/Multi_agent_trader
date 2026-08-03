import tomllib
from pathlib import Path


def test_package_exposes_version() -> None:
    import trading_house

    assert trading_house.__version__ == "0.1.0"


def test_project_metadata_defers_console_script_until_cli_exists() -> None:
    project_root = Path(__file__).parents[2]
    metadata = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"].get("scripts", {}) == {}
