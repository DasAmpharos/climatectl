"""Smart Device Management API (https://smartdevicemanagement.googleapis.com/v1), and how
its traits map to the neutral vocabulary in climatectl.thermostat."""

from __future__ import annotations

from typing import Any

import requests

from ...providers import ProviderError
from ...thermostat import COOL, COOLING, HEAT, HEAT_COOL, HEATING, IDLE, OFF, Thermostat

BASE_URL = "https://smartdevicemanagement.googleapis.com/v1"
THERMOSTAT_TYPE = "sdm.devices.types.THERMOSTAT"
T = "sdm.devices.traits."
SETPOINT_COMMAND = "sdm.devices.commands.ThermostatTemperatureSetpoint."

# SDM value -> neutral value. ThermostatEco's mode is MANUAL_ECO or OFF: anything but OFF is
# eco on.
MODES = {"HEAT": HEAT, "COOL": COOL, "HEATCOOL": HEAT_COOL, "OFF": OFF}
HVAC = {"HEATING": HEATING, "COOLING": COOLING, "OFF": IDLE}


class SdmError(ProviderError):
    pass


def list_devices(project_id: str, access_token: str) -> list[dict[str, Any]]:
    try:
        resp = requests.get(
            f"{BASE_URL}/enterprises/{project_id}/devices",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
    except requests.RequestException as e:
        raise SdmError(f"devices.list failed: {type(e).__name__}") from None
    _raise_for_status(resp, "devices.list")
    return resp.json().get("devices", [])


def execute(
    project_id: str, access_token: str, device_id: str, command: str, params: dict[str, Any]
) -> None:
    """Run one device command, e.g. ThermostatTemperatureSetpoint.SetRange."""
    label = f"executeCommand {command.rsplit('.', 1)[-1]}"
    try:
        resp = requests.post(
            f"{BASE_URL}/enterprises/{project_id}/devices/{device_id}:executeCommand",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"command": command, "params": params},
            timeout=30,
        )
    except requests.RequestException as e:
        raise SdmError(f"{label} failed: {type(e).__name__}") from None
    _raise_for_status(resp, label)


def setpoint_command(heat_c: float | None, cool_c: float | None) -> tuple[str, dict[str, float]]:
    """SetRange for both setpoints, SetHeat or SetCool for one."""
    if heat_c is not None and cool_c is not None:
        return f"{SETPOINT_COMMAND}SetRange", {"heatCelsius": heat_c, "coolCelsius": cool_c}
    if heat_c is not None:
        return f"{SETPOINT_COMMAND}SetHeat", {"heatCelsius": heat_c}
    if cool_c is not None:
        return f"{SETPOINT_COMMAND}SetCool", {"coolCelsius": cool_c}
    raise ValueError("set_range needs a heat or a cool setpoint")


def _raise_for_status(resp: requests.Response, label: str) -> None:
    if resp.ok:
        return
    try:
        error = resp.json()["error"]
        detail = f"{error.get('status')}: {error.get('message')}"
    except ValueError, KeyError:
        detail = "unparseable error body"
    raise SdmError(f"{label} -> {resp.status_code}: {detail}")


def device_name(device: dict[str, Any]) -> str:
    """The name set in the Google Home app, else its room, else "(unnamed)"."""
    custom = device.get("traits", {}).get(f"{T}Info", {}).get("customName")
    room = next((p.get("displayName") for p in device.get("parentRelations", [])), None)
    return custom or room or "(unnamed)"


def neutral(table: dict[str, str], value: str | None) -> str | None:
    """`value` in the neutral vocabulary; one with no neutral name passes through lowercased."""
    return None if value is None else table.get(value, value.lower())


def readings(traits: dict[str, Any]) -> dict[str, Any]:
    """The neutral fields in a set of traits: a device's, or the changed ones in an event.
    A field whose trait is absent is None."""

    def trait(name: str) -> dict[str, Any]:
        return traits.get(f"{T}{name}", {})

    eco = trait("ThermostatEco").get("mode")
    return {
        "temp_c": trait("Temperature").get("ambientTemperatureCelsius"),
        "humidity_pct": trait("Humidity").get("ambientHumidityPercent"),
        "hvac": neutral(HVAC, trait("ThermostatHvac").get("status")),
        "heat_c": trait("ThermostatTemperatureSetpoint").get("heatCelsius"),
        "cool_c": trait("ThermostatTemperatureSetpoint").get("coolCelsius"),
        "mode": neutral(MODES, trait("ThermostatMode").get("mode")),
        "eco": None if eco is None else eco != "OFF",
    }


def thermostat(device: dict[str, Any]) -> Thermostat:
    traits = device.get("traits", {})
    now = readings(traits)
    eco = traits.get(f"{T}ThermostatEco", {})
    connectivity = traits.get(f"{T}Connectivity", {}).get("status")
    return Thermostat(
        device_id=device["name"].rsplit("/", 1)[-1],
        name=device_name(device),
        online=None if connectivity is None else connectivity == "ONLINE",
        ambient_c=now["temp_c"],
        humidity_pct=now["humidity_pct"],
        mode=now["mode"],
        hvac=now["hvac"],
        eco=now["eco"],
        heat_c=now["heat_c"],
        cool_c=now["cool_c"],
        eco_heat_c=eco.get("heatCelsius"),
        eco_cool_c=eco.get("coolCelsius"),
    )


def thermostats(devices: list[dict[str, Any]]) -> list[Thermostat]:
    return [thermostat(d) for d in devices if d.get("type") == THERMOSTAT_TYPE]


def describe(device: dict[str, Any]) -> str:
    """One line per device in SDM's own terms, e.g. `Downstairs [THERMOSTAT], 21.3°C / 70°F,
    HVAC OFF`."""
    traits = device.get("traits", {})
    kind = device.get("type", "").rsplit(".", 1)[-1]
    parts = [f"{device_name(device)} [{kind}]"]
    temp = traits.get(f"{T}Temperature", {}).get("ambientTemperatureCelsius")
    if temp is not None:
        parts.append(f"{temp:.1f}°C / {temp * 9 / 5 + 32:.0f}°F")
    hvac = traits.get(f"{T}ThermostatHvac", {}).get("status")
    if hvac:
        parts.append(f"HVAC {hvac}")
    mode = traits.get(f"{T}ThermostatMode", {}).get("mode")
    if mode:
        parts.append(f"mode {mode}")
    return ", ".join(parts)
