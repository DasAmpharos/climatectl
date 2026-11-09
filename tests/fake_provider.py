"""A second thermostat provider, all in memory, to show the logger needs nothing from Nest.
Tests register it as THERMOSTAT_PROVIDER=fake by patching providers.PROVIDERS."""

from __future__ import annotations

import sqlite3

from climatectl import store
from climatectl.thermostat import HEAT, HEATING, Thermostat, ThermostatEvent

HALL = Thermostat(
    device_id="hall",
    name="Hall",
    online=True,
    ambient_c=19.0,
    humidity_pct=45.0,
    mode=HEAT,
    hvac=HEATING,
    eco=False,
    heat_c=20.0,
    cool_c=None,
    eco_heat_c=None,
    eco_cool_c=None,
)


class FakeEvents:
    def __init__(self, waiting: list[ThermostatEvent]) -> None:
        self.waiting = waiting

    def drain(self, conn: sqlite3.Connection, received_at: str) -> int:
        new = store.save_events(conn, self.waiting)
        self.waiting = []
        return new


class FakeProvider:
    name = "Fake"
    hints: dict[str, str] = {}

    def __init__(self) -> None:
        self.prepared = 0
        self.ranges: list[tuple[str, float | None, float | None]] = []
        self.pending = [ThermostatEvent("fake-1", "2026-10-24T00:30:00Z", "hall", hvac=HEATING)]

    def prepare(self, conn: sqlite3.Connection) -> None:
        self.prepared += 1

    def thermostats(self) -> list[Thermostat]:
        return [HALL]

    def set_range(self, device_id: str, heat_c: float | None, cool_c: float | None) -> None:
        self.ranges.append((device_id, heat_c, cool_c))

    def events(self) -> FakeEvents:
        return FakeEvents(self.pending)


def provider() -> FakeProvider:
    return FakeProvider()
