"""Thermostats in provider-neutral terms. Every provider maps its own values to these, so the
store, the views, the health checks and the CLIs never see a brand's vocabulary.

The names are Home Assistant's, for its climate entities, so a Home Assistant provider maps
almost one to one:

- mode (what the thermostat is set to do): heat, cool, heat_cool, off
- hvac (what the equipment is doing now): heating, cooling, idle. Idle means not calling for
  either, whatever the mode; the mode says whether the system is off.
- eco: True while an energy-saving preset replaces the normal setpoints, else False

A provider passes through a value with no neutral name, lowercased, so nothing is lost. The
core only acts on the names above: anything but heating or cooling counts as not calling."""

from __future__ import annotations

from dataclasses import dataclass

HEAT, COOL, HEAT_COOL, OFF = "heat", "cool", "heat_cool", "off"
HEATING, COOLING, IDLE = "heating", "cooling", "idle"
CALLING = (HEATING, COOLING)


@dataclass(frozen=True)
class Thermostat:
    """One thermostat's current state. Temperatures are Celsius. Any field can be None when
    the device doesn't report it."""

    device_id: str
    name: str
    online: bool | None
    ambient_c: float | None
    humidity_pct: float | None
    mode: str | None  # heat, cool, heat_cool, off
    hvac: str | None  # heating, cooling, idle
    eco: bool | None
    heat_c: float | None
    cool_c: float | None
    eco_heat_c: float | None
    eco_cool_c: float | None


@dataclass(frozen=True)
class ThermostatEvent:
    """A change a provider pushed between snapshots. Only the fields that changed are set;
    the rest are None. `event_id` is the provider's own id for it, so a redelivered event is
    stored once."""

    event_id: str
    at: str  # when the change happened, UTC
    device_id: str
    temp_c: float | None = None
    humidity_pct: float | None = None
    hvac: str | None = None
    heat_c: float | None = None
    cool_c: float | None = None
    mode: str | None = None
    eco: bool | None = None
