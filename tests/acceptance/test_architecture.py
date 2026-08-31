"""Executable guards for Phase 0's authority and no-trading boundaries.

These walk the real source tree with ``ast`` rather than trusting convention,
so a future edit that reaches for a broker, an agent framework, a private key,
or a migration from runtime code fails the build.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

from trading_house.constitution.models import Constitution, ConstitutionModel
from trading_house.settings import RuntimeSettings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src" / "trading_house"
SIGNING_MODULE = SOURCE_ROOT / "constitution" / "signing.py"

FORBIDDEN_TOP_LEVEL_IMPORTS = frozenset({"langgraph", "openai", "anthropic", "ccxt"})
MT5_IMPORT_ALLOWED = frozenset({SOURCE_ROOT / "brokers" / "mt5" / "terminal.py"})
TERMINAL_STATEMENT_CAP = 80
PRIVATE_KEY_SYMBOLS = frozenset({"load_pem_private_key", "Ed25519PrivateKey"})
MIGRATION_CALL_NAMES = frozenset({"upgrade", "downgrade"})
SIGNING_PRIMITIVES = frozenset({"sign_bytes", "sign_file", "load_private_key", "generate_key_pair"})
# signing.py implements them; cli.py exposes the documented offline sign command.
# Nothing on the runtime startup path may reach either.
SIGNING_ALLOWED = frozenset({SIGNING_MODULE, SOURCE_ROOT / "cli.py"})
RUNTIME_STARTUP_PATH = SOURCE_ROOT / "ops" / "health.py"
# I-11: the provider boundary must never be able to reach broker credentials,
# the database that stores runtime state, or process settings.
CREDENTIAL_BEARING = frozenset(
    {"trading_house.settings", "trading_house.database", "trading_house.brokers", "psycopg"}
)
AGENTS_ROOT = SOURCE_ROOT / "agents"
# The reverse of CREDENTIAL_BEARING: no process holding broker credentials may
# execute agent-authored code (I-11's other direction). These are exactly the
# modules that could hold or reach credentials.
CREDENTIAL_HOLDING_ROOTS = (
    SOURCE_ROOT / "brokers",
    SOURCE_ROOT / "database",
    SOURCE_ROOT / "settings.py",
)
AGENTS_MODULE = frozenset({"trading_house.agents"})


def _source_files() -> list[Path]:
    return sorted(SOURCE_ROOT.rglob("*.py"))


def _parsed() -> Iterator[tuple[Path, ast.Module]]:
    for path in _source_files():
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_top_level(tree: ast.Module) -> set[str]:
    packages: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            packages.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            packages.add(node.module.split(".")[0])
    return packages


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            names.update(alias.name for alias in node.names)
    return names


def _imported_modules(tree: ast.Module) -> set[str]:
    """Fully-qualified module paths, for both import forms."""

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def _reaches(tree: ast.Module, forbidden: frozenset[str]) -> set[str]:
    """Modules imported that are, or live under, a forbidden path."""

    return {
        module
        for module in _imported_modules(tree)
        if any(module == root or module.startswith(f"{root}.") for root in forbidden)
    }


def _referenced_attributes(tree: ast.Module) -> set[str]:
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def test_source_tree_is_not_empty() -> None:
    """A silent glob failure must not make every other guard vacuous."""

    assert len(_source_files()) >= 10


def test_no_module_imports_a_broker_or_agent_framework() -> None:
    offenders = {
        path.relative_to(PROJECT_ROOT).as_posix(): sorted(
            _imported_top_level(tree) & FORBIDDEN_TOP_LEVEL_IMPORTS
        )
        for path, tree in _parsed()
        if _imported_top_level(tree) & FORBIDDEN_TOP_LEVEL_IMPORTS
    }

    assert offenders == {}


def test_metatrader5_is_importable_from_exactly_one_module() -> None:
    """The venue-neutral core exists so a second broker is cheap. One file
    may speak MT5; anything wider re-creates the coupling this phase removed."""

    offenders = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path, tree in _parsed()
        if path not in MT5_IMPORT_ALLOWED and "MetaTrader5" in _imported_top_level(tree)
    ]

    assert offenders == []


def test_the_terminal_module_is_where_metatrader5_actually_lives() -> None:
    """Guard the guard: if terminal.py stops importing it, the exemption is stale."""

    terminal = SOURCE_ROOT / "brokers" / "mt5" / "terminal.py"
    assert terminal.exists()
    tree = ast.parse(terminal.read_text(encoding="utf-8"))
    assert "MetaTrader5" in _imported_top_level(tree)


def test_the_terminal_module_stays_thin() -> None:
    """terminal.py is omitted from coverage, so a size cap is what stops it
    becoming the place untested logic accumulates."""

    terminal = SOURCE_ROOT / "brokers" / "mt5" / "terminal.py"
    tree = ast.parse(terminal.read_text(encoding="utf-8"))
    statements = sum(1 for node in ast.walk(tree) if isinstance(node, ast.stmt))

    assert statements <= TERMINAL_STATEMENT_CAP, (
        f"terminal.py has {statements} statements; move logic into the pure modules"
    )


def test_only_the_signing_module_touches_private_key_primitives() -> None:
    offenders: dict[str, list[str]] = {}
    for path, tree in _parsed():
        if path == SIGNING_MODULE:
            continue
        found = (_imported_names(tree) | _referenced_attributes(tree)) & PRIVATE_KEY_SYMBOLS
        if found:
            offenders[path.relative_to(PROJECT_ROOT).as_posix()] = sorted(found)

    assert offenders == {}


def test_the_signing_module_is_where_private_keys_actually_live() -> None:
    """Guard the guard: if signing moves, the exemption above must move too."""

    assert SIGNING_MODULE.exists()
    tree = ast.parse(SIGNING_MODULE.read_text(encoding="utf-8"))
    assert (_imported_names(tree) | _referenced_attributes(tree)) & PRIVATE_KEY_SYMBOLS


def test_no_runtime_module_can_sign_the_constitution() -> None:
    """Definition of Done: no runtime component can sign or rewrite the constitution."""

    offenders: dict[str, list[str]] = {}
    for path, tree in _parsed():
        if path in SIGNING_ALLOWED:
            continue
        found = (_imported_names(tree) | _referenced_attributes(tree)) & SIGNING_PRIMITIVES
        if found:
            offenders[path.relative_to(PROJECT_ROOT).as_posix()] = sorted(found)

    assert offenders == {}


def test_the_readiness_path_never_reaches_signing() -> None:
    """The startup gate verifies signatures; it must have no way to create one."""

    tree = ast.parse(RUNTIME_STARTUP_PATH.read_text(encoding="utf-8"))
    reachable = (_imported_names(tree) | _referenced_attributes(tree)) & (
        SIGNING_PRIMITIVES | PRIVATE_KEY_SYMBOLS
    )

    assert reachable == set()


def test_runtime_source_never_imports_the_alembic_command_api() -> None:
    offenders = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path, tree in _parsed()
        if "command"
        in {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "alembic"
            for alias in node.names
        }
        or "alembic.command" in _imported_names(tree)
    ]

    assert offenders == []


def test_runtime_source_never_calls_upgrade_or_downgrade() -> None:
    offenders: dict[str, list[str]] = {}
    for path, tree in _parsed():
        called = sorted(
            {
                node.func.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in MIGRATION_CALL_NAMES
            }
        )
        if called:
            offenders[path.relative_to(PROJECT_ROOT).as_posix()] = called

    assert offenders == {}


def test_settings_cannot_carry_any_risk_limit() -> None:
    """Invariant I-2: limits come from signed config, never the environment."""

    limit_words = ("risk", "loss", "drawdown", "leverage", "limit", "capital")
    offenders = [
        name
        for name in RuntimeSettings.model_fields
        if any(word in name.lower() for word in limit_words)
    ]

    assert offenders == []


def test_every_constitution_model_is_frozen_and_closed() -> None:
    """Invariant I-2: a loaded limit cannot be edited in memory."""

    pending = [ConstitutionModel]
    seen: set[type[ConstitutionModel]] = set()
    while pending:
        for subclass in pending.pop().__subclasses__():
            seen.add(subclass)
            pending.append(subclass)

    assert Constitution in seen
    for model in seen:
        assert model.model_config.get("frozen") is True, model.__name__
        assert model.model_config.get("extra") == "forbid", model.__name__
        assert model.model_config.get("strict") is True, model.__name__


@pytest.mark.parametrize("forbidden", sorted(FORBIDDEN_TOP_LEVEL_IMPORTS))
def test_forbidden_import_detection_actually_works(forbidden: str) -> None:
    """Prove the AST guard fires, so a passing suite is not a false negative."""

    tree = ast.parse(f"import {forbidden}\n")

    assert _imported_top_level(tree) & FORBIDDEN_TOP_LEVEL_IMPORTS == {forbidden}


def test_no_agent_provider_reaches_the_database_or_broker() -> None:
    """I-11: no module anywhere under agents/ may be able to see credentials.

    Walking every file (not just providers/base.py) means a future concrete
    provider under agents/providers/ -- precisely where a credential reach
    would occur -- stays covered instead of the guard silently going blind.
    """

    offenders: dict[str, list[str]] = {}
    for path in sorted(AGENTS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        reached = _reaches(tree, CREDENTIAL_BEARING)
        if reached:
            offenders[path.relative_to(PROJECT_ROOT).as_posix()] = sorted(reached)

    assert offenders == {}


@pytest.mark.parametrize(
    "statement",
    [
        "from trading_house.settings import RuntimeSettings",
        "from trading_house.database import open_runtime_connection",
        "from trading_house.brokers.base import BrokerAdapter",
        "import psycopg",
        "from psycopg import connect",
    ],
)
def test_credential_reach_detection_actually_works(statement: str) -> None:
    """Prove the provider guard fires, so a passing suite is not a false negative."""

    assert _reaches(ast.parse(statement + "\n"), CREDENTIAL_BEARING)


def test_no_credential_holding_module_imports_agent_authored_code() -> None:
    """I-11, the unguarded direction: no process holding broker credentials
    (brokers/, database/, settings.py) may execute agent-authored code."""

    offenders: dict[str, list[str]] = {}
    for root in CREDENTIAL_HOLDING_ROOTS:
        paths = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            reached = _reaches(tree, AGENTS_MODULE)
            if reached:
                offenders[path.relative_to(PROJECT_ROOT).as_posix()] = sorted(reached)

    assert offenders == {}


@pytest.mark.parametrize(
    "statement",
    [
        "import trading_house.agents",
        "from trading_house.agents.providers.base import AgentRun",
        "from trading_house.agents.providers import base",
    ],
)
def test_credential_holder_reach_into_agents_detection_actually_works(statement: str) -> None:
    """Prove the reverse guard fires, so a passing suite is not a false negative."""

    assert _reaches(ast.parse(statement + "\n"), AGENTS_MODULE)
