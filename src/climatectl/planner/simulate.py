"""Runs a plan through each level's house model to score it: the indoor temperature every STEP
for 24 hours, how long the furnace and AC run, and how long the room is outside its comfort band.

Per level, in °C per hour:

    dT/dt = -loss * (T - T_outdoor) + sun * W/m² / 1000 + the furnace or the AC

The thermostat is an ideal controller: the furnace runs whenever the room would fall below the
heat setpoint, for just the share of each step that holds it there, and the AC likewise above
the cool setpoint. A real thermostat cycles around its setpoint (about 1F), so minute-to-minute
temperatures differ; the totals are what's compared.

It works from what the planner was sent (plan_run.input_json), so a stored plan can be scored
again at any time against the same start, the same house model and the same forecast.

Approximations, so scores compare plans with each other rather than predict to the minute:
- The furnace and AC rates are measured with the heat loss of those runs included and are
  used as gross rates, so recoveries come out a little slow (about 10% in the cold).
- The sun term is a net daily rise spread over the day by the forecast's sunshine.
- People, cooking and warmth moving between levels aren't modelled.
- A level whose model lacks a number it needs says so in `notes`; that term is left out.
Nothing here is backtested yet."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..store import parse_utc
from ..thermostat import COOL, HEAT
from .config import Band, delta_c, to_c

STEP = timedelta(minutes=5)
HORIZON = timedelta(hours=24)
TOLERANCE_C = 0.05  # numerical slack before a room counts as outside its band


@dataclass
class Score:
    level: str
    outside_minutes: float = 0.0  # below or above the band
    below_degree_minutes: float = 0.0  # in the payload's unit
    above_degree_minutes: float = 0.0
    heat_minutes: float = 0.0
    cool_minutes: float = 0.0
    low_c: float | None = None
    high_c: float | None = None
    worst_miss: float = 0.0  # the largest gap outside the band, in the payload's unit
    worst_at: datetime | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Model:
    loss: float
    heat: float | None  # °C per hour while the furnace runs
    cool: float | None  # °C per hour while the AC runs (positive here)
    sun: float  # °C per kWh/m²


def score(
    payload: dict[str, Any], plan: list[dict[str, Any]], now: datetime, zone: ZoneInfo
) -> dict[str, Score]:
    """Scores per level. `plan` is in plan_run.plan_json's shape: per level, changes with UTC
    times and Celsius setpoints. A level with no entry keeps its current setpoints."""
    unit = payload["unit"]
    weather = _weather(payload.get("forecast") or [], unit)
    by_level = {p["level"]: p for p in plan}
    scores = {}
    for level in payload["levels"]:
        changes = (by_level.get(level["level"]) or {}).get("changes") or []
        scores[level["level"]] = _level(level, changes, weather, now, zone, unit)
    return scores


def baselines(payload: dict[str, Any], now: datetime, zone: ZoneInfo) -> dict[str, list]:
    """Plans to compare a model's plan against: "hold" keeps the current setpoints all day (the
    no-AI baseline); "band edges" sets each level to its band's edges whenever the band
    changes, with no pre-heating or pre-cooling."""
    unit = payload["unit"]
    edges = []
    for level in payload["levels"]:
        bands = _bands(level, unit)
        changes, last = [], None
        at = now
        while at < now + HORIZON:
            band = _band_at(bands, at.astimezone(zone).time())
            if band is not last:
                changes.append({"at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "heat_c": band.heat_c, "cool_c": band.cool_c})  # fmt: skip
                last = band
            at += STEP
        edges.append({"level": level["level"], "changes": changes})
    return {"hold": [], "band edges": edges}


def _level(level, changes, weather, now, zone, unit) -> Score:
    out = Score(level["level"])
    current = level.get("now") or {}
    model = _model(level.get("house_model") or {}, unit, out)
    bands = _bands(level, unit)
    temp = current.get("temp")
    if temp is None or not weather:
        out.notes.append("no starting temperature" if temp is None else "no forecast")
        return out
    temp_c = to_c(temp, unit)
    mode = current.get("mode")
    heat_sp = _c(current.get("heat_setpoint"), unit)
    cool_sp = _c(current.get("cool_setpoint"), unit)
    schedule = sorted((parse_utc(c["at"]), c.get("heat_c"), c.get("cool_c")) for c in changes)
    hours = STEP.total_seconds() / 3600
    minutes = STEP.total_seconds() / 60
    out.low_c = out.high_c = temp_c
    at = now
    while at < now + HORIZON:
        while schedule and schedule[0][0] <= at:
            _, heat_sp, cool_sp = schedule.pop(0)
        if mode == COOL:
            heat_sp = None
        elif mode == HEAT:
            cool_sp = None
        outdoor, sun = _at(weather, at + STEP / 2)
        temp_c += (-model.loss * (temp_c - outdoor) + model.sun * sun / 1000) * hours
        if heat_sp is not None and temp_c < heat_sp and model.heat:
            share = min(1.0, (heat_sp - temp_c) / (model.heat * hours))
            temp_c += share * model.heat * hours
            out.heat_minutes += share * minutes
        elif cool_sp is not None and temp_c > cool_sp and model.cool:
            share = min(1.0, (temp_c - cool_sp) / (model.cool * hours))
            temp_c -= share * model.cool * hours
            out.cool_minutes += share * minutes
        at += STEP
        out.low_c, out.high_c = min(out.low_c, temp_c), max(out.high_c, temp_c)
        band = _band_at(bands, at.astimezone(zone).time())
        below, above = band.heat_c - temp_c, temp_c - band.cool_c
        for gap, attr in ((below, "below_degree_minutes"), (above, "above_degree_minutes")):
            if gap > TOLERANCE_C:
                miss = gap if unit == "C" else gap * 9 / 5
                setattr(out, attr, getattr(out, attr) + miss * minutes)
                out.outside_minutes += minutes
                if miss > out.worst_miss:
                    out.worst_miss, out.worst_at = miss, at
    return out


def _model(raw: dict[str, Any], unit: str, out: Score) -> _Model:
    def rate(key: str, what: str) -> float | None:
        value = raw.get(key)
        if value is None:
            out.notes.append(f"no {what} rate measured: {what} not simulated")
            return None
        return abs(delta_c(value, unit))

    loss = raw.get("heat_loss_per_hour_per_degree_of_difference")
    if loss is None:
        out.notes.append("no heat-loss rate measured: drift not simulated")
    sun = raw.get("sunny_day_rise_per_kwh_m2")
    return _Model(
        loss=loss or 0.0,
        heat=rate("furnace_degrees_per_hour", "furnace"),
        cool=rate("ac_degrees_per_hour", "AC"),
        sun=delta_c(sun, unit) if sun else 0.0,
    )


def _bands(level: dict[str, Any], unit: str) -> list[Band]:
    bands = []
    for b in level.get("comfort_bands") or []:
        start = b.get("from") and time.fromisoformat(b["from"])
        end = b.get("to") and time.fromisoformat(b["to"])
        heat, cool = to_c(b["heat_at_least"], unit), to_c(b["cool_at_most"], unit)
        bands.append(Band(b.get("name", ""), heat, cool, start or None, end or None))
    return bands


def _band_at(bands: list[Band], at: time) -> Band:
    for band in bands:
        if band.start is not None and band.covers(at):
            return band
    return next(b for b in bands if b.start is None)


def _weather(forecast: list[dict[str, Any]], unit: str) -> list[tuple[datetime, float, float]]:
    return [
        (parse_utc(h["at_utc"]), to_c(h["temp"], unit), h.get("sun_w_m2") or 0.0)
        for h in forecast
        if h.get("temp") is not None
    ]


def _at(weather, when: datetime) -> tuple[float, float]:
    """Outdoor temperature and sun at `when`, linear between forecast hours."""
    if when <= weather[0][0]:
        return weather[0][1], weather[0][2]
    for (t0, a0, s0), (t1, a1, s1) in zip(weather, weather[1:], strict=False):
        if t0 <= when <= t1:
            f = (when - t0) / (t1 - t0)
            return a0 + f * (a1 - a0), s0 + f * (s1 - s0)
    return weather[-1][1], weather[-1][2]


def _c(value: float | None, unit: str) -> float | None:
    return None if value is None else to_c(value, unit)
