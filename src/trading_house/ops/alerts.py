"""Telling a person. One webhook, four body shapes, and no retries.

An alert is sent once per halt entered and once per halt cleared -- the
transitions -- never per cycle a condition persists. Delivery is never on the
path a halt depends on: the halt is committed before an alert is attempted,
and a failed delivery is recorded in the audit ledger rather than raised, so a
dead chat service cannot stop the system from stopping itself.

The webhook URL carries the channel's credential (a Slack or Discord hook URL
is the token), so it is a ``SecretStr``, must be https, and appears in no
output, error or audit row.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol
from urllib.parse import urlsplit

from pydantic import SecretStr

from trading_house.core.errors import ConfigurationError

DELIVERY_TIMEOUT_SECONDS = 5.0
DISCORD_CONTENT_LIMIT = 2000


class AlertDeliveryError(Exception):
    """Raised when an alert could not be handed to its channel.

    Deliberately not a ``TradingHouseError``: it never leaves
    ``ControlService``, which audits it and carries on, so it has no exit code
    to own.
    """


class AlertFormat(str, Enum):  # noqa: UP042
    SLACK = "slack"
    DISCORD = "discord"
    NTFY = "ntfy"
    JSON = "json"


@dataclass(frozen=True, slots=True)
class Alert:
    """``critical`` for a halt entered, ``info`` for one cleared."""

    title: str
    severity: str
    fields: Mapping[str, str] = field(default_factory=dict)

    def text(self) -> str:
        lines = [f"[{self.severity.upper()}] {self.title}"]
        lines.extend(f"{key}: {value}" for key, value in sorted(self.fields.items()))
        return "\n".join(lines)


class Alerter(Protocol):
    @property
    def channel(self) -> str: ...
    def send(self, alert: Alert) -> None: ...


class NoAlerter:
    """No channel configured. Every send fails, so every halt records that
    nobody was told -- the honest outcome, rather than a silent success."""

    channel = "unconfigured"

    def send(self, alert: Alert) -> None:
        raise AlertDeliveryError()


Opener = Callable[..., Any]


class WebhookAlerter:
    def __init__(
        self,
        url: SecretStr,
        alert_format: AlertFormat,
        *,
        opener: Opener = urllib.request.urlopen,
    ) -> None:
        if urlsplit(url.get_secret_value()).scheme != "https":
            # Plain http would put the channel's token on the wire in clear.
            raise ConfigurationError()
        self._url = url
        self._format = alert_format
        self._opener = opener

    @property
    def channel(self) -> str:
        return f"webhook:{self._format.value}"

    def send(self, alert: Alert) -> None:
        body, content_type, headers = self._body(alert)
        request = urllib.request.Request(  # noqa: S310 -- scheme checked https in __init__
            self._url.get_secret_value(),
            data=body,
            headers={"Content-Type": content_type, **headers},
            method="POST",
        )
        try:
            with self._opener(request, timeout=DELIVERY_TIMEOUT_SECONDS) as response:
                status = getattr(response, "status", 200)
        except Exception:
            # The cause can carry the URL; it stays off the public error.
            raise AlertDeliveryError() from None
        if not 200 <= int(status) < 300:
            raise AlertDeliveryError()

    def _body(self, alert: Alert) -> tuple[bytes, str, dict[str, str]]:
        text = alert.text()
        if self._format is AlertFormat.NTFY:
            priority = "urgent" if alert.severity == "critical" else "default"
            return text.encode(), "text/plain; charset=utf-8", {"Priority": priority}
        payload: dict[str, Any]
        if self._format is AlertFormat.SLACK:
            payload = {"text": text}
        elif self._format is AlertFormat.DISCORD:
            payload = {"content": text[:DISCORD_CONTENT_LIMIT]}
        else:
            payload = {
                "title": alert.title,
                "severity": alert.severity,
                "fields": dict(alert.fields),
                "text": text,
            }
        return json.dumps(payload, sort_keys=True).encode(), "application/json", {}


def alerter_from(url: SecretStr | None, alert_format: AlertFormat) -> Alerter:
    return NoAlerter() if url is None else WebhookAlerter(url, alert_format)
