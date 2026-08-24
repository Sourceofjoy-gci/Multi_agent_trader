"""Phase 0.5 acceptance: the revised contracts hold end to end."""

import ast
from pathlib import Path

from trading_house.constitution.binding import load_venue_binding
from trading_house.constitution.loader import load_constitution

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG = PROJECT_ROOT / "config"

# venue.py is deliberately excluded: it defines Mt5VenueRef, the one place
# broker-shaped facts (magic, retcode) are allowed to live, precisely so the
# venue-neutral modules below never need to. See core/venue.py's docstring.
CANONICAL_MODULES = ("values.py", "instruments.py", "schemas.py")


def test_the_revised_constitution_verifies() -> None:
    loaded = load_constitution(
        CONFIG / "risk_constitution.yaml",
        CONFIG / "risk_constitution.yaml.sig",
        CONFIG / "risk_constitution.public.pem",
    )
    assert set(loaded.constitution.books) == {"fx_scalp", "fx_swing", "equity_swing", "sleeve"}


def test_the_venue_binding_verifies_and_covers_every_book() -> None:
    binding = load_venue_binding(
        CONFIG / "venue_binding.mt5.yaml",
        CONFIG / "venue_binding.mt5.yaml.sig",
        CONFIG / "risk_constitution.public.pem",
    )
    loaded = load_constitution(
        CONFIG / "risk_constitution.yaml",
        CONFIG / "risk_constitution.yaml.sig",
        CONFIG / "risk_constitution.public.pem",
    )
    assert set(binding.books) == set(loaded.constitution.books)


def test_the_constitution_names_no_venue() -> None:
    """A signed constitution must survive changing brokers."""

    text = (CONFIG / "risk_constitution.yaml").read_text(encoding="utf-8").lower()
    for token in ("mt5", "magic", "metatrader", "server_symbol"):
        assert token not in text


def test_no_canonical_module_mentions_a_broker_encoding() -> None:
    forbidden = {"magic", "deviation_points", "retcode", "filling"}
    for name in CANONICAL_MODULES:
        tree = ast.parse((PROJECT_ROOT / "src" / "trading_house" / "core" / name).read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
        assert forbidden.isdisjoint(names), name
