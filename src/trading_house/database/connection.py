"""Fail-closed PostgreSQL runtime connection setup."""

from contextlib import suppress
from typing import Any

import psycopg
from pydantic import SecretStr

from trading_house.core.errors import DatabaseUnavailableError


class _DatabaseDriverFailure(Exception):
    """Credential-free diagnostic cause for a failed driver operation."""


def open_runtime_connection(dsn: SecretStr) -> psycopg.Connection[tuple[Any, ...]]:
    """Connect with a UTC session without retaining plaintext DSN material."""

    connection: psycopg.Connection[tuple[Any, ...]] | None = None
    try:
        connection = psycopg.connect(dsn.get_secret_value())
        with connection.cursor() as cursor:
            cursor.execute("SET TIME ZONE 'UTC'")
            cursor.execute("SHOW TIME ZONE")
            row = cursor.fetchone()
            if row != ("UTC",):
                raise RuntimeError("database did not enter UTC session state")
        connection.commit()
        return connection
    except Exception:
        if connection is not None:
            with suppress(Exception):
                connection.close()

    raise DatabaseUnavailableError() from _DatabaseDriverFailure("database driver operation failed")
