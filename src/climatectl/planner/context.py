"""This run's input for the planner, built from the database, the config, the notes file and the
feedback file. Temperatures are in the config's unit.

Never in it: secrets, the home location, device ids. Levels go by their names in the config."""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..store import parse_utc
from ..thermostat import COOL, HEAT, HEAT_COOL, OFF
from . import house
from .config import Config, Level, delta_from_c, from_c

FRESH = timedelta(minutes=30)  # a level with no snapshot this recent isn't planned
HORIZON = timedelta(hours=24)
RECENT_DAYS = 7
FEEDBACK_DAYS, FEEDBACK_ROWS, FEEDBACK_NOTE_CHARS = 28, 40, 400
NOTES_CHARS = 4000
PLANNABLE_MODES = (HEAT, COOL, HEAT_COOL)
WRITE_MATCH = timedelta(minutes=15)  # how soon after the applier writes its change shows up


@dataclass(frozen=True)
class LevelState:
    """What the guardrails need about one level at planning time."""

    level: Level
    device_id: str | None
    mode: str | None
    heat_c: float | None
    cool_c: float | None
    skip: str | None  # why this level isn't planned, else None


@dataclass(frozen=True)
class Context:
    now: datetime
    zone: ZoneInfo
    payload: dict[str, Any]  # sent to Claude as JSON
    levels: dict[str, LevelState]  # by level key


def build(conn: sqlite3.Connection, config: Config, now: datetime, zone: ZoneInfo) -> Context:
    unit = config.unit
    names = dict(conn.execute("SELECT device_id, name FROM thermostat"))
    states, levels = {}, []
    for level in config.levels:
        device = _device(level.key, names)
        state, current = _state(conn, level, device, now, zone, config)
        states[level.key] = state
        model = house.fit(conn, device, now, zone) if device else None
        levels.append(
            {
                "level": level.key,
                "about": level.about,
                "planned": state.skip is None,
                **({"not_planned_because": state.skip} if state.skip else {}),
                "now": current,
                "comfort_bands": [_band(b, unit) for b in level.bands],
                "house_model": _model(model, unit) if model else None,
                "recent_days": _recent(conn, device, now, zone) if device else [],
            }
        )
    local = now.astimezone(zone)
    payload = {
        "now": local.strftime("%Y-%m-%dT%H:%M %A"),
        "unit": unit,
        "levels": levels,
        "forecast": _forecast(conn, now, unit),
        "feedback": _feedback(config, now, zone),
        "notes": _notes(config),
    }
    return Context(now, zone, payload, states)


def _device(key: str, names: dict[str, str]) -> str | None:
    """A level's thermostat: the config key is its device id or its latest name."""
    if key in names:
        return key
    return next((d for d, n in names.items() if n == key), None)


def _state(conn, level: Level, device: str | None, now: datetime, zone: ZoneInfo, config: Config):
    unit = config.unit
    if device is None:
        return LevelState(level, None, None, None, None, "no thermostat by this name"), None
    row = conn.execute(
        "SELECT taken_at, online, ambient_c, humidity_pct, mode, hvac, eco, heat_c, cool_c"
        " FROM thermostat_snapshot WHERE device_id = ? ORDER BY taken_at DESC LIMIT 1",
        (device,),
    ).fetchone()
    if row is None:
        return LevelState(level, device, None, None, None, "no snapshot yet"), None
    taken_at, online, ambient, humidity, mode, hvac, eco, heat, cool = row
    reading = conn.execute(
        "SELECT temp_c FROM indoor_reading WHERE device_id = ? ORDER BY at DESC LIMIT 1",
        (device,),
    ).fetchone()
    changed = _last_setpoint_change(conn, device, now - config.limits.manual_pause, level.key)
    skip = None
    if now - parse_utc(taken_at) > FRESH:
        skip = "no recent snapshot"
    elif online == 0:
        skip = "thermostat offline"
    elif eco:
        skip = "Eco is on"
    elif mode not in PLANNABLE_MODES:
        skip = f"mode is {mode or 'unknown'}" if mode != OFF else "system is off"
    elif changed is not None:
        local = changed.astimezone(zone).strftime("%H:%M")
        skip = f"setpoints changed at the thermostat at {local}, so it's paused"
    current = {
        "temp": _t(reading[0] if reading else ambient, unit),
        "humidity_pct": humidity,
        "mode": mode,
        "hvac": hvac,
        "heat_setpoint": _t(heat, unit) if mode in (HEAT, HEAT_COOL) else None,
        "cool_setpoint": _t(cool, unit) if mode in (COOL, HEAT_COOL) else None,
    }
    return LevelState(level, device, mode, heat, cool, skip), current


def _last_setpoint_change(
    conn, device: str, since: datetime, level: str | None = None
) -> datetime | None:
    """When a person last changed the setpoints after `since`, or None. A change to what the
    applier wrote for `level` (apply_step) within WRITE_MATCH of writing it was the applier's."""
    rows = conn.execute(
        "SELECT at, heat_c, cool_c FROM setpoint WHERE device_id = ? AND at >= ? ORDER BY at",
        (device, (since - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")),
    ).fetchall()
    written = _written(conn, level, since) if level else []
    last, changed = None, None
    for at, heat, cool in rows:
        value = (heat and round(heat, 1), cool and round(cool, 1))
        when = parse_utc(at)
        if last is not None and value != last and when >= since:
            ours = any(
                abs(when - w_at) <= WRITE_MATCH
                and (w_heat is None or heat is None or abs(w_heat - heat) <= 0.35)
                and (w_cool is None or cool is None or abs(w_cool - cool) <= 0.35)
                for w_at, w_heat, w_cool in written
            )
            if not ours:
                changed = when
        last = value
    return changed


def _written(
    conn, level: str, since: datetime
) -> list[tuple[datetime, float | None, float | None]]:
    """The applier's writes for `level` since a little before `since`."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'apply_step'"
    ).fetchone()
    if not exists:
        return []
    rows = conn.execute(
        "SELECT done_at, heat_c, cool_c FROM apply_step WHERE level = ? AND outcome = 'applied'"
        " AND done_at >= ?",
        (level, (since - WRITE_MATCH).strftime("%Y-%m-%dT%H:%M:%SZ")),
    ).fetchall()
    return [(parse_utc(at), heat, cool) for at, heat, cool in rows]


def _t(celsius: float | None, unit: str) -> float | None:
    return None if celsius is None else round(from_c(celsius, unit), 1)


def _d(celsius: float | None, unit: str, digits: int = 2) -> float | None:
    return None if celsius is None else round(delta_from_c(celsius, unit), digits)


def _band(band, unit: str) -> dict[str, Any]:
    out: dict[str, Any] = {"name": band.name}
    if band.start is not None:
        out["from"], out["to"] = band.start.strftime("%H:%M"), band.end.strftime("%H:%M")
    else:
        out["when"] = "whenever no other band applies"
    return {**out, "heat_at_least": _t(band.heat_c, unit), "cool_at_most": _t(band.cool_c, unit)}


def _model(m: house.Model, unit: str) -> dict[str, Any]:
    return {
        "heat_loss_per_hour_per_degree_of_difference": m.loss_per_h and round(m.loss_per_h, 4),
        "furnace_degrees_per_hour": _d(m.heat_c_per_h, unit, 1),
        "furnace_runs_measured": m.heat_runs,
        "ac_degrees_per_hour": _d(m.cool_c_per_h, unit, 1),
        "ac_runs_measured": m.cool_runs,
        "sun_degrees_per_hour_per_kw_m2": _d(m.sun_c_per_kwh, unit),
        "other_gains_degrees_per_hour": _d(m.other_c_per_h, unit),
        "hours_fitted": round(m.fit_hours),
        "typical_miss": _d(m.fit_rmse_c, unit),
        "outdoor_range_fitted": m.outdoor_c_range
        and [_t(m.outdoor_c_range[0], unit), _t(m.outdoor_c_range[1], unit)],
    }


def _recent(conn, device: str, now: datetime, zone: ZoneInfo) -> list[dict[str, Any]]:
    """Heating and cooling minutes and degree-days (base 65°F) per local day, last 7 days."""
    today = now.astimezone(zone).date()
    first = today - timedelta(days=RECENT_DAYS)
    start = datetime(first.year, first.month, first.day, tzinfo=zone).astimezone(now.tzinfo)
    rows = conn.execute(
        "SELECT d.date, w.hdd_f, w.cdd_f, h.heating_min, h.cooling_min"
        " FROM local_day d"
        " LEFT JOIN weather_daily w ON w.day = d.start_at"
        " LEFT JOIN hvac_daily h ON h.day = d.start_at AND h.device_id = ?"
        " WHERE d.start_at >= ? AND d.date < ? ORDER BY d.start_at",
        (device, start.strftime("%Y-%m-%dT%H:%M:%SZ"), today.isoformat()),
    ).fetchall()
    return [
        {
            "date": date,
            "heating_degree_days_f": hdd and round(hdd, 1),
            "cooling_degree_days_f": cdd and round(cdd, 1),
            "furnace_minutes": round(heating or 0),
            "ac_minutes": round(cooling or 0),
        }
        for date, hdd, cdd, heating, cooling in rows
        if hdd is not None
    ]


def _forecast(conn, now: datetime, unit: str) -> list[dict[str, Any]]:
    """Hourly, from the latest forecast, for the next 24 hours."""
    issued = conn.execute("SELECT max(issued_at) FROM weather_forecast").fetchone()[0]
    if issued is None:
        return []
    rows = conn.execute(
        "SELECT valid_at, temp_c, humidity_pct, cloud_pct, solar_wm2, wind_kmh, precip_mm"
        " FROM weather_forecast WHERE issued_at = ? AND valid_at >= ? AND valid_at <= ?"
        " ORDER BY valid_at",
        (
            issued,
            (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            (now + HORIZON).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    ).fetchall()
    return [
        {
            "at_utc": at,
            "temp": _t(temp, unit),
            "humidity_pct": humidity,
            "cloud_pct": cloud,
            "sun_w_m2": solar,
            "wind_kmh": wind,
            "precip_mm": precip,
        }
        for at, temp, humidity, cloud, solar, wind, precip in rows
    ]


def _feedback(config: Config, now: datetime, zone: ZoneInfo) -> list[dict[str, str]]:
    """Recent rows of the feedback CSV (an at_utc column, then anything: level, feeling,
    readings, a note), newest last. Notes are cut to FEEDBACK_NOTE_CHARS."""
    path = config.feedback_path
    if path is None or not path.exists():
        return []
    since = now - timedelta(days=FEEDBACK_DAYS)
    rows = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            try:
                at = parse_utc(row.get("at_utc", ""))
            except ValueError:
                continue
            if since <= at <= now:
                local = at.astimezone(zone).strftime("%Y-%m-%dT%H:%M %a")
                rest = {
                    k: (v or "")[:FEEDBACK_NOTE_CHARS]
                    for k, v in row.items()
                    if k not in (None, "at_utc")  # None holds a row's extra cells
                }
                rows.append({"at_local": local, **rest})
    return rows[-FEEDBACK_ROWS:]


def _notes(config: Config) -> str:
    path = config.notes_path
    if path is None or not path.exists():
        return ""
    return path.read_text()[:NOTES_CHARS]
