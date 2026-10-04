import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import pytest
import typer.rich_utils
from alembic import command
from alembic.config import Config
from hypothesis import HealthCheck, settings
from psycopg import sql
from sqlalchemy import URL
from testcontainers.community.postgres import PostgresContainer

MIGRATION_PASSWORD = "integration-migration-password"  # noqa: S105
RUNTIME_PASSWORD = "integration-runtime-password"  # noqa: S105
TEST_SUPERUSER_PASSWORD = "integration-test-superuser-password"  # noqa: S105
# Phase 8A gave the trial ledger its own database, DSN and migration target
# rather than another schema in the application one. What that buys is
# operational separation: the ledger can be backed up, restored, migrated or
# dropped on its own schedule, and no migration of the live application schema
# can disturb it. It is not a read boundary -- the same
# ``trading_house_runtime`` role holds SELECT on the ledger in both databases,
# because 8A grants it, so the privilege that keeps the chain append-only is the
# absence of INSERT rather than which database the rows live in.
RESEARCH_DATABASE = "trading_house_research"

# Hypothesis's 200 ms per-example deadline and its ``too_slow`` health check
# time the machine, not the property. On a loaded Windows box a full run failed
# three property tests that pass in isolation. Reproduced under load: the
# mutated-YAML constitution test with ``DeadlineExceeded`` (a 30-80 ms parse
# took 260-300 ms) and the mark identity test with ``FailedHealthCheck``
# (nine inputs in a second). The third, the constitution drift test, did not
# fail at 200 ms here, but the deadline is the only part of it that depends on
# the clock. So both are off for the whole suite rather than test by test, as
# ``tests/property/test_promotion.py`` already did for itself. Neither setting
# weakens an assertion.
settings.register_profile(
    "trading_house", deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.load_profile("trading_house")

# Typer forces Rich's terminal mode whenever GITHUB_ACTIONS is set, which puts
# ANSI styling inside every option name in ``--help`` output: a substring
# assertion then fails in CI, and a ``not in`` one passes without checking
# anything. Typer reads this at render time, so import order does not matter.
typer.rich_utils.FORCE_TERMINAL = False


def printed_strings(*streams: str) -> str:
    """What a command actually printed, with both layers of escaping undone.

    Searching a raw stream for a leaked Windows path is escape-blind, and
    twice over. ``OSError.__str__`` reprs the filename, which doubles every
    backslash; ``json.dumps`` then doubles them again on the wire, and a
    nested object rendered through ``str()`` doubles them a further time. So a
    ``FileNotFoundError`` that prints the operator's whole path, or a payload
    that carries one, arrives with four backslashes per separator and matches
    no path any test holds. ``json.loads`` undoes the outer layer and
    collapsing the doubles undoes the inner one. On POSIX every spelling
    already agrees, which is exactly how a check that asserts nothing here
    would have shipped unnoticed.

    Shared rather than duplicated. ``tests/unit/test_cli.py`` grew this in the
    previous fix round; ``tests/integration/research`` asserted against the
    raw stream and so could not fail under any production change on Windows --
    in the file that is this phase's reproducibility proof. Two copies would
    be two things to keep in step, and the copy that drifts is the one nobody
    is looking at.

    Two narrow false negatives are left standing rather than coded around: a
    leak escaped a third time collapses to two backslashes and stops matching,
    and a path on a UNC share would have its own leading ``\\\\`` collapsed.
    Neither is reachable in this repository's layout. What is not left
    standing is the empty case -- a command that exits without printing would
    satisfy every ``not in`` at the call site while proving nothing, so this
    refuses to return nothing at all.
    """

    printed = " ".join(
        str(value) for stream in streams if stream.strip() for value in json.loads(stream).values()
    )
    assert printed, "the command printed nothing, so a leak assertion would be vacuous"
    return printed.replace("\\\\", "\\")


@dataclass(frozen=True)
class DatabaseHarness:
    admin_dsn: str = field(repr=False)
    migration_dsn: str = field(repr=False)
    runtime_dsn: str = field(repr=False)
    test_superuser_dsn: str = field(repr=False)
    alembic_config: Config
    research_migration_dsn: str = field(repr=False)
    research_runtime_dsn: str = field(repr=False)
    research_alembic_config: Config


def _dsn(
    container: PostgresContainer, *, user: str, password: str, dbname: str | None = None
) -> str:
    return psycopg.conninfo.make_conninfo(
        host=container.get_container_host_ip(),
        port=container.get_exposed_port(container.port),
        dbname=dbname if dbname is not None else container.dbname,
        user=user,
        password=password,
    )


def _migration_url(container: PostgresContainer, *, dbname: str | None = None) -> str:
    url = URL.create(
        "postgresql+psycopg",
        username="trading_house_migrator",
        password=MIGRATION_PASSWORD,
        host=container.get_container_host_ip(),
        port=int(container.get_exposed_port(container.port)),
        database=dbname if dbname is not None else container.dbname,
    )
    return url.render_as_string(hide_password=False)


def _bootstrap_roles(container: PostgresContainer, admin_dsn: str) -> None:
    with (
        psycopg.connect(admin_dsn, autocommit=True) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute("CREATE ROLE trading_house_owner NOLOGIN")
        cursor.execute(
            sql.SQL("CREATE ROLE trading_house_migrator LOGIN PASSWORD {}").format(
                sql.Literal(MIGRATION_PASSWORD)
            )
        )
        cursor.execute("GRANT trading_house_owner TO trading_house_migrator")
        cursor.execute(
            sql.SQL("CREATE ROLE trading_house_runtime LOGIN PASSWORD {}").format(
                sql.Literal(RUNTIME_PASSWORD)
            )
        )
        cursor.execute(
            sql.SQL("CREATE ROLE trading_house_test_superuser LOGIN SUPERUSER PASSWORD {}").format(
                sql.Literal(TEST_SUPERUSER_PASSWORD)
            )
        )
        cursor.execute(
            sql.SQL("GRANT CONNECT, CREATE ON DATABASE {} TO trading_house_owner").format(
                sql.Identifier(container.dbname)
            )
        )
        cursor.execute("GRANT CREATE ON SCHEMA public TO trading_house_owner")
        cursor.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO trading_house_runtime").format(
                sql.Identifier(container.dbname)
            )
        )
        cursor.execute(
            sql.SQL("ALTER DATABASE {} SET TIME ZONE 'UTC'").format(
                sql.Identifier(container.dbname)
            )
        )

        cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(RESEARCH_DATABASE)))
        cursor.execute(
            sql.SQL("GRANT CONNECT, CREATE ON DATABASE {} TO trading_house_owner").format(
                sql.Identifier(RESEARCH_DATABASE)
            )
        )
        cursor.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO trading_house_runtime").format(
                sql.Identifier(RESEARCH_DATABASE)
            )
        )
        # Explicit rather than inherited from the default PUBLIC grant on a
        # database: this role runs the migrations here, and an environment that
        # tightened that default would otherwise break the suite's setup with an
        # error that reads like a migration failure.
        cursor.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO trading_house_migrator").format(
                sql.Identifier(RESEARCH_DATABASE)
            )
        )
        cursor.execute(
            sql.SQL("ALTER DATABASE {} SET TIME ZONE 'UTC'").format(
                sql.Identifier(RESEARCH_DATABASE)
            )
        )

    # alembic's own version table lands in `public`, and `public` in a database
    # whose owner is the container superuser grants nothing to the roles this
    # suite creates. The application database gets the same grant above, against
    # a connection to itself; this is that line again for the second database,
    # without which `upgrade` cannot create alembic_version there at all.
    with (
        psycopg.connect(
            _dsn(
                container,
                user=container.username,
                password=container.password,
                dbname=RESEARCH_DATABASE,
            ),
            autocommit=True,
        ) as research_connection,
        research_connection.cursor() as research_cursor,
    ):
        research_cursor.execute("GRANT CREATE ON SCHEMA public TO trading_house_owner")


@pytest.fixture(scope="session")
def database() -> Iterator[DatabaseHarness]:
    project_root = Path(__file__).resolve().parents[1]
    with PostgresContainer(
        "postgres:18-alpine",
        username="postgres",
        password="integration-admin-password",  # noqa: S106
        dbname="trading_house",
        driver="psycopg",
    ).with_env("TZ", "UTC") as container:
        admin_dsn = _dsn(container, user=container.username, password=container.password)
        _bootstrap_roles(container, admin_dsn)

        alembic_config = Config(str(project_root / "alembic.ini"))
        alembic_config.set_main_option(
            "sqlalchemy.url", _migration_url(container).replace("%", "%%")
        )
        research_alembic_config = Config(str(project_root / "alembic.ini"))
        research_alembic_config.set_main_option(
            "sqlalchemy.url",
            _migration_url(container, dbname=RESEARCH_DATABASE).replace("%", "%%"),
        )
        # Both databases run the same migration history: the research database
        # is a second *deployment target* for this repository's schema, not a
        # schema of its own, so it gets the audit schema and the extension
        # ``0007`` hashes with, and the same 0007 at the same revision.
        command.upgrade(alembic_config, "head")
        command.upgrade(research_alembic_config, "head")

        yield DatabaseHarness(
            admin_dsn=admin_dsn,
            migration_dsn=_dsn(
                container,
                user="trading_house_migrator",
                password=MIGRATION_PASSWORD,
            ),
            runtime_dsn=_dsn(
                container,
                user="trading_house_runtime",
                password=RUNTIME_PASSWORD,
            ),
            test_superuser_dsn=_dsn(
                container,
                user="trading_house_test_superuser",
                password=TEST_SUPERUSER_PASSWORD,
            ),
            alembic_config=alembic_config,
            research_migration_dsn=_dsn(
                container,
                user="trading_house_migrator",
                password=MIGRATION_PASSWORD,
                dbname=RESEARCH_DATABASE,
            ),
            research_runtime_dsn=_dsn(
                container,
                user="trading_house_runtime",
                password=RUNTIME_PASSWORD,
                dbname=RESEARCH_DATABASE,
            ),
            research_alembic_config=research_alembic_config,
        )


@pytest.fixture
def isolated_audit_ledger(database: DatabaseHarness) -> Iterator[None]:
    """Give one test a fresh ledger without bypassing append-only protections."""

    command.downgrade(database.alembic_config, "base")
    command.upgrade(database.alembic_config, "head")
    try:
        yield
    finally:
        command.downgrade(database.alembic_config, "base")
        command.upgrade(database.alembic_config, "head")


@pytest.fixture
def isolated_research_ledger(database: DatabaseHarness) -> Iterator[None]:
    """Give one test a fresh trial ledger, in the research database only.

    The application database is never reset for a research test: a phase 8 test
    that could silently empty `trading_house` would be able to hide a real
    regression in every other suite that runs after it.
    """

    command.downgrade(database.research_alembic_config, "base")
    command.upgrade(database.research_alembic_config, "head")
    try:
        yield
    finally:
        command.downgrade(database.research_alembic_config, "base")
        command.upgrade(database.research_alembic_config, "head")


@pytest.fixture
def research_ledger_dsn(database: DatabaseHarness) -> str:
    return database.research_runtime_dsn


@pytest.fixture
def research_migration_dsn(database: DatabaseHarness) -> str:
    return database.research_migration_dsn


@pytest.fixture
def research_evidence_root(tmp_path: Path) -> Path:
    return tmp_path / "evidence"
