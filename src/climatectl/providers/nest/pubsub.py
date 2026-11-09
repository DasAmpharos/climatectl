"""Nest SDM events from the Pub/Sub pull subscription, over the REST API.

Credentials come from Application Default Credentials: `gcloud auth application-default
login` on a development machine, or GOOGLE_APPLICATION_CREDENTIALS pointing at a service
account key on the server. Delivery is at least once, so callers store events keyed on
eventId and acknowledge only after the store commits."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import google.auth
import requests
from google.auth.exceptions import DefaultCredentialsError, GoogleAuthError
from google.auth.transport.requests import AuthorizedSession

from ... import envfile
from ...envfile import ConfigError
from ...providers import ProviderError

BASE_URL = "https://pubsub.googleapis.com/v1"
SCOPES = ["https://www.googleapis.com/auth/pubsub"]
DEFAULT_SUBSCRIPTION = "sdm-events-pull"
SUBSCRIPTION_PATH = re.compile(r"^projects/[a-z][a-z0-9-]+/subscriptions/[A-Za-z][\w.~%+-]+$")
NO_CREDENTIALS = (
    "No Google Cloud credentials. On a development machine run "
    "`gcloud auth application-default login`; on the server set "
    "GOOGLE_APPLICATION_CREDENTIALS to the service account key file."
)


class PubSubError(ProviderError):
    pass


class HttpSession(Protocol):
    def post(self, url: str, json: dict[str, Any], timeout: float) -> requests.Response: ...


@dataclass(frozen=True)
class Event:
    """One SDM event. `traits` is empty for relationUpdate events (devices added, moved or
    removed). `at` is when Google says the change happened, not when it was pulled."""

    ack_id: str
    event_id: str
    at: str
    device_id: str | None
    kind: str  # resourceUpdate or relationUpdate
    traits: dict[str, Any]
    raw: str


def subscription_path() -> str:
    """PUBSUB_SUBSCRIPTION (a full path or just a name), else sdm-events-pull in
    GCP_PROJECT_ID."""
    settings = envfile.read_settings(("PUBSUB_SUBSCRIPTION", "GCP_PROJECT_ID"))
    name = settings.get("PUBSUB_SUBSCRIPTION", DEFAULT_SUBSCRIPTION)
    if not name.startswith("projects/"):
        if "GCP_PROJECT_ID" not in settings:
            raise ConfigError(f"Missing in {envfile.env_path()}: GCP_PROJECT_ID")
        name = f"projects/{settings['GCP_PROJECT_ID']}/subscriptions/{name}"
    if not SUBSCRIPTION_PATH.match(name):
        raise ConfigError(f"Not a Pub/Sub subscription path: {name}")
    return name


def default_session() -> HttpSession:
    try:
        credentials, _ = google.auth.default(scopes=SCOPES)
    except DefaultCredentialsError:
        raise PubSubError(NO_CREDENTIALS) from None
    return AuthorizedSession(credentials)


class Subscriber:
    def __init__(self, subscription: str, session: HttpSession | None = None) -> None:
        self.subscription = subscription
        self._session = session

    @property
    def session(self) -> HttpSession:
        if self._session is None:
            self._session = default_session()
        return self._session

    def pull(self, max_messages: int = 100) -> list[Event]:
        body = self._post("pull", {"maxMessages": max_messages})
        return [parse(m) for m in body.get("receivedMessages", [])]

    def acknowledge(self, ack_ids: list[str]) -> None:
        if ack_ids:
            self._post("acknowledge", {"ackIds": ack_ids})

    def _post(self, verb: str, payload: dict[str, Any]) -> dict[str, Any]:
        label = f"Pub/Sub {verb}"
        try:
            resp = self.session.post(
                f"{BASE_URL}/{self.subscription}:{verb}", json=payload, timeout=60
            )
        except requests.RequestException as e:
            raise PubSubError(f"{label} failed: {type(e).__name__}") from None
        except GoogleAuthError as e:  # e.g. a revoked login or a deleted service account key
            raise PubSubError(f"{label}: credentials failed: {type(e).__name__}") from None
        if not resp.ok:
            try:
                error = resp.json()["error"]
                detail = f"{error.get('status')}: {error.get('message')}"
            except ValueError, KeyError:
                detail = "unparseable error body"
            raise PubSubError(f"{label} -> {resp.status_code}: {detail}")
        return resp.json()


def parse(received: dict[str, Any]) -> Event:
    message = received["message"]
    try:
        raw = base64.b64decode(message.get("data", "")).decode()
        data = json.loads(raw)
    except binascii.Error, UnicodeDecodeError, json.JSONDecodeError:
        raw, data = message.get("data", ""), {}
    if "resourceUpdate" in data:
        kind, update = "resourceUpdate", data["resourceUpdate"]
    elif "relationUpdate" in data:
        kind, update = "relationUpdate", data["relationUpdate"]
    else:
        kind, update = "unknown", {}
    name = update.get("name") or update.get("object")
    return Event(
        ack_id=received["ackId"],
        event_id=data.get("eventId") or message["messageId"],
        at=_utc(data.get("timestamp") or message["publishTime"]),
        device_id=name.rsplit("/", 1)[-1] if name and "/devices/" in name else None,
        kind=kind,
        traits=update.get("traits", {}),
        raw=raw,
    )


def _utc(text: str) -> str:
    """Google's timestamps (fractional seconds, Z) in the store's format."""
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
