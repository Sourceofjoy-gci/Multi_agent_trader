"""The guard's memory: an append-only record of what we believe about each
position, and the query that gives ``PositionGuard`` its working set.

Append-only is enforced by grant and trigger (migration 0006), the same
two-layer discipline ``ledger.py`` uses and for the same reason -- "we never
call UPDATE" is not evidence, a database that refuses the statement is.

Row shape (binding, from ``execution/loop.py``'s ``PositionStore`` docstring):
``latest()`` and each element of ``open_positions()`` return exactly the
last-appended payload's own fields, plus ``position_ticket`` and
``lifecycle``, and nothing structural -- no ``seq``, ``event_time`` or
``recorded_at``. The loop carries a fetched row forward verbatim into its next
write, so any extra structural column would get copied into every later
payload, stringified, and grow without bound. The two structural keys are
layered on top of the payload on the way out (see ``_flatten``) so a stray
same-named payload key can never shadow them.

There is deliberately no ``PositionEvent`` dataclass here (YAGNI): the guard
consumes plain mappings throughout (``record.get(...)``, ``row["..."]``), and
a frozen dataclass would typecheck in isolation while breaking the moment the
composition root wires the two together.

This module intentionally has no runtime dependency on ``execution/loop.py``
-- its only link to the guard's port is the type-only conformance pin at the
bottom of this file, checked by mypy and erased at runtime.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Never

import psycopg
from psycopg.types.json import Jsonb

from trading_house.core.errors import DatabaseUnavailableError
from trading_house.execution.ledger import ConnectionFactory

if TYPE_CHECKING:
    from trading_house.execution.loop import PositionStore as _GuardPositionStore

# The daemon's own reserved row (``loop.py``'s ``SYSTEM_TICKET``) is never a
# position, so it must not resurface here -- a restart would otherwise read a
# shutdown/escalation row back as an open position (Addition 3 of the task-2
# supplement). Position tickets are always positive; 0 is reserved for that.
_SYSTEM_TICKET = 0

_APPEND_SQL = """
INSERT INTO execution.position_events (position_ticket, lifecycle, event_time, payload)
VALUES (%s, %s, %s, %s)
"""

_LATEST_SQL = """
SELECT position_ticket, lifecycle, payload
FROM execution.position_events
WHERE position_ticket = %s
ORDER BY seq DESC
LIMIT 1
"""

# The CLOSED filter is applied in the OUTER query, after DISTINCT ON has
# already picked each ticket's single latest row. Filtering `lifecycle <>
# 'CLOSED'` on the raw rows first (a plain WHERE before the DISTINCT ON) would
# throw away a ticket's later CLOSED row while leaving its earlier OPEN row
# eligible to win the DISTINCT ON -- resurfacing a closed position forever.
# The ticket exclusion has no such ordering hazard (a ticket's identity never
# changes across its rows), so it stays in the inner query for one less row
# for DISTINCT ON to sort.
_OPEN_POSITIONS_SQL = """
SELECT position_ticket, lifecycle, payload FROM (
    SELECT DISTINCT ON (position_ticket) position_ticket, lifecycle, payload
    FROM execution.position_events
    WHERE position_ticket <> %s
    ORDER BY position_ticket, seq DESC
) latest
WHERE lifecycle <> 'CLOSED'
"""


class _PositionStoreFailure(Exception):
    """Credential- and row-free diagnostic cause for a failed store operation."""


def _raise_database_unavailable() -> Never:
    raise DatabaseUnavailableError() from _PositionStoreFailure("position store operation failed")


def _payload_dumps(payload: Any) -> str:
    """Serialize a payload with every non-native value (notably ``Decimal``)
    turned into a string rather than silently coerced into a JSON float."""

    return json.dumps(payload, default=str)


def _flatten(position_ticket: int, lifecycle: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """The payload's own fields with the two structural columns layered on
    top, so a stray ``payload["lifecycle"]`` or ``payload["position_ticket"]``
    can never shadow the real column."""

    return {**payload, "position_ticket": position_ticket, "lifecycle": lifecycle}


class PostgresPositionStore:
    """Append-only access to the position event store."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def _connect(self) -> psycopg.Connection[tuple[Any, ...]]:
        try:
            return self._connection_factory()
        except DatabaseUnavailableError:
            raise
        except Exception:
            _raise_database_unavailable()

    def append(
        self,
        position_ticket: int,
        lifecycle: str,
        event_time: datetime,
        payload: Mapping[str, str],
    ) -> None:
        """Insert one event and commit before returning.

        The commit is explicit, not left to an implicit ``with connection:``
        exit -- same reasoning as ``ledger.py``'s ``append``: this is a write
        the caller relies on being durable before its next line runs.
        """

        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    _APPEND_SQL,
                    (
                        position_ticket,
                        lifecycle,
                        event_time,
                        Jsonb(dict(payload), dumps=_payload_dumps),
                    ),
                )
            connection.commit()
        finally:
            connection.close()

    def latest(self, position_ticket: int) -> Mapping[str, Any] | None:
        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(_LATEST_SQL, (position_ticket,))
                row = cursor.fetchone()
        finally:
            connection.close()
        return None if row is None else _flatten(row[0], row[1], row[2])

    def open_positions(self) -> Sequence[Mapping[str, Any]]:
        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(_OPEN_POSITIONS_SQL, (_SYSTEM_TICKET,))
                rows = cursor.fetchall()
        finally:
            connection.close()
        return tuple(_flatten(row[0], row[1], row[2]) for row in rows)


if TYPE_CHECKING:  # pragma: no cover

    def _conforms_to_the_guards_port(store: PostgresPositionStore) -> _GuardPositionStore:
        """Structural conformance, checked by mypy and erased at runtime.

        ``loop.PositionStore`` is a Protocol rather than a base class (it was
        declared before this module existed), so nothing else would catch a
        signature drifting out of shape until the composition root wired
        them together.
        """
        return store
