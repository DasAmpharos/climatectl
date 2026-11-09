"""Nest's own tables: sdm_event, the raw log of every Pub/Sub event as Google sent it. Each
event's thermostat fields also go into the neutral thermostat_event table, which is what the
views and health checks read."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

from ... import store
from ...thermostat import ThermostatEvent
from . import sdm
from .pubsub import Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS sdm_event (
    event_id     TEXT PRIMARY KEY,
    event_at     TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    device_id    TEXT,
    kind         TEXT NOT NULL,
    traits_json  TEXT NOT NULL,
    raw_json     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sdm_event_device_time ON sdm_event (device_id, event_at);
CREATE INDEX IF NOT EXISTS sdm_event_time ON sdm_event (event_at);
"""


def normalize(
    event_id: str, at: str, device_id: str | None, traits: dict
) -> ThermostatEvent | None:
    """The thermostat fields in one event, or None if it has none (a relationUpdate, a fan
    timer change, a device that isn't a thermostat)."""
    fields = sdm.readings(traits)
    if device_id is None or all(v is None for v in fields.values()):
        return None
    return ThermostatEvent(event_id, at, device_id, **fields)


def save_events(conn: sqlite3.Connection, events: Iterable[Event], received_at: str) -> int:
    """How many of `events` were new. Pub/Sub may redeliver; repeats are ignored. The raw
    rows and the neutral ones commit together."""
    events = list(events)
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO sdm_event VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (e.event_id, e.at, received_at, e.device_id, e.kind, json.dumps(e.traits), e.raw)
            for e in events
        ],
    )
    new = conn.total_changes - before
    normalized = (normalize(e.event_id, e.at, e.device_id, e.traits) for e in events)
    store.save_events(conn, [n for n in normalized if n is not None])
    return new


def prepare(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
