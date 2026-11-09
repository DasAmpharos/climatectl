"""In-memory stand-in for a Pub/Sub pull subscription (:pull and :acknowledge).

Like the real thing, a pulled message that isn't acknowledged is delivered again."""

from __future__ import annotations

import base64
import itertools
import json
from typing import Any

from fake_google import PROJECT, FakeResponse

SUBSCRIPTION = "projects/climatectl-123/subscriptions/sdm-events-pull"


def sdm_event(event_id: str, at: str, device: str, traits: dict[str, Any]) -> dict[str, Any]:
    return {
        "eventId": event_id,
        "timestamp": at,
        "resourceUpdate": {
            "name": f"enterprises/{PROJECT}/devices/{device}",
            "traits": traits,
        },
        "userId": "AVPHwEtest",
        "resourceGroup": [f"enterprises/{PROJECT}/devices/{device}"],
    }


def hvac(event_id: str, at: str, device: str, status: str) -> dict[str, Any]:
    return sdm_event(
        event_id, at, device, {"sdm.devices.traits.ThermostatHvac": {"status": status}}
    )


class FakePubSub:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.waiting: dict[str, dict[str, Any]] = {}  # ackId -> received message
        self.acked: list[str] = []
        self.fail = False
        self.calls: list[str] = []

    def publish(self, payload: dict[str, Any] | bytes) -> None:
        n = next(self._ids)
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.waiting[f"ack-{n}"] = {
            "ackId": f"ack-{n}",
            "message": {
                "data": base64.b64encode(data).decode(),
                "messageId": f"msg-{n}",
                "publishTime": "2026-10-24T01:00:00.123Z",
            },
        }

    def post(self, url: str, json: dict[str, Any], timeout: float) -> FakeResponse:
        verb = url.rsplit(":", 1)[-1]
        self.calls.append(verb)
        if self.fail:
            return FakeResponse(
                403, {"error": {"status": "PERMISSION_DENIED", "message": "User not authorized"}}
            )
        if verb == "pull":
            batch = list(self.waiting.values())[: json["maxMessages"]]
            return FakeResponse(200, {"receivedMessages": batch} if batch else {})
        for ack_id in json["ackIds"]:
            self.waiting.pop(ack_id, None)
            self.acked.append(ack_id)
        return FakeResponse(200, {})
