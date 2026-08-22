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

FORBIDDEN_TOP_LEVEL_IMPORTS = frozenset({"MetaTrader5", "langgraph", "openai", "anthropic", "ccxt"})
PRIVATE_KEY_SYMBOLS = frozenset({"load_pem_private_key", "Ed25519PrivateKey"})
MIGRATION_CALL_NAMES = frozenset({"upgrade", "downgrade"})


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
