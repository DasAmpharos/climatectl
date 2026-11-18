"""Each level's house model, fitted from the logged data so the planner works from measured
numbers instead of estimating the physics from raw history. Per thermostat, in °C per hour:

    dT/dt = -loss * (T - T_outdoor) + furnace * heating + ac * cooling + sun * kW/m² + other

where heating and cooling are the share of the time the furnace and the AC ran. The numbers are
the ones that best predict the readings over the last DAYS days: from the reading at the start
of each stretch (up to a day, broken where the weather is missing), the equation is run forward
with the furnace and the AC as they really ran and the weather as observed, and the squared
misses against the readings are made as small as possible.

Fitting whole days makes the furnace and AC rates effective ones. A short run lifts the reading
much faster (about twice as fast in the house this was developed on) and the reading then
settles back, because most of that warmth goes into the walls and furniture. Only the effective
rate predicts how long the furnace runs over a day; the cost is that the model's recoveries come
out slower than the real ones.

`other` is everything steady the equation doesn't name: people, appliances, warmth from the
other level (positive) or air leaking out (negative).

A rate is None until enough data backs it: the furnace's and the AC's need MIN_RUNS runs, the
sun's MIN_SUN_KWH a day on average, and nothing is fitted from less than MIN_DAYS days."""

from __future__ import annotations

import math
import sqlite3
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..store import parse_utc
from ..thermostat import COOLING, HEATING

DAYS = 30
STEP = timedelta(minutes=15)
STRETCH = timedelta(hours=24)
MIN_STRETCH = timedelta(hours=6)
MAX_READING_AGE = timedelta(minutes=45)
MAX_WEATHER_GAP = timedelta(hours=3)
MIN_RUN = timedelta(minutes=5)
MIN_RUNS, MIN_DAYS = 3, 3
MIN_SUN_KWH = 1.0  # kWh/m² a day, on average, before the sun's rate is fitted
MAX_ITERATIONS = 2000

H = STEP.total_seconds() / 3600


@dataclass(frozen=True)
class Model:
    loss_per_h: float | None  # °C per hour per °C of indoor-outdoor difference
    heat_c_per_h: float | None
    heat_runs: int
    cool_c_per_h: float | None  # negative
    cool_runs: int
    sun_c_per_kwh: float | None  # °C per hour per kW/m² of sun
    other_c_per_h: float | None
    fit_hours: float
    fit_rmse_c: float | None  # the typical miss of the fitted model against the readings
    outdoor_c_range: tuple[float, float] | None  # the outdoor temperatures it was fitted over


# One step: the reading (None if none is fresh), outdoor °C, sun kW/m², and the share of the step
# the furnace and the AC ran.
Step = tuple[float | None, float, float, float, float]


class _Series:
    """Values in time order, with the latest one at or before a time."""

    def __init__(self, rows: list[tuple[datetime, float]]) -> None:
        self.times = [t for t, _ in rows]
        self.values = [v for _, v in rows]

    def at(self, when: datetime, max_age: timedelta) -> float | None:
        i = bisect_right(self.times, when)
        if i == 0 or when - self.times[i - 1] > max_age:
            return None
        return self.values[i - 1]


def fit(
    conn: sqlite3.Connection, device_id: str, now: datetime, zone: ZoneInfo, days: int = DAYS
) -> Model:
    """`zone` is unused; kept so callers don't change if the fit ever needs local days."""
    since = now - timedelta(days=days)
    span = (since.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    indoor = _Series(
        [
            (parse_utc(at), t)
            for at, t in conn.execute(
                "SELECT at, temp_c FROM indoor_reading WHERE device_id = ? AND at >= ?"
                " AND at <= ? ORDER BY at",
                (device_id, *span),
            )
        ]
    )
    runs = [
        (status, max(parse_utc(start), since), min(parse_utc(end) if end else now, now))
        for status, start, end in conn.execute(
            "SELECT status, start_at, end_at FROM hvac_run WHERE device_id = ?"
            " AND status IN (?, ?) AND (end_at IS NULL OR end_at >= ?) AND start_at <= ?"
            " ORDER BY start_at",
            (device_id, HEATING, COOLING, *span),
        )
    ]
    weather = [
        (parse_utc(at), t, (s or 0.0) / 1000)
        for at, t, s in conn.execute(
            "SELECT observed_at, temp_c, solar_wm2 FROM weather_observation"
            " WHERE temp_c IS NOT NULL AND observed_at >= ? AND observed_at <= ?"
            " ORDER BY observed_at",
            (
                (since - MAX_WEATHER_GAP).strftime("%Y-%m-%dT%H:%M:%SZ"),
                (now + MAX_WEATHER_GAP).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        )
    ]
    heat_runs = sum(1 for s, a, b in runs if s == HEATING and b - a >= MIN_RUN)
    cool_runs = sum(1 for s, a, b in runs if s == COOLING and b - a >= MIN_RUN)
    fit_heat, fit_cool = heat_runs >= MIN_RUNS, cool_runs >= MIN_RUNS

    stretches = [
        s
        for s in _stretches(since, now, indoor, weather, runs)
        # Equipment too rarely run to fit would leave its warmth unexplained.
        if (fit_heat or not any(step[3] for step in s))
        and (fit_cool or not any(step[4] for step in s))
    ]
    hours = sum(len(s) for s in stretches) * H
    if hours < MIN_DAYS * 24:
        return Model(None, None, heat_runs, None, cool_runs, None, None, hours, None, None)
    fit_sun = sum(step[2] for s in stretches for step in s) * H / (hours / 24) >= MIN_SUN_KWH

    # The free numbers, each with its start and how it maps to the equation: rates that can't
    # be negative are fitted as logarithms.
    free: list[tuple[str, float, Callable[[float], float]]] = [
        ("loss", math.log(0.02), math.exp),
        ("other", 0.0, float),
    ]
    if fit_heat:
        free.append(("heat", math.log(2.0), math.exp))
    if fit_cool:
        free.append(("cool", math.log(2.0), math.exp))
    if fit_sun:
        free.append(("sun", math.log(0.3), math.exp))

    def numbers(x: list[float]) -> dict[str, float]:
        return {"heat": 0.0, "cool": 0.0, "sun": 0.0} | {
            name: to(v) for (name, _, to), v in zip(free, x, strict=True)
        }

    best, error = _minimize(lambda x: _error(numbers(x), stretches), [f[1] for f in free])
    n = numbers(best)
    outside = [step[1] for s in stretches for step in s]
    return Model(
        loss_per_h=n["loss"],
        heat_c_per_h=n["heat"] if fit_heat else None,
        heat_runs=heat_runs,
        cool_c_per_h=-n["cool"] if fit_cool else None,
        cool_runs=cool_runs,
        sun_c_per_kwh=n["sun"] if fit_sun else None,
        other_c_per_h=n["other"],
        fit_hours=hours,
        fit_rmse_c=math.sqrt(error),
        outdoor_c_range=(min(outside), max(outside)),
    )


def _stretches(since, now, indoor: _Series, weather, runs) -> list[list[Step]]:
    """Steps from `since` to `now`, in stretches of up to STRETCH that each start at a reading.
    A stretch ends early where the weather is missing; shorter than MIN_STRETCH, it's dropped."""
    times = [t for t, _, _ in weather]
    out: list[list[Step]] = []
    current: list[Step] = []
    started = None
    at = since
    while at + STEP <= now:
        step = _step(at, indoor, weather, times, runs)
        if step is None or (current and at - started >= STRETCH):
            if current and len(current) * STEP >= MIN_STRETCH:
                out.append(current)
            current = []
        if step is not None and (current or step[0] is not None):
            if not current:
                started = at
            current.append(step)
        at += STEP
    if len(current) * STEP >= MIN_STRETCH:
        out.append(current)
    return out


def _step(at, indoor: _Series, weather, times, runs) -> Step | None:
    middle = at + STEP / 2
    i = bisect_right(times, middle)
    if i == 0 or i == len(times):
        return None
    (t0, out0, sun0), (t1, out1, sun1) = weather[i - 1], weather[i]
    if t1 - t0 > MAX_WEATHER_GAP:
        return None
    f = (middle - t0) / (t1 - t0)
    shares = {HEATING: 0.0, COOLING: 0.0}
    for status, start, end in runs:
        if start >= at + STEP:
            break
        overlap = (min(end, at + STEP) - max(start, at)) / STEP
        if overlap > 0:
            shares[status] += overlap
    return (
        indoor.at(at, MAX_READING_AGE),
        out0 + f * (out1 - out0),
        sun0 + f * (sun1 - sun0),
        min(1.0, shares[HEATING]),
        min(1.0, shares[COOLING]),
    )


def _error(n: dict[str, float], stretches: list[list[Step]]) -> float:
    """The mean squared miss of the readings, running each stretch forward from its first."""
    loss, heat, cool, sun, other = n["loss"], n["heat"], n["cool"], n["sun"], n["other"]
    total, count = 0.0, 0
    for stretch in stretches:
        temp = stretch[0][0]
        for reading, outside, kw, heating, cooling in stretch:
            if reading is not None:
                total += (temp - reading) ** 2
                count += 1
            temp += (-loss * (temp - outside) + heat * heating - cool * cooling + sun * kw
                     + other) * H  # fmt: skip
    return total / count


def _minimize(f: Callable[[list[float]], float], x0: list[float]) -> tuple[list[float], float]:
    """Nelder-Mead, from x0 with a step of 0.3 in each direction."""
    n = len(x0)
    points = [list(x0)] + [
        [x + (0.3 if i == j else 0.0) for j, x in enumerate(x0)] for i in range(n)
    ]
    values = [f(p) for p in points]
    for _ in range(MAX_ITERATIONS):
        order = sorted(range(n + 1), key=values.__getitem__)
        points, values = [points[i] for i in order], [values[i] for i in order]
        if values[-1] - values[0] < 1e-10 * max(1.0, values[0]):
            break
        centre = [sum(p[i] for p in points[:-1]) / n for i in range(n)]
        worst = points[-1]
        reflected = _toward(centre, worst, 1.0)
        fr = f(reflected)
        if fr < values[0]:
            expanded = _toward(centre, worst, 2.0)
            fe = f(expanded)
            points[-1], values[-1] = (expanded, fe) if fe < fr else (reflected, fr)
        elif fr < values[-2]:
            points[-1], values[-1] = reflected, fr
        else:
            contracted = _toward(centre, worst, -0.5)
            fc = f(contracted)
            if fc < values[-1]:
                points[-1], values[-1] = contracted, fc
            else:
                best = points[0]
                points = [best] + [[b + 0.5 * (x - b) for b, x in zip(best, p, strict=True)]
                                   for p in points[1:]]  # fmt: skip
                values = [values[0]] + [f(p) for p in points[1:]]
    i = min(range(n + 1), key=values.__getitem__)
    return points[i], values[i]


def _toward(centre: list[float], worst: list[float], scale: float) -> list[float]:
    """The point `scale` times the worst point's distance past the centre, away from it."""
    return [c + scale * (c - w) for c, w in zip(centre, worst, strict=True)]
