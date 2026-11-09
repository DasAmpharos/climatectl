"""Acute fault checks, run after every logger cycle. They read only what the logger stored, so
they work from day one, before any baseline exists.

A thermostat reports when it *calls* for heat or cooling, not whether the furnace or AC
answered, so the main fault signature is a call that doesn't move the temperature. Readings
come from both the 15-minute snapshots and the provider's events, merged in time order: events
give exact start/stop times, snapshots fill in when an event is missing.

ALERT_FLOOR and ALERT_CEILING set the indoor temperatures that alert whatever the mode, each a
number and a unit, e.g. 55F or 12.5C."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import envfile
from .envfile import ConfigError
from .store import parse_utc
from .thermostat import CALLING, HEATING

LOOKBACK = timedelta(hours=6)  # history each check looks at
FRESH = timedelta(minutes=30)  # device checks skip a device with no data this recent

HEAT_GRACE = timedelta(minutes=30)
MIN_RISE_C = 0.3
COOL_GRACE = timedelta(minutes=45)
MIN_DROP_C = 0.3
SHORT_RUN = timedelta(minutes=5)
SHORT_RUNS = 3  # this many short runs inside SHORT_WINDOW is short cycling
SHORT_WINDOW = timedelta(hours=2)
FLOOR = "55F"  # pipe-freeze margin
CEILING = "86F"
OFFLINE_GRACE = timedelta(hours=1)
STALE_SNAPSHOTS = timedelta(hours=1)
STALE_WEATHER = timedelta(hours=2)
STALE_EVENTS = timedelta(hours=12)  # temperature alone changes every hour or two
TEMPERATURE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*°?([CF])$", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    """One problem seen this cycle. `key` identifies it across cycles (check and device), so
    an ongoing problem is one alert, not one per cycle."""

    key: str
    title: str
    message: str
    priority: str  # urgent, high, default (see alerts.PUSHOVER_PRIORITY)


@dataclass(frozen=True)
class Limits:
    floor_c: float
    ceiling_c: float

    @classmethod
    def from_settings(cls) -> Limits:
        settings = envfile.read_settings(("ALERT_FLOOR", "ALERT_CEILING"))
        floor = celsius(settings.get("ALERT_FLOOR", FLOOR), "ALERT_FLOOR")
        ceiling = celsius(settings.get("ALERT_CEILING", CEILING), "ALERT_CEILING")
        if floor >= ceiling:
            raise ConfigError("ALERT_FLOOR must be below ALERT_CEILING.")
        return cls(floor, ceiling)


def celsius(text: str, setting: str) -> float:
    """`text` such as 55F or 12.5C, in Celsius."""
    match = TEMPERATURE.match(text.strip())
    if not match:
        raise ConfigError(f"{setting} must be a temperature with its unit, e.g. 55F or 12.5C.")
    value, unit = float(match[1]), match[2].upper()
    return value if unit == "C" else (value - 32) * 5 / 9


DEFAULT_LIMITS = Limits(celsius(FLOOR, "FLOOR"), celsius(CEILING, "CEILING"))


@dataclass(frozen=True)
class Run:
    status: str
    start: datetime
    end: datetime | None  # None: still going


@dataclass
class Device:
    device_id: str
    name: str
    temps: list[tuple[datetime, float]]
    statuses: list[tuple[datetime, str]]
    online: list[tuple[datetime, bool]]

    def latest_at(self) -> datetime | None:
        times = [s[-1][0] for s in (self.temps, self.statuses, self.online) if s]
        return max(times) if times else None


def check(
    conn: sqlite3.Connection,
    now: datetime,
    events_enabled: bool,
    limits: Limits = DEFAULT_LIMITS,
    provider: str = "thermostat",
    hints: Mapping[str, str] | None = None,
) -> list[Finding]:
    """`provider` names the source in messages (e.g. Nest), and `hints` adds its advice to
    the finding with that key (see providers.Provider)."""
    findings = _system(conn, now, events_enabled, provider)
    for device in _devices(conn, now - LOOKBACK):
        latest = device.latest_at()
        if latest is not None and now - latest <= FRESH:
            findings += _device(device, now, limits)
    hints = hints or {}
    return [
        Finding(f.key, f.title, f"{f.message} {hints[f.key]}", f.priority) if f.key in hints else f
        for f in findings
    ]


def runs(statuses: list[tuple[datetime, str]]) -> list[Run]:
    """Consecutive identical statuses collapsed into runs. Duplicate events are common."""
    out: list[Run] = []
    for at, status in statuses:
        if out and out[-1].status == status:
            continue
        if out:
            out[-1] = Run(out[-1].status, out[-1].start, at)
        out.append(Run(status, at, None))
    return out


def _device(d: Device, now: datetime, limits: Limits) -> list[Finding]:
    found = []
    history = runs(d.statuses)
    current = history[-1] if history else None
    if current and current.status in CALLING and d.temps:
        heating = current.status == HEATING
        grace = HEAT_GRACE if heating else COOL_GRACE
        before = _value_at(d.temps, current.start)
        if now - current.start >= grace and before is not None:
            change = d.temps[-1][1] - before
            if (change if heating else -change) < (MIN_RISE_C if heating else MIN_DROP_C):
                found.append(_no_response(d, current, before, now))

    short = [
        r
        for r in history
        if r.status in CALLING
        and r.end is not None
        and r.end >= now - SHORT_WINDOW
        and r.end - r.start < SHORT_RUN
    ]
    if len(short) >= SHORT_RUNS:
        found.append(
            Finding(
                f"short_cycling:{d.device_id}",
                f"{d.name}: short cycling",
                f"{len(short)} runs under {_minutes(SHORT_RUN)} in the last "
                f"{_minutes(SHORT_WINDOW)}. A dirty filter or a failing flame sensor is the "
                "usual cause.",
                "high",
            )
        )

    if d.temps:
        temp = d.temps[-1][1]
        if temp < limits.floor_c:
            found.append(
                Finding(
                    f"below_floor:{d.device_id}",
                    f"{d.name} is too cold",
                    f"{_f(temp)}, below the {_f(limits.floor_c)} safety floor. Check that the "
                    "furnace runs and the thermostat is in a heating mode.",
                    "urgent",
                )
            )
        elif temp > limits.ceiling_c:
            found.append(
                Finding(
                    f"above_ceiling:{d.device_id}",
                    f"{d.name} is too hot",
                    f"{_f(temp)}, above {_f(limits.ceiling_c)}. Check the AC and the thermostat "
                    "mode.",
                    "high",
                )
            )

    since = None  # start of the current offline streak
    for at, ok in reversed(d.online):
        if ok:
            break
        since = at
    if since is not None and now - since >= OFFLINE_GRACE:
        found.append(
            Finding(
                f"offline:{d.device_id}",
                f"{d.name} thermostat offline",
                f"Offline for {_minutes(now - since)}. Check its Wi-Fi and power.",
                "high",
            )
        )
    return found


def _no_response(d: Device, run: Run, before: float, now: datetime) -> Finding:
    heating = run.status == HEATING
    change = (d.temps[-1][1] - before) * 9 / 5
    what, kind = ("heat", "furnace") if heating else ("cooling", "AC")
    hint = (
        "Check the furnace (filter, igniter, flame sensor, gas) and that no window is open."
        if heating
        else "Check that the outdoor unit runs and the coil isn't iced; check the filter."
    )
    return Finding(
        f"{'heat_no_rise' if heating else 'cool_no_drop'}:{d.device_id}",
        f"{d.name}: {kind} may not be working",
        f"Calling for {what} for {_minutes(now - run.start)}, but the temperature went from "
        f"{_f(before)} to {_f(d.temps[-1][1])} ({change:+.1f}°F). {hint}",
        "high",
    )


def _system(
    conn: sqlite3.Connection, now: datetime, events_enabled: bool, provider: str
) -> list[Finding]:
    found = []
    first, last = conn.execute(
        "SELECT min(taken_at), max(taken_at) FROM thermostat_snapshot"
    ).fetchone()
    if last and now - parse_utc(last) >= STALE_SNAPSHOTS:
        found.append(
            Finding(
                "stale:thermostats",
                "Thermostat data stopped",
                f"No thermostat readings for {_minutes(now - parse_utc(last))}: the "
                f"{provider} API calls are failing. Check the logger's output.",
                "default",
            )
        )
    (weather,) = conn.execute("SELECT max(fetched_at) FROM weather_observation").fetchone()
    if weather and now - parse_utc(weather) >= STALE_WEATHER:
        found.append(
            Finding(
                "stale:weather",
                "Weather data stopped",
                f"No new weather observation for {_minutes(now - parse_utc(weather))}. "
                "Check the logger's output.",
                "default",
            )
        )
    if events_enabled and first and now - parse_utc(first) >= STALE_EVENTS:
        (event,) = conn.execute("SELECT max(at) FROM thermostat_event").fetchone()
        if event is None or now - parse_utc(event) >= STALE_EVENTS:
            age = "ever" if event is None else f"for {_minutes(now - parse_utc(event))}"
            found.append(
                Finding(
                    "stale:events",
                    f"{provider[:1].upper()}{provider[1:]} events stopped",
                    f"No {provider} events {age}.",
                    "default",
                )
            )
    return found


def _devices(conn: sqlite3.Connection, since: datetime) -> list[Device]:
    cutoff = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    devices: dict[str, Device] = {}
    for taken_at, device_id, name, online, ambient_c, hvac in conn.execute(
        "SELECT taken_at, device_id, name, online, ambient_c, hvac FROM thermostat_snapshot"
        " WHERE taken_at >= ? ORDER BY taken_at",
        (cutoff,),
    ):
        d = devices.setdefault(device_id, Device(device_id, name, [], [], []))
        d.name = name or d.name
        at = parse_utc(taken_at)
        if ambient_c is not None:
            d.temps.append((at, ambient_c))
        if hvac is not None:
            d.statuses.append((at, hvac))
        if online is not None:
            d.online.append((at, bool(online)))
    for at, device_id, temp, status in conn.execute(
        "SELECT at, device_id, temp_c, hvac FROM thermostat_event WHERE at >= ?"
        " AND (temp_c IS NOT NULL OR hvac IS NOT NULL)",
        (cutoff,),
    ):
        d = devices.get(device_id)
        if d is None:  # not a thermostat we have a snapshot of
            continue
        if temp is not None:
            d.temps.append((parse_utc(at), temp))
        if status is not None:
            d.statuses.append((parse_utc(at), status))
    for d in devices.values():
        d.temps.sort(key=lambda r: r[0])
        d.statuses.sort(key=lambda r: r[0])
    return list(devices.values())


def _value_at(series: list[tuple[datetime, float]], at: datetime) -> float | None:
    """The last value at or before `at`, else the first one after it (the run began before
    the lookback window)."""
    earlier = [v for t, v in series if t <= at]
    return earlier[-1] if earlier else (series[0][1] if series else None)


def _f(celsius: float) -> str:
    return f"{celsius * 9 / 5 + 32:.1f}°F"


def _minutes(span: timedelta) -> str:
    total = int(span.total_seconds() // 60)
    hours, minutes = divmod(total, 60)
    if not hours:
        return f"{minutes} min"
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"
