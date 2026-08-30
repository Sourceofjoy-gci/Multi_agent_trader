import threading
import time
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
