"""Google Nest thermostats through the Smart Device Management API, with events from a
Pub/Sub pull subscription. `climatectl nest-auth` sets up the OAuth refresh token."""

from __future__ import annotations

import sqlite3

from ...thermostat import Thermostat
from . import pubsub, sdm, store
from .session import open_session

MAX_PULLS = 20  # per drain, 100 events each: far more than a house of thermostats produces


class NestEvents:
    def __init__(self, subscriber: pubsub.Subscriber) -> None:
        self.subscriber = subscriber

    def drain(self, conn: sqlite3.Connection, received_at: str) -> int:
        """Store every waiting event, acknowledging each batch only after it is committed."""
        new = 0
        for _ in range(MAX_PULLS):
            events = self.subscriber.pull()
            if not events:
                break
            new += store.save_events(conn, events, received_at)
            self.subscriber.acknowledge([e.ack_id for e in events])
        return new


class NestProvider:
    name = "Nest"
    hints = {
        "stale:thermostats": "If the refresh token was revoked, run `climatectl nest-auth` again.",
        "stale:events": (
            "Check that events are on in the Device Access Console and that the Pub/Sub "
            "subscription still exists."
        ),
        "no_thermostats": (
            "Check that they're switched on at https://nestservices.google.com/partnerconnections"
        ),
    }

    def prepare(self, conn: sqlite3.Connection) -> None:
        store.prepare(conn)

    def thermostats(self) -> list[Thermostat]:
        session = open_session()
        return sdm.thermostats(sdm.list_devices(session.project_id, session.access_token))

    def set_range(self, device_id: str, heat_c: float | None, cool_c: float | None) -> None:
        command, params = sdm.setpoint_command(heat_c, cool_c)
        session = open_session()
        sdm.execute(session.project_id, session.access_token, device_id, command, params)

    def events(self) -> NestEvents:
        return NestEvents(pubsub.Subscriber(pubsub.subscription_path()))


def provider() -> NestProvider:
    return NestProvider()
