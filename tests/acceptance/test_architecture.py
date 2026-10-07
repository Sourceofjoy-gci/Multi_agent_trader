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
MARKETDATA_ROOT = SOURCE_ROOT / "marketdata"
TERMINAL_STATEMENT_CAP = 90
"""Raised from 80 once, for Phase 10a's ``copy_ticks_range`` (spec D-6): MetaTrader5
may be imported in one module only, so the tick call has to live here. Conversion
stays in ``boundary.py`` and the adapter."""
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
FEATURES_ROOT = SOURCE_ROOT / "features"
# features/ may reach marketdata/ and core/, nothing else this project owns:
# a feature must never fetch from a broker itself, and Section 8.2 -- not
# this package -- turns a feature into a risk decision.
FEATURES_FORBIDDEN = frozenset({"trading_house.brokers", "trading_house.risk"})
RISK_ROOT = SOURCE_ROOT / "risk"
# risk/ may reach core/ and constitution/, nothing else this project owns. The
# same arithmetic has to produce the same answer in live trading and in a
# backtester with no terminal, and a broker or store import would end that.
RISK_FORBIDDEN = frozenset(
    {"trading_house.brokers", "trading_house.marketdata", "trading_house.features"}
)
BACKTEST_ROOT = SOURCE_ROOT / "research" / "backtest"
# An ALLOWLIST, which is what spec section 4 states: research/backtest/ "may
# import core/, marketdata/, features/ and risk/" -- plus its own modules. The
# denylist this replaces named only brokers/ and execution/, so an import of
# database/, audit/, ops/ or constitution/ passed a guard whose docstring
# claimed to enforce the sentence above, and the spec's own remark that adding
# strategies/ later "widens the allowlist deliberately" had no allowlist to
# widen.
#
# The two the denylist did name are still the two that matter most: a simulator
# that could place an order is no longer a simulator, and one that could read a
# terminal would make a replay depend on whether MetaTrader 5 happened to be
# running. The composition shim that joins this package to a live bar store
# lives in ops/, exactly as the position guard's does -- which is why ops/ is
# NOT on this list.
#
# research/ is admitted at research.backtest rather than whole: research/ also
# holds trial_ledger.py and packages.py, and Phase 8 reaching for the ledger
# from here should be a deliberate widening of this line rather than something
# that was always permitted.
BACKTEST_ALLOWED = frozenset(
    {
        "trading_house.core",
        "trading_house.marketdata",
        "trading_house.features",
        "trading_house.risk",
        "trading_house.research.backtest",
    }
)
RESEARCH_ROOT = SOURCE_ROOT / "research"
# An allowlist, not a denylist again: a research module that could reach
# brokers/, execution/ or agents/ could place an order, write to the live intent
# ledger or run agent-authored code while claiming to be analysis. Those three
# are examples; the rule is the set below, and widening it is then a decision
# somebody makes on purpose.
#
# All seven entries are granted by this phase's specification (the Phase 8A
# plan, Task 6 Step 2) rather than chosen from what the code happens to do today.
# ``database`` and ``strategies`` are in that list and no module under research/
# imports either one -- the ledger's ConnectionFactory is injected by cli.py and
# nothing here calls ``open_runtime_connection``; and nothing here compares a
# candidate against the strategy registry. They are reserved: a later phase that
# does need them widens nothing, and the import that eventually needs it is a use
# of an already-granted permission rather than a change to the boundary. Keeping
# an entry whose reach is not yet exercised is the point -- narrowing this set to
# the five packages currently imported would be the more informative guard and
# the wrong one, because it would fail a legitimate future module for importing
# something the specification already permits.
#
# research/backtest/ is covered by this loop too, and deliberately is not
# narrowed by it. BACKTEST_ALLOWED above is the stricter statement about that
# subtree and is still enforced on its own, so backtest/ ends up held to
# whatever the two agree on -- and the note on BACKTEST_ALLOWED that research/
# is admitted there at research.backtest rather than whole still stands. A
# widening of this envelope that was not meant as one therefore cannot relax the
# backtester, because that subtree is held by both.
RESEARCH_ALLOWED = frozenset(
    {
        "trading_house.core",
        "trading_house.database",
        "trading_house.features",
        "trading_house.marketdata",
        "trading_house.research",
        "trading_house.risk",
        "trading_house.strategies",
    }
)
STRATEGIES_ROOT = SOURCE_ROOT / "strategies"
STRATEGIES_ALLOWED = frozenset(
    {
        "trading_house.core",
        "trading_house.features",
        "trading_house.risk",
        "trading_house.strategies",
    }
)
EXECUTION_ROOT = SOURCE_ROOT / "execution"
# execution/ may reach core/ and database/, nothing else this project owns:
# it declares the venue port (VenueSubmitPort/DealSource/ProtectionPort) it
# needs and the MT5 adapter satisfies it structurally, so the ledger, order
# manager and position guard stay testable without MetaTrader5 installed.
# constitution/ joined the list with Phase 5: the guard's ProtectionPort needs
# the signed binding to turn a server symbol back into an instrument, and the
# obvious place to put that -- inside the daemon -- would have made the guard
# unconstructible without a signed file on disk. It lives in ops/ instead.
EXECUTION_FORBIDDEN = frozenset(
    {
        "trading_house.brokers",
        "trading_house.risk",
        "trading_house.marketdata",
        "trading_house.constitution",
    }
)
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


def _relative_imports(tree: ast.Module) -> set[str]:
    """Every ``from .foo import bar`` in one module -- the ``level > 0`` form
    this module's own ``_imported_modules`` and ``test_phase5.py``'s
    ``_project_imports`` are both blind to, since each requires
    ``node.level == 0`` (fix round 1, Finding 7). A relative import inside
    execution/ would clear ``EXECUTION_FORBIDDEN`` unseen by either. The repo
    uses none today; ``test_no_module_uses_a_relative_import`` below keeps
    that true instead of teaching both detectors to resolve one, which would
    be two independent resolutions that could drift out of sync with each
    other -- exactly the "less independent than they look" shape the finding
    describes."""

    return {
        f"{'.' * node.level}{node.module or ''}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level > 0
    }


def _reaches(tree: ast.Module, forbidden: frozenset[str]) -> set[str]:
    """Modules imported that are, or live under, a forbidden path."""

    return {
        module
        for module in _imported_modules(tree)
        if any(module == root or module.startswith(f"{root}.") for root in forbidden)
    }


def _reaches_outside(tree: ast.Module, allowed: frozenset[str]) -> set[str]:
    """Project modules imported that are not, and do not live under, an
    allowed path.

    Only ``trading_house`` modules are judged: the standard library, pydantic
    and typer are not this project's to partition. A bare ``import
    trading_house`` is flagged, because the package root is a door to every
    subpackage and naming it is not the same as naming one.
    """

    return {
        module
        for module in _imported_modules(tree)
        if (module == "trading_house" or module.startswith("trading_house."))
        and not any(module == root or module.startswith(f"{root}.") for root in allowed)
    }


def _referenced_attributes(tree: ast.Module) -> set[str]:
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def test_source_tree_is_not_empty() -> None:
    """A silent glob failure must not make every other guard vacuous."""

    assert len(_source_files()) >= 10


def test_no_module_uses_a_relative_import() -> None:
    """Closes the blind spot ``_relative_imports`` documents: neither this
    file's own import-boundary checks nor ``test_phase5.py``'s see a
    ``from .foo import bar``. Repo-wide, not just ``execution/``, because
    ``_project_imports`` in ``test_phase5.py`` shares the same gap and this
    is a strictly stronger guarantee that covers it too."""

    offenders: dict[str, list[str]] = {}
    for path, tree in _parsed():
        found = sorted(_relative_imports(tree))
        if found:
            offenders[path.relative_to(PROJECT_ROOT).as_posix()] = found

    assert offenders == {}


def test_the_relative_import_guard_can_still_fail() -> None:
    """Guard the guard: prove the detector flags a real relative import
    rather than a check that stopped looking."""

    assert _relative_imports(ast.parse("from ..brokers.mt5 import adapter\n")) == {"..brokers.mt5"}


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


def test_no_marketdata_module_imports_metatrader5() -> None:
    """I-17's precondition: marketdata/ reaches history only through the
    venue-neutral ``HistoryProvider`` seam (``provider.py``), never
    MetaTrader5 directly. Unlike the whole-tree guard above, this one has no
    exemption at all -- nothing under this package may import it."""

    offenders = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path, tree in _parsed()
        if path.is_relative_to(MARKETDATA_ROOT) and "MetaTrader5" in _imported_top_level(tree)
    ]

    assert offenders == []


def test_the_marketdata_import_guard_can_still_fail() -> None:
    """Guard the guard: with no exempted module to point at (the check above
    is a blanket ban, not an allow-list), prove instead that the detector it
    relies on -- ``_imported_top_level`` -- actually flags a real MetaTrader5
    import, so a passing check reflects marketdata/ staying clean rather than
    a detector that stopped looking."""

    tree = ast.parse("import MetaTrader5\n")

    assert "MetaTrader5" in _imported_top_level(tree)


def test_no_feature_module_imports_a_broker_or_risk_module() -> None:
    """features/ may only reach marketdata/ and core/. A feature that could
    fetch from a broker directly, or size a position itself, would let a
    caller route around the risk gate Section 8.2 owns."""

    offenders: dict[str, list[str]] = {}
    for path, tree in _parsed():
        if not path.is_relative_to(FEATURES_ROOT):
            continue
        reached = _reaches(tree, FEATURES_FORBIDDEN)
        if reached:
            offenders[path.relative_to(PROJECT_ROOT).as_posix()] = sorted(reached)

    assert offenders == {}


@pytest.mark.parametrize(
    "statement",
    [
        "from trading_house.brokers.base import BrokerAdapter",
        "import trading_house.risk",
        "from trading_house.risk.sizing import position_size",
    ],
)
def test_the_features_import_guard_can_still_fail(statement: str) -> None:
    """Guard the guard: prove the detector actually flags a real brokers/ or
    risk/ import, so a passing check reflects features/ staying clean rather
    than a detector that stopped looking."""

    assert _reaches(ast.parse(statement + "\n"), FEATURES_FORBIDDEN)


def test_no_risk_module_imports_a_broker_store_or_feature_module() -> None:
    """A backtest whose sizing diverges from production lies about expectancy,
    so the risk arithmetic must not be able to reach live infrastructure."""

    offenders: dict[str, list[str]] = {}
    for path, tree in _parsed():
        if not path.is_relative_to(RISK_ROOT):
            continue
        reached = _reaches(tree, RISK_FORBIDDEN)
        if reached:
            offenders[path.relative_to(PROJECT_ROOT).as_posix()] = sorted(reached)

    assert offenders == {}


def test_the_risk_package_is_not_empty() -> None:
    """Guard the guard: an empty risk/ would make the loop above pass
    vacuously, so a passing check reflects clean code rather than no code."""

    assert sorted(RISK_ROOT.rglob("*.py"))


@pytest.mark.parametrize(
    "statement",
    [
        "from trading_house.brokers.mt5.gateway import Gateway",
        "import trading_house.marketdata",
        "from trading_house.features.engine import FeatureEngine",
    ],
)
def test_the_risk_import_guard_can_still_fail(statement: str) -> None:
    """Guard the guard: prove the detector flags a real import, so a passing
    check reflects risk/ staying clean rather than a detector gone blind."""

    assert _reaches(ast.parse(statement + "\n"), RISK_FORBIDDEN)


def test_no_execution_module_imports_a_broker_risk_or_marketdata_module() -> None:
    """execution/ imports core/ and database/ only. Reaching brokers/ directly
    would make the order manager and reconciler untestable without
    MetaTrader5 installed; reaching risk/ or marketdata/ has no reason to
    exist on the order-submission path at all."""

    offenders: dict[str, list[str]] = {}
    for path, tree in _parsed():
        if not path.is_relative_to(EXECUTION_ROOT):
            continue
        reached = _reaches(tree, EXECUTION_FORBIDDEN)
        if reached:
            offenders[path.relative_to(PROJECT_ROOT).as_posix()] = sorted(reached)

    assert offenders == {}


def test_the_execution_package_is_not_empty() -> None:
    """Guard the guard: an empty execution/ would make the loop above pass
    vacuously, so a passing check reflects clean code rather than no code."""

    assert sorted(EXECUTION_ROOT.rglob("*.py"))


@pytest.mark.parametrize(
    "statement",
    [
        "from trading_house.brokers.mt5.adapter import Mt5BrokerAdapter",
        "import trading_house.risk",
        "from trading_house.marketdata.store import PostgresBarStore",
        "from trading_house.constitution.binding import VenueBinding",
    ],
)
def test_the_execution_import_guard_can_still_fail(statement: str) -> None:
    """Guard the guard: prove the detector flags a real import, so a passing
    check reflects execution/ staying clean rather than a detector gone
    blind."""

    assert _reaches(ast.parse(statement + "\n"), EXECUTION_FORBIDDEN)


def test_no_backtest_module_imports_outside_its_allowlist() -> None:
    """research/backtest/ may reach core/, marketdata/, features/ and risk/,
    and nothing else this project owns.

    Stated as an allowlist because that is how spec section 4 states it. A
    broker import would make a replay depend on a running terminal and give
    the simulator a way to place a real order; an execution import would give
    it the order manager and the intent ledger, so a "backtest" could write to
    the live plane. But those are two examples, not the rule -- and the rule is
    what a simulator whose whole job is to replay stored bars may touch.
    Widening this set is then a decision somebody makes on purpose, which is
    exactly what the spec says adding strategies/ later should be.
    """

    offenders: dict[str, list[str]] = {}
    for path, tree in _parsed():
        if not path.is_relative_to(BACKTEST_ROOT):
            continue
        reached = _reaches_outside(tree, BACKTEST_ALLOWED)
        if reached:
            offenders[path.relative_to(PROJECT_ROOT).as_posix()] = sorted(reached)

    assert offenders == {}


def test_the_backtest_package_is_not_empty() -> None:
    """Guard the guard: an empty research/backtest/ would make the loop above
    pass vacuously, so a passing check reflects clean code rather than no code."""

    assert sorted(BACKTEST_ROOT.rglob("*.py"))


@pytest.mark.parametrize(
    "statement",
    [
        "from trading_house.execution import loop",
        "from trading_house.execution.manager import OrderManager",
        "import trading_house.brokers",
        "from trading_house.brokers.mt5.adapter import Mt5BrokerAdapter",
        # The four the denylist this replaced could not see. Every one of them
        # passed the old guard while contradicting the sentence its own
        # docstring quoted from the spec.
        "from trading_house.database.connection import open_runtime_connection",
        "from trading_house.audit.repository import PostgresAuditLedger",
        "from trading_house.ops.backtest import build_backtester",
        "import trading_house",
    ],
)
def test_the_backtest_import_guard_can_still_fail(statement: str) -> None:
    """Guard the guard: prove the detector flags a real import, so a passing
    check reflects research/backtest/ staying clean rather than a detector
    gone blind."""

    assert _reaches_outside(ast.parse(statement + "\n"), BACKTEST_ALLOWED)


@pytest.mark.parametrize(
    "statement",
    [
        "from trading_house.core.schemas import Side",
        "from trading_house.marketdata.models import Bar",
        "from trading_house.features.engine import FeatureEngine",
        "from trading_house.risk.engine import RiskEngine",
        "from trading_house.research.backtest.costs import CostModel",
        "import json",
        "from pydantic import Field",
    ],
)
def test_the_backtest_import_guard_permits_what_the_spec_permits(statement: str) -> None:
    """Guard the guard, the other direction -- which only an allowlist needs.

    A detector that flagged everything would satisfy the case above while
    failing the real loop, and the four denylist arrows next door cannot go
    wrong this way because a denylist that over-matches simply never fires.
    This pins the exact set section 4 grants, so narrowing it is as visible as
    widening it.
    """

    assert _reaches_outside(ast.parse(statement + "\n"), BACKTEST_ALLOWED) == set()


def test_no_research_module_imports_outside_its_allowlist() -> None:
    """research/ may reach core/, database/, features/, marketdata/, its own
    modules, risk/ and strategies/ -- and nothing else this project owns.

    The ledger and the evidence store are the reason this envelope is wider than
    the backtester's: sealing a trial needs the same PostgreSQL and the same
    stored bars the run was built from, and comparing a candidate against the
    registry's strategies is what a research module is for. The boundary that
    has to hold is the one at brokers/, execution/ and agents/: a research
    process that could send an order, write to the live intent ledger, or run
    agent-authored code would not be research, and the phrase "it is only
    analysis" is exactly the claim that must be made unfalsifiable by the
    import graph rather than trusted.
    """

    offenders: dict[str, list[str]] = {}
    for path, tree in _parsed():
        if not path.is_relative_to(RESEARCH_ROOT):
            continue
        reached = _reaches_outside(tree, RESEARCH_ALLOWED)
        if reached:
            offenders[path.relative_to(PROJECT_ROOT).as_posix()] = sorted(reached)

    assert offenders == {}


def test_the_research_package_is_not_empty() -> None:
    """Guard the guard: an empty research/ would make the loop above pass
    vacuously, so a passing check reflects clean code rather than no code."""

    assert sorted(RESEARCH_ROOT.rglob("*.py"))


@pytest.mark.parametrize(
    "statement",
    [
        "from trading_house.brokers.mt5.gateway import Mt5Gateway",
        "import trading_house.brokers",
        "from trading_house.execution.manager import OrderManager",
        "from trading_house.agents.providers.base import AgentRun",
        "from trading_house.constitution.signing import sign_bytes",
        "import trading_house",
    ],
)
def test_the_research_import_guard_can_still_fail(statement: str) -> None:
    """Guard the guard: prove the detector flags a real import, so a passing
    check reflects research/ staying clean rather than a detector gone blind."""

    assert _reaches_outside(ast.parse(statement + "\n"), RESEARCH_ALLOWED)


@pytest.mark.parametrize(
    "statement",
    [
        "from trading_house.research.backtest.result import BacktestResult",
        "from trading_house.database.connection import open_runtime_connection",
        "from trading_house.core.errors import ConfigurationError",
        "from trading_house.marketdata.models import Bar",
        "from trading_house.features.sessions import Session",
        "from trading_house.risk.engine import RiskEngine",
        "from trading_house.strategies.spec import StrategySpec",
        "import json",
    ],
)
def test_the_research_import_guard_permits_what_the_spec_permits(statement: str) -> None:
    """Guard the guard, the other direction -- which only an allowlist needs.

    Pins the exact set the specification grants, so narrowing it is as visible
    as widening it. ``database`` and ``strategies`` are pinned here even though
    no module under research/ imports either: they are permitted, not required,
    and a positive case for each is what lets a future phase drop one by deleting
    a line, rather than by discovering afterwards which positive case had been
    load-bearing.
    """

    assert _reaches_outside(ast.parse(statement + "\n"), RESEARCH_ALLOWED) == set()


def test_the_backtest_boundary_is_not_widened_by_the_research_one() -> None:
    """Guard the guard, across the two guards.

    research/ is the parent of research/backtest/, so this loop reads the
    backtester's files against a set that names more roots than the backtester's
    own. A widening of this set that nobody meant as one would then relax the
    backtest boundary, if the backtest guard were not also running. Four
    assertions pin exactly that, and each says one thing:

    * ``BACKTEST_ROOT.is_relative_to(RESEARCH_ROOT)`` -- both loops really do
      reach the same files, so the composition below is not vacuous.
    * ``BACKTEST_ALLOWED - RESEARCH_ALLOWED`` is at most the single entry
      ``trading_house.research.backtest``. That is the one name the backtester
      may hold without the research set naming it, and only because
      ``trading_house.research`` already covers it by prefix. Any other
      difference would be a root this guard does not cover at all.
    * ``database`` and ``strategies`` are absent from ``BACKTEST_ALLOWED``. The
      research set grants them; the backtest set does not, and the two reserved
      entries must not be usable to undo that refusal from outside.
    * Taken together: every root the backtester permits is one the research
      guard also permits, so for the shared subtree the effective boundary is
      ``BACKTEST_ALLOWED`` -- the stricter of the two -- and no change confined
      to ``RESEARCH_ALLOWED`` can widen it.
    """

    assert BACKTEST_ROOT.is_relative_to(RESEARCH_ROOT)
    # Stated as the difference rather than as an ordering: the backtest set may
    # name ``trading_house.research.backtest`` only because it is *narrower* than
    # the research set's ``trading_house.research``, and that one entry is the
    # whole of what it adds.
    backtest_only = BACKTEST_ALLOWED - RESEARCH_ALLOWED
    assert backtest_only <= {"trading_house.research.backtest"}
    assert "trading_house.database" not in BACKTEST_ALLOWED
    assert "trading_house.strategies" not in BACKTEST_ALLOWED


def test_no_strategy_module_imports_outside_its_allowlist() -> None:
    offenders: dict[str, list[str]] = {}
    for path, tree in _parsed():
        if not path.is_relative_to(STRATEGIES_ROOT):
            continue
        reached = _reaches_outside(tree, STRATEGIES_ALLOWED)
        if reached:
            offenders[path.relative_to(PROJECT_ROOT).as_posix()] = sorted(reached)

    assert offenders == {}


def test_the_strategy_package_is_not_empty() -> None:
    assert sorted(STRATEGIES_ROOT.rglob("*.py"))


@pytest.mark.parametrize(
    "statement",
    [
        "from trading_house.research.backtest.strategy import Strategy",
        "from trading_house.marketdata.models import Timeframe",
        "import trading_house.brokers",
    ],
)
def test_the_strategy_import_guard_can_still_fail(statement: str) -> None:
    assert _reaches_outside(ast.parse(statement + "\n"), STRATEGIES_ALLOWED)


@pytest.mark.parametrize(
    "statement",
    [
        "from trading_house.core.snapshot import FeatureSnapshot",
        "from trading_house.features.sessions import Session",
        "from trading_house.risk.engine import RiskEngine",
        "from trading_house.strategies.spec import StrategySpec",
        "import json",
    ],
)
def test_the_strategy_import_guard_permits_what_the_spec_permits(statement: str) -> None:
    assert _reaches_outside(ast.parse(statement + "\n"), STRATEGIES_ALLOWED) == set()


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
