"""Each level's house model, fitted from the logged data so the planner applies measured numbers
instead of estimating the physics from raw history. Per thermostat, over the last DAYS days:

- heat loss: °C per hour per °C of indoor-outdoor difference, from nights (22:00-06:00 local)
  with the HVAC off until its first call. The difference drives the loss, so one number covers
  mild and cold nights.
- heating and cooling rates: the median °C per hour of the furnace's and the AC's runs. Each
  run's end temperature is the highest (heating) or lowest (cooling) reading up to 20 minutes
  after it stops, because the thermostat reports the change after the equipment stops.
- sun: the median rise from 08:00 to the afternoon peak per kWh/m² of sun, on days with no HVAC
  call between 08:00 and 18:00. Rough: the outdoor warm-up and people at home are in it too.

Each number is None until enough runs, nights or days back it. Nothing here is backtested yet."""

from __future__ import annotations

import sqlite3
import statistics
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..store import parse_utc
from ..thermostat import CALLING, COOLING, HEATING

DAYS = 30
NIGHT_START, NIGHT_HOURS, MIN_NIGHT_HOURS = 22, 8, 4
MIN_GAP_C = 3 * 5 / 9  # nights closer to the outdoor temperature say little about heat loss
MIN_RUN = timedelta(minutes=5)
AFTER_RUN = timedelta(minutes=20)
MAX_READING_AGE = timedelta(hours=3)
DAY_START, DAY_HOURS = 8, 10
MIN_SUN_KWH = 1.0
MIN_NIGHTS, MIN_RUNS, MIN_DAYS = 2, 3, 3


@dataclass(frozen=True)
class Model:
    loss_per_h: float | None  # °C per hour per °C of indoor-outdoor difference
    loss_nights: int
    outdoor_c_range: tuple[float, float] | None  # outdoor temperatures the nights covered
    heat_c_per_h: float | None
    heat_runs: int
    cool_c_per_h: float | None  # negative
    cool_runs: int
    sun_c_per_kwh: float | None  # °C per kWh/m² of sun between 08:00 and 18:00
    sun_days: int


@dataclass(frozen=True)
class _Run:
    status: str
    start: datetime
    end: datetime | None


class _Series:
    """Readings in time order, with the latest one at or before a time."""

    def __init__(self, rows: list[tuple[datetime, float]]) -> None:
        self.times = [t for t, _ in rows]
        self.values = [v for _, v in rows]

    def at(self, when: datetime) -> float | None:
        i = bisect_right(self.times, when)
        if i == 0 or when - self.times[i - 1] > MAX_READING_AGE:
            return None
        return self.values[i - 1]

    def between(self, start: datetime, end: datetime) -> list[float]:
        """Readings with start < time <= end."""
        return self.values[bisect_right(self.times, start) : bisect_right(self.times, end)]


def fit(
    conn: sqlite3.Connection, device_id: str, now: datetime, zone: ZoneInfo, days: int = DAYS
) -> Model:
    since = now - timedelta(days=days)
    stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    indoor = _Series(
        [
            (parse_utc(at), t)
            for at, t in conn.execute(
                "SELECT at, temp_c FROM indoor_reading WHERE device_id = ? AND at >= ? ORDER BY at",
                (device_id, stamp),
            )
        ]
    )
    runs = [
        _Run(status, parse_utc(start), end and parse_utc(end))
        for status, start, end in conn.execute(
            "SELECT status, start_at, end_at FROM hvac_run WHERE device_id = ?"
            " AND (end_at IS NULL OR end_at >= ?) ORDER BY start_at",
            (device_id, stamp),
        )
    ]
    weather = conn.execute(
        "SELECT observed_at, temp_c, solar_wm2 FROM weather_observation WHERE observed_at >= ?"
        " ORDER BY observed_at",
        (stamp,),
    ).fetchall()
    outdoor = _Series([(parse_utc(at), t) for at, t, _ in weather if t is not None])
    sun = [(parse_utc(at), s or 0.0) for at, _, s in weather]

    dates = []
    day = since.astimezone(zone).date()
    while day <= now.astimezone(zone).date():
        dates.append(day)
        day += timedelta(days=1)

    loss, nights, outdoor_range = _heat_loss(dates, zone, now, indoor, outdoor, runs)
    heat, heat_runs = _rate(HEATING, runs, indoor)
    cool, cool_runs = _rate(COOLING, runs, indoor)
    sun_gain, sun_days = _sun(dates, zone, now, indoor, sun, runs)
    return Model(loss, nights, outdoor_range, heat, heat_runs, cool, cool_runs, sun_gain, sun_days)


def _calls(runs: list[_Run], start: datetime, end: datetime) -> list[_Run]:
    return [
        r
        for r in runs
        if r.status in CALLING and r.start < end and (r.end is None or r.end > start)
    ]


def _heat_loss(dates, zone, now, indoor, outdoor, runs):
    nights = []  # (indoor-outdoor difference, °C per hour), both from the same stretch
    for day in dates:
        start = datetime(day.year, day.month, day.day, NIGHT_START, tzinfo=zone)
        end = start + timedelta(hours=NIGHT_HOURS)
        if end > now:
            continue
        calls = _calls(runs, start, end)
        if any(r.start <= start for r in calls):
            continue  # already heating or cooling when the night began
        end = min([r.start for r in calls], default=end)
        hours = (end - start).total_seconds() / 3600
        first, last = indoor.at(start), indoor.at(end)
        outside = outdoor.between(start, end)
        if hours < MIN_NIGHT_HOURS or first is None or last is None or not outside:
            continue
        gap = (first + last) / 2 - statistics.mean(outside)
        if gap > MIN_GAP_C:
            nights.append((gap, (last - first) / hours, min(outside), max(outside)))
    if len(nights) < MIN_NIGHTS:
        return None, len(nights), None
    # Least squares through the origin: rate = -loss * difference.
    loss = -sum(g * r for g, r, _, _ in nights) / sum(g * g for g, _, _, _ in nights)
    return loss, len(nights), (min(n[2] for n in nights), max(n[3] for n in nights))


def _rate(status: str, runs: list[_Run], indoor: _Series) -> tuple[float | None, int]:
    rates = []
    for r in runs:
        if r.status != status or r.end is None or r.end - r.start < MIN_RUN:
            continue
        first = indoor.at(r.start)
        after = indoor.between(r.start, r.end + AFTER_RUN)
        if first is None or not after:
            continue
        last = max(after) if status == HEATING else min(after)
        rates.append((last - first) / ((r.end - r.start).total_seconds() / 3600))
    if len(rates) < MIN_RUNS:
        return None, len(rates)
    return statistics.median(rates), len(rates)


def _sun(dates, zone, now, indoor, sun, runs):
    gains = []
    for day in dates:
        start = datetime(day.year, day.month, day.day, DAY_START, tzinfo=zone)
        end = start + timedelta(hours=DAY_HOURS)
        if end > now or _calls(runs, start, end):
            continue
        kwh = sum(s for at, s in sun if start <= at < end) * 0.25 / 1000  # 15-minute readings
        first, readings = indoor.at(start), indoor.between(start, end)
        if kwh >= MIN_SUN_KWH and first is not None and readings:
            gains.append((max(readings) - first) / kwh)
    if len(gains) < MIN_DAYS:
        return None, len(gains)
    return statistics.median(gains), len(gains)
