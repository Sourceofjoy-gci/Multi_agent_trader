"""Read-only checks for the database migration revision."""

from typing import Any

import psycopg
from alembic.config import Config
from alembic.script import ScriptDirectory

from trading_house.core.errors import MigrationMismatchError


def assert_at_head(connection: psycopg.Connection[tuple[Any, ...]], alembic_config: Config) -> None:
    """Fail closed unless exactly one database revision equals the configured head."""

    try:
        expected_head = ScriptDirectory.from_config(alembic_config).get_current_head()
        with connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM public.alembic_version")
            revisions = cursor.fetchall()
    except Exception as error:
        raise MigrationMismatchError() from error

    if expected_head is None or revisions != [(expected_head,)]:
        raise MigrationMismatchError()
