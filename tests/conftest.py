import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import URL
from testcontainers.community.postgres import PostgresContainer

MIGRATION_PASSWORD = "integration-migration-password"  # noqa: S105
RUNTIME_PASSWORD = "integration-runtime-password"  # noqa: S105
TEST_SUPERUSER_PASSWORD = "integration-test-superuser-password"  # noqa: S105


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


def _dsn(container: PostgresContainer, *, user: str, password: str) -> str:
    return psycopg.conninfo.make_conninfo(
        host=container.get_container_host_ip(),
        port=container.get_exposed_port(container.port),
        dbname=container.dbname,
        user=user,
        password=password,
    )


def _migration_url(container: PostgresContainer) -> str:
    url = URL.create(
        "postgresql+psycopg",
        username="trading_house_migrator",
        password=MIGRATION_PASSWORD,
        host=container.get_container_host_ip(),
        port=int(container.get_exposed_port(container.port)),
        database=container.dbname,
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
        command.upgrade(alembic_config, "head")

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
