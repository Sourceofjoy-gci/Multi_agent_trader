import threading
import time
from collections.abc import Mapping
from datetime import UTC

import pytest

from tests.unit.brokers.mt5.conftest import FakeTerminal
from trading_house.brokers.mt5.boundary import ACCOUNT_TRADE_MODE_REAL
from trading_house.brokers.mt5.gateway import Mt5Gateway, Priority
from trading_house.core.clock import SystemClock
from trading_house.core.errors import BrokerUnavailableError, NonDemoAccountError


def _gateway(terminal: FakeTerminal) -> Mt5Gateway:
    return Mt5Gateway(terminal, clock=SystemClock(), request_timeout_seconds=5.0)


def test_a_non_demo_account_is_refused_before_any_request_is_served() -> None:
    """There must be no state in which the gateway is connected to a live
    account and merely not trading yet."""

    terminal = FakeTerminal(trade_mode=ACCOUNT_TRADE_MODE_REAL)
    gateway = _gateway(terminal)

    with pytest.raises(NonDemoAccountError):
        gateway.start()

    assert terminal.shutdown_calls == 1


def test_a_terminal_that_will_not_initialise_raises_typed() -> None:
    terminal = FakeTerminal(initialises=False)
    gateway = _gateway(terminal)

    with pytest.raises(BrokerUnavailableError):
        gateway.start()

    assert terminal.shutdown_calls == 1


def test_calls_are_served_on_a_demo_account() -> None:
    with _gateway(FakeTerminal()) as gateway:
        tick = gateway.call(Priority.MARKET_DATA, lambda t: t.symbol_tick("EURUSD"))

    assert tick is not None
    assert tick.bid == 1.1


def test_higher_priority_work_overtakes_lower() -> None:
    """FIFO would let a protection call sit behind data polls. At scalp
    horizons that is the difference between a managed stop and a blown one."""

    terminal = FakeTerminal()
    order: list[Priority] = []
    with _gateway(terminal) as gateway:
        terminal.gate.clear()
        blocker = threading.Thread(
            target=lambda: gateway.call(Priority.MARKET_DATA, lambda t: t.symbol_info("X")),
            daemon=True,
        )
        blocker.start()
        threading.Event().wait(0.2)

        threads = []
        for priority in (Priority.MARKET_DATA, Priority.RECONCILE, Priority.PROTECTION):
            thread = threading.Thread(
                target=lambda p=priority: gateway.call(p, lambda t: order.append(p)),
                daemon=True,
            )
            thread.start()
            threads.append(thread)
            threading.Event().wait(0.05)

        terminal.gate.set()
        for thread in threads:
            thread.join(5)
        blocker.join(5)

    assert order == [Priority.PROTECTION, Priority.RECONCILE, Priority.MARKET_DATA]


def test_metrics_report_served_calls_and_a_heartbeat() -> None:
    with _gateway(FakeTerminal()) as gateway:
        gateway.call(Priority.MARKET_DATA, lambda t: t.symbol_tick("EURUSD"))
        metrics = gateway.metrics()

    assert metrics.served >= 1
    assert metrics.last_heartbeat.tzinfo is UTC


def test_a_failing_operation_propagates_and_is_counted() -> None:
    def boom(terminal: object) -> None:
        raise RuntimeError("broker exploded")

    with _gateway(FakeTerminal()) as gateway:
        with pytest.raises(RuntimeError, match="broker exploded"):
            gateway.call(Priority.MARKET_DATA, boom)
        assert gateway.metrics().failed >= 1


def test_stopping_shuts_the_terminal_down() -> None:
    terminal = FakeTerminal()
    gateway = _gateway(terminal)
    gateway.start()
    gateway.stop()

    assert terminal.shutdown_calls == 1


def test_calls_before_start_are_refused() -> None:
    gateway = _gateway(FakeTerminal())

    with pytest.raises(BrokerUnavailableError):
        gateway.call(Priority.MARKET_DATA, lambda t: None)


def test_a_fresh_gateway_is_stale_until_reconciled() -> None:
    with _gateway(FakeTerminal()) as gateway:
        assert gateway.metrics().stale is True
        gateway.mark_reconciled()
        assert gateway.metrics().stale is False


def test_a_failed_call_marks_state_stale_again() -> None:
    """A failed terminal call is a connectivity gap; state cannot be trusted."""

    def boom(terminal: object) -> None:
        raise RuntimeError("connection lost")

    with _gateway(FakeTerminal()) as gateway:
        gateway.mark_reconciled()
        with pytest.raises(RuntimeError):
            gateway.call(Priority.MARKET_DATA, boom)
        assert gateway.metrics().stale is True


def test_stopping_fails_queued_requests_instead_of_making_them_wait() -> None:
    """A request the actor never reached must fail at once, not time out."""

    terminal = FakeTerminal()
    gateway = _gateway(terminal)
    gateway.start()
    terminal.gate.clear()

    blocker = threading.Thread(
        target=lambda: gateway.call(Priority.MARKET_DATA, lambda t: t.symbol_info("X")),
        daemon=True,
    )
    blocker.start()
    threading.Event().wait(0.2)

    outcome: list[BaseException] = []
    elapsed: list[float] = []

    def victim() -> None:
        started = time.monotonic()
        try:
            gateway.call(Priority.MARKET_DATA, lambda t: t.symbol_tick("EURUSD"))
        except BaseException as error:
            outcome.append(error)
        elapsed.append(time.monotonic() - started)

    waiter = threading.Thread(target=victim, daemon=True)
    waiter.start()
    threading.Event().wait(0.2)

    stopper = threading.Thread(target=gateway.stop, daemon=True)
    stopper.start()
    threading.Event().wait(0.2)
    terminal.gate.set()

    waiter.join(10)
    stopper.join(10)
    blocker.join(10)

    assert len(outcome) == 1
    assert isinstance(outcome[0], BrokerUnavailableError)
    assert elapsed[0] < 3.0, "the caller waited out the timeout instead of being drained"
    assert gateway.metrics().queue_depth[Priority.MARKET_DATA] == 0


def test_stopping_twice_shuts_the_terminal_down_once() -> None:
    terminal = FakeTerminal()
    gateway = _gateway(terminal)
    gateway.start()

    gateway.stop()
    gateway.stop()

    assert terminal.shutdown_calls == 1


def test_stopping_without_starting_does_not_shut_the_terminal_down() -> None:
    terminal = FakeTerminal()
    gateway = _gateway(terminal)

    gateway.stop()

    assert terminal.shutdown_calls == 0


def test_gateway_lifecycle_is_audited() -> None:
    events: list[tuple[str, Mapping[str, object]]] = []
    terminal = FakeTerminal()
    gateway = Mt5Gateway(
        terminal,
        clock=SystemClock(),
        on_event=lambda name, payload: events.append((name, payload)),
    )
    gateway.start()
    gateway.mark_reconciled()
    gateway.stop()

    assert [name for name, _ in events] == [
        "gateway.connected",
        "gateway.demo_verified",
        "gateway.reconciled",
        "gateway.disconnected",
    ]
    for _, payload in events:
        assert "account" not in str(payload).lower()


def test_stop_without_start_emits_no_disconnection_event() -> None:
    """A connection that never existed must not be recorded as disconnected."""

    events: list[str] = []
    gateway = Mt5Gateway(
        FakeTerminal(), clock=SystemClock(), on_event=lambda name, _: events.append(name)
    )

    gateway.stop()

    assert events == []


def test_a_second_stop_emits_no_further_disconnection_event() -> None:
    events: list[str] = []
    gateway = Mt5Gateway(
        FakeTerminal(), clock=SystemClock(), on_event=lambda name, _: events.append(name)
    )
    gateway.start()

    gateway.stop()
    gateway.stop()

    assert events.count("gateway.disconnected") == 1


def test_a_failed_demo_check_emits_no_disconnection_event() -> None:
    """The gateway never reached ``_started``, so ``stop()`` semantics do not
    apply; the typed error itself carries the refusal."""

    events: list[str] = []
    terminal = FakeTerminal(trade_mode=ACCOUNT_TRADE_MODE_REAL)
    gateway = Mt5Gateway(
        terminal, clock=SystemClock(), on_event=lambda name, _: events.append(name)
    )

    with pytest.raises(NonDemoAccountError):
        gateway.start()

    assert "gateway.disconnected" not in events


def test_the_audit_hook_is_never_called_while_the_lock_is_held() -> None:
    """A slow hook (e.g. a database write) must not block metrics() or the
    actor thread's own bookkeeping. Re-acquiring a held, non-reentrant lock
    from the same thread would deadlock the test outright, so this probes
    with a non-blocking acquire instead of risking a hang."""

    lock_was_held: list[str] = []

    def on_event(name: str, payload: Mapping[str, object]) -> None:
        del payload
        acquired = gateway._lock.acquire(blocking=False)
        if acquired:
            gateway._lock.release()
        else:
            lock_was_held.append(name)

    gateway = Mt5Gateway(FakeTerminal(), clock=SystemClock(), on_event=on_event)
    gateway.start()
    gateway.mark_reconciled()
    gateway.stop()

    assert lock_was_held == []
