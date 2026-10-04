"""Phase 11: the alert channel. No request ever leaves the process here; the
opener is a fake that records what would have been sent."""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import SecretStr

from trading_house.core.errors import ConfigurationError
from trading_house.ops.alerts import (
    DISCORD_CONTENT_LIMIT,
    Alert,
    AlertDeliveryError,
    AlertFormat,
    NoAlerter,
    WebhookAlerter,
    alerter_from,
)

URL = SecretStr("https://hooks.example.test/T000/B000/secret-token")
ALERT = Alert(
    title="trading halted",
    severity="critical",
    fields={"kind": "safe_mode", "reason": "guard_escalation"},
)


@dataclass
class _Response:
    status: int = 200

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@dataclass
class FakeOpener:
    status: int = 200
    raises: Exception | None = None
    sent: list[urllib.request.Request] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)

    def __call__(self, request: urllib.request.Request, *, timeout: float) -> Any:
        self.sent.append(request)
        self.timeouts.append(timeout)
        if self.raises is not None:
            raise self.raises
        return _Response(self.status)


def _send(alert_format: AlertFormat, opener: FakeOpener | None = None) -> FakeOpener:
    opener = opener or FakeOpener()
    WebhookAlerter(URL, alert_format, opener=opener).send(ALERT)
    return opener


def _json(opener: FakeOpener) -> dict[str, Any]:
    data = opener.sent[0].data
    assert isinstance(data, bytes)
    return dict(json.loads(data))


def test_the_text_leads_with_severity_and_lists_fields_in_order() -> None:
    assert ALERT.text() == ("[CRITICAL] trading halted\nkind: safe_mode\nreason: guard_escalation")


def test_slack_gets_text() -> None:
    opener = _send(AlertFormat.SLACK)

    assert _json(opener) == {"text": ALERT.text()}
    assert opener.sent[0].get_method() == "POST"
    assert opener.sent[0].full_url == URL.get_secret_value()
    assert opener.timeouts == [5.0]


def test_discord_gets_content_cut_to_its_limit() -> None:
    long_alert = Alert(title="x" * 3000, severity="critical")
    opener = FakeOpener()
    WebhookAlerter(URL, AlertFormat.DISCORD, opener=opener).send(long_alert)

    assert len(_json(opener)["content"]) == DISCORD_CONTENT_LIMIT


def test_ntfy_gets_plain_text_and_an_urgent_priority_for_a_halt() -> None:
    opener = _send(AlertFormat.NTFY)

    assert opener.sent[0].data == ALERT.text().encode()
    assert opener.sent[0].get_header("Priority") == "urgent"


def test_generic_json_carries_the_fields() -> None:
    body = _json(_send(AlertFormat.JSON))

    assert body["severity"] == "critical"
    assert body["fields"] == {"kind": "safe_mode", "reason": "guard_escalation"}


def test_plain_http_is_refused_because_the_url_is_the_credential() -> None:
    with pytest.raises(ConfigurationError):
        WebhookAlerter(SecretStr("http://hooks.example.test/x"), AlertFormat.SLACK)


def test_a_transport_failure_is_a_delivery_error_that_does_not_carry_the_url() -> None:
    opener = FakeOpener(raises=OSError(f"could not reach {URL.get_secret_value()}"))

    with pytest.raises(AlertDeliveryError) as failure:
        _send(AlertFormat.SLACK, opener)

    assert "secret-token" not in str(failure.value)
    assert failure.value.__cause__ is None


def test_a_non_2xx_answer_is_a_delivery_error() -> None:
    with pytest.raises(AlertDeliveryError):
        _send(AlertFormat.SLACK, FakeOpener(status=500))


def test_no_channel_means_every_send_fails_honestly() -> None:
    alerter = alerter_from(None, AlertFormat.JSON)

    assert isinstance(alerter, NoAlerter)
    assert alerter.channel == "unconfigured"
    with pytest.raises(AlertDeliveryError):
        alerter.send(ALERT)


def test_a_configured_channel_names_its_format() -> None:
    assert alerter_from(URL, AlertFormat.NTFY).channel == "webhook:ntfy"
