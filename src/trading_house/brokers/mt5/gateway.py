"""A single-threaded actor owning every MetaTrader 5 call.

The MetaTrader 5 Python API is not thread-safe and its calls block, so exactly
one thread touches it. Work is prioritised rather than FIFO: a protection call
must never sit behind a queue of market-data polls.

A hard demo check runs inside ``start()`` before any request is served, so
there is no state in which the gateway is connected to a live account and
merely not trading yet.

Known limitation: ``mt5.*`` calls are blocking C calls. If one hangs, Python
cannot kill it and the actor thread is stuck. Callers time out, the heartbeat
stops advancing, and health reports unhealthy — but in-process recovery is not
possible. Real isolation needs a separate process.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from itertools import count
from types import TracebackType
from typing import Any, Self, TypeVar, cast

from trading_house.brokers.mt5.boundary import ACCOUNT_TRADE_MODE_DEMO, TerminalPort
from trading_house.core.clock import Clock
from trading_house.core.errors import BrokerUnavailableError, NonDemoAccountError

T = TypeVar("T")

_SHUTDOWN_TIMEOUT_SECONDS = 5.0


class Priority(IntEnum):
    """Lower runs first. Protection must never queue behind a data poll."""

    PROTECTION = 0
    ORDER = 1
    RECONCILE = 2
    MARKET_DATA = 3


@dataclass(frozen=True, slots=True)
class GatewayMetrics:
    """Operational data about the actor, not venue-neutral fact."""

    queue_depth: Mapping[Priority, int]
    served: int
    failed: int
    last_heartbeat: datetime
    stale: bool


@dataclass(slots=True)
class _Request:
    operation: Callable[[TerminalPort], Any]
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None


class Mt5Gateway:
    """Serialise every terminal call through one prioritised thread."""

    def __init__(
        self,
        terminal: TerminalPort,
        *,
        clock: Clock,
        request_timeout_seconds: float = 10.0,
    ) -> None:
        self._terminal = terminal
        self._clock = clock
        self._timeout = request_timeout_seconds
        self._queue: queue.PriorityQueue[tuple[int, int, _Request]] = queue.PriorityQueue()
        self._sequence = count()
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._served = 0
        self._failed = 0
        self._depth: dict[Priority, int] = dict.fromkeys(Priority, 0)
        self._heartbeat = clock.now()
        self._server_utc_offset_seconds = 0
        self._stale = True
        self._started = False

    def start(self) -> None:
        """Connect, refuse a non-demo account, then begin serving."""

        if not self._terminal.initialize():
            self._terminal.shutdown()
            raise BrokerUnavailableError()
        try:
            if self._terminal.account_trade_mode() != ACCOUNT_TRADE_MODE_DEMO:
                raise NonDemoAccountError()
            self._server_utc_offset_seconds = self._terminal.server_utc_offset_seconds()
        except BaseException:
            self._terminal.shutdown()
            raise
        self._stopping.clear()
        self._thread = threading.Thread(target=self._serve, name="mt5-gateway", daemon=True)
        self._thread.start()
        self._started = True

    def stop(self) -> None:
        self._stopping.set()
        thread = self._thread
        if thread is not None:
            thread.join(_SHUTDOWN_TIMEOUT_SECONDS)
        self._thread = None
        self._drain()
        if self._started:
            self._started = False
            self._terminal.shutdown()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.stop()

    def call(self, priority: Priority, operation: Callable[[TerminalPort], T]) -> T:
        """Run one operation on the actor thread and return its result."""

        if self._thread is None or not self._thread.is_alive():
            raise BrokerUnavailableError()
        request = _Request(operation=operation)
        with self._lock:
            self._depth[priority] += 1
        self._queue.put((int(priority), next(self._sequence), request))
        if not request.done.wait(self._timeout):
            raise BrokerUnavailableError()
        if request.error is not None:
            raise request.error
        return cast("T", request.result)

    def metrics(self) -> GatewayMetrics:
        with self._lock:
            return GatewayMetrics(
                queue_depth=dict(self._depth),
                served=self._served,
                failed=self._failed,
                last_heartbeat=self._heartbeat,
                stale=self._stale,
            )

    def mark_stale(self) -> None:
        """Record that gateway state can no longer be trusted."""

        with self._lock:
            self._stale = True

    def mark_reconciled(self) -> None:
        """Clear staleness. Only a successful reconcile may call this."""

        with self._lock:
            self._stale = False

    @property
    def server_utc_offset_seconds(self) -> int:
        return self._server_utc_offset_seconds

    def _serve(self) -> None:
        while not self._stopping.is_set():
            try:
                priority_value, _, request = self._queue.get(timeout=0.1)
            except queue.Empty:
                with self._lock:
                    self._heartbeat = self._clock.now()
                continue
            try:
                request.result = request.operation(self._terminal)
            except BaseException as error:  # relayed to the calling thread
                request.error = error
            finally:
                with self._lock:
                    self._depth[Priority(priority_value)] -= 1
                    self._heartbeat = self._clock.now()
                    if request.error is None:
                        self._served += 1
                    else:
                        self._failed += 1
                        self._stale = True
                request.done.set()

    def _drain(self) -> None:
        """Fail every request the actor never reached.

        Without this a caller waits out the full request timeout for an
        answer that is already known to be unavailable, and its queue-depth
        entry never comes back.
        """

        while True:
            try:
                priority_value, _, request = self._queue.get_nowait()
            except queue.Empty:
                return
            request.error = BrokerUnavailableError()
            with self._lock:
                self._depth[Priority(priority_value)] -= 1
                self._failed += 1
                self._stale = True
            request.done.set()
