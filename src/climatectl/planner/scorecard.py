"""The trial's scorecard: does the planner's schedule save energy without costing comfort?

Every trial day (see config.Trial: from day_starts to day_starts, named by the date it starts)
gets, per level, its arm, the furnace and AC minutes from the logged runs, the outdoor
temperature, how long the thermostat's reading was really outside the comfort band (by more
than the trial's tolerance, since thermostats cycle around a setpoint), the lowest reading, and
how long a baseline or hold day spent away from the hold setpoints.

Weather is the main noise, so energy is compared through a fit made on the days at the hold
setpoints (baseline and hold days): furnace minutes = slope * degree-days + intercept, with the
degree-day base temperature chosen to fit best. Each day's residual is what it used against
what that fit expects for its weather. The saving is the mean residual of the hold days minus
that of the planned days, which share the same weeks of weather because the arms alternate, and
its 95% interval comes from the spread of both. Before the trial, the fit's spread gives the
smallest saving the trial could detect.

The simulator's accuracy is checked too: each stored plan's "hold the setpoints" baseline
predicts the next 24 hours' furnace minutes, which can be compared with what happened while the
level really held those setpoints.

The days are saved in `scorecard_day` (recomputed each run) for dashboards."""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from statistics import fmean, stdev
from zoneinfo import ZoneInfo

from ..store import parse_utc
from .apply import NOT_APPLIED
from .config import Config, Trial, from_c

SAMPLE = timedelta(minutes=5)
BASES_C = [b / 2 for b in range(20, 37)]  # degree-day bases tried: 10-18 °C (50-64.4 °F)
MIN_FIT_DAYS = 6
OFF_HOLD_LIMIT_MIN = 60  # a baseline or hold day further from the hold setpoints is left out
MIN_WEATHER_HOURS = 18  # of 24, observed or forecast
HEATING_DAY_MIN = 30  # a day the fit expects at least this much furnace is a heating day
# Two-sided 95% t values by degrees of freedom; 2.0 past the table.
T95 = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26,
       10: 2.23, 12: 2.18, 15: 2.13, 20: 2.09, 25: 2.06, 30: 2.04}  # fmt: skip

SCHEMA = """
CREATE TABLE IF NOT EXISTS scorecard_day (
    level         TEXT NOT NULL,
    day           TEXT NOT NULL,  -- the local date the trial day starts
    arm           TEXT NOT NULL,  -- baseline, planned, hold, skipped, after
    start_at      TEXT NOT NULL,
    end_at        TEXT NOT NULL,
    outdoor_c     REAL,
    degree_days_c REAL,           -- below the fitted base
    expected_min  REAL,           -- furnace minutes the hold fit expects for the weather
    heating_min   REAL NOT NULL,
    cooling_min   REAL NOT NULL,
    outside_min   REAL NOT NULL,  -- the reading past the band by more than the tolerance
    low_c         REAL,
    off_hold_min  REAL NOT NULL,
    used          INTEGER NOT NULL,  -- 1 if it counts in the fit or the comparison
    notes         TEXT NOT NULL,
    PRIMARY KEY (level, day)
);
"""


@dataclass
class Day:
    level: str
    day: date
    arm: str
    start: datetime
    end: datetime
    hourly_c: list[float]  # outdoor, one per hour with data
    heating_min: float
    cooling_min: float
    outside_min: float
    low_c: float | None
    off_hold_min: float
    notes: list[str] = field(default_factory=list)
    degree_days_c: float | None = None
    expected_min: float | None = None

    @property
    def outdoor_c(self) -> float | None:
        return fmean(self.hourly_c) if self.hourly_c else None

    @property
    def used(self) -> bool:
        return not self.notes and self.arm in ("baseline", "planned", "hold")


@dataclass
class Fit:
    base_c: float
    slope: float  # minutes per °C-day
    intercept: float
    r2: float
    sd: float  # residual standard deviation, minutes a day
    n: int

    def expect(self, hourly_c: list[float]) -> tuple[float, float]:
        dd = degree_days(hourly_c, self.base_c)
        return dd, max(0.0, self.intercept + self.slope * dd)


@dataclass
class Effect:
    planned: int
    hold: int
    saved_min: float  # a day; positive means the plan used less
    interval_min: float | None  # ± for 95%
    saved_pct: float | None
    interval_pct: float | None


@dataclass
class LevelCard:
    level: str
    days: list[Day]
    fit: Fit | None
    effect: Effect | None
    detectable_min: float | None  # the smallest saving a day the whole trial could detect
    heating_day_min: float | None  # the fit's mean expectation on heating days so far
    simulator: list[tuple[datetime, float, float]]  # (from, predicted, actual) furnace minutes


def degree_days(hourly_c: list[float], base_c: float) -> float:
    """Degree-days below base, scaled to 24 hours from the hours there are."""
    if not hourly_c:
        return 0.0
    return sum(max(0.0, base_c - t) for t in hourly_c) / len(hourly_c)


def trial_days(trial: Trial, zone: ZoneInfo, first: date, last: date):
    day = first
    while day <= last:
        start = datetime.combine(day, trial.day_starts, tzinfo=zone).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=1), trial.day_starts, tzinfo=zone)
        yield day, start, end.astimezone(UTC)
        day += timedelta(days=1)


def build(conn: sqlite3.Connection, cfg: Config, zone: ZoneInfo, now: datetime) -> list[LevelCard]:
    trial = cfg.trial
    if trial is None:
        raise ValueError("no trial in the planner config")
    devices = {name: dev for dev, name in conn.execute("SELECT device_id, name FROM thermostat")}
    first_data, last_data = conn.execute("SELECT min(at), max(at) FROM indoor_reading").fetchone()
    if first_data is None:
        return []
    first = trial.baseline_from or parse_utc(first_data).astimezone(zone).date()
    # Up to the trial's end (plus a day, to show what follows), the data, and now.
    last_day = (parse_utc(last_data).astimezone(zone) - _since_midnight(trial)).date()
    last = min(now.astimezone(zone).date(), trial.end + timedelta(days=1), last_day)
    weather = _hourly_weather(conn)
    cards = []
    for level in cfg.levels:
        device = devices.get(level.key, level.key)
        days = []
        for day, start, end in trial_days(trial, zone, first, last):
            if end > now:
                break  # not over yet
            days.append(_day(conn, cfg, trial, level, device, day, start, end, weather, zone))
        fit = _fit([d for d in days if d.used and d.arm in ("baseline", "hold")])
        for d in days:
            if fit and len(d.hourly_c) >= MIN_WEATHER_HOURS:
                d.degree_days_c, d.expected_min = fit.expect(d.hourly_c)
        heating = [d.expected_min for d in days
                   if d.used and (d.expected_min or 0) >= HEATING_DAY_MIN]  # fmt: skip
        cards.append(LevelCard(
            level.key, days, fit, _effect(days, fit), _detectable(fit, trial, level.key),
            fmean(heating) if heating else None,
            _simulator(conn, level.key, device, trial, zone, now),
        ))  # fmt: skip
    return cards


def _hourly_weather(conn) -> dict[datetime, float]:
    """Outdoor °C by UTC hour: observations, with gaps filled from the latest forecast issued
    before that hour (the logger can be down; the weather isn't)."""
    hours: dict[datetime, list[float]] = {}
    for at, temp in conn.execute(
        "SELECT observed_at, temp_c FROM weather_observation WHERE temp_c IS NOT NULL"
    ):
        hour = parse_utc(at).replace(minute=0, second=0)
        hours.setdefault(hour, []).append(temp)
    out = {h: fmean(v) for h, v in hours.items()}
    for valid, temp in conn.execute(
        "SELECT valid_at, temp_c FROM weather_forecast f WHERE temp_c IS NOT NULL AND issued_at ="
        " (SELECT max(issued_at) FROM weather_forecast g WHERE g.valid_at = f.valid_at"
        "  AND g.issued_at <= f.valid_at)"
    ):
        out.setdefault(parse_utc(valid).replace(minute=0, second=0), temp)
    return out


def _minutes_between(conn, device, status, start, end) -> float:
    total = 0.0
    for a, b in conn.execute(
        "SELECT start_at, end_at FROM hvac_run WHERE device_id = ? AND status = ?"
        " AND start_at < ? AND (end_at IS NULL OR end_at > ?)",
        (device, status, _iso(end), _iso(start)),
    ):
        a, b = max(parse_utc(a), start), min(parse_utc(b) if b else end, end)
        total += max(0.0, (b - a).total_seconds() / 60)
    return total


def _steps(conn, sql, device, start, end):
    """(at, values...) rows of a step series from just before start to end."""
    before = conn.execute(sql.format(where="at <= ? ORDER BY at DESC LIMIT 1"),
                          (device, _iso(start))).fetchall()  # fmt: skip
    during = conn.execute(sql.format(where="at > ? AND at < ? ORDER BY at"),
                          (device, _iso(start), _iso(end))).fetchall()  # fmt: skip
    return [(parse_utc(r[0]), *r[1:]) for r in before + during]


def _day(conn, cfg, trial, level, device, day, start, end, weather, zone) -> Day:
    hourly = [weather[h] for h in _hours(start, end) if h in weather]
    readings = _steps(conn, "SELECT at, temp_c FROM indoor_reading WHERE device_id = ? AND {where}",
                      device, start, end)  # fmt: skip
    setpoints = _steps(conn, "SELECT at, heat_c, cool_c FROM setpoint WHERE device_id = ?"
                       " AND {where}", device, start, end)  # fmt: skip
    minutes = SAMPLE.total_seconds() / 60
    outside = off_hold = 0.0
    low = None
    seen = False
    at = start
    while at < end:
        temp = _value_at(readings, at)
        if temp is not None:
            seen = True
            low = temp if low is None else min(low, temp)
            band = level.band_at(at.astimezone(zone).time())
            if temp < band.heat_c - trial.tolerance_c or temp > band.cool_c + trial.tolerance_c:
                outside += minutes
        sp = _value_at(setpoints, at, 2)
        if sp and sp[0] is not None and abs(sp[0] - trial.hold_heat_c) > 0.3:
            off_hold += minutes
        at += SAMPLE
    d = Day(level.key, day, trial.arm(level.key, day), start, end, hourly,
            _minutes_between(conn, device, "heating", start, end),
            _minutes_between(conn, device, "cooling", start, end),
            outside, low, off_hold)  # fmt: skip
    if not seen:
        d.notes.append("no readings")
    if len(hourly) < MIN_WEATHER_HOURS:
        d.notes.append(f"weather for {len(hourly)} of 24 hours")
    if d.arm in ("baseline", "hold") and off_hold > OFF_HOLD_LIMIT_MIN:
        d.notes.append(f"{off_hold:.0f} min away from the hold heat setpoint")
    if d.arm in ("planned", "hold"):
        d.notes.extend(_not_applied(conn, level.key, day))
    return d


def _not_applied(conn, level: str, day: date) -> list[str]:
    """Why the applier (apply.py) didn't keep this day to its arm, if it didn't. Days from before
    the applier, or applied by hand, have no rows and pass."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'apply_step'"
    ).fetchone():
        return []
    rows = conn.execute(
        "SELECT outcome, note FROM apply_step WHERE level = ? AND trial_day = ?"
        f" AND outcome IN ({', '.join('?' * len(NOT_APPLIED))}) ORDER BY due_at",
        (level, day.isoformat(), *NOT_APPLIED),
    ).fetchall()
    return [f"{outcome}: {note}" for outcome, note in rows]


def _hours(start, end):
    at = start.replace(minute=0, second=0, microsecond=0)
    while at < end:
        if at >= start - timedelta(minutes=59):
            yield at
        at += timedelta(hours=1)


def _value_at(steps, at, width=1):
    value = None
    for row in steps:
        if row[0] > at:
            break
        value = row[1] if width == 1 else row[1:]
    return value


def _fit(days: list[Day]) -> Fit | None:
    days = [d for d in days if len(d.hourly_c) >= MIN_WEATHER_HOURS]
    if len(days) < MIN_FIT_DAYS:
        return None
    y = [d.heating_min for d in days]
    best = None
    for base in BASES_C:
        x = [degree_days(d.hourly_c, base) for d in days]
        mx, my = fmean(x), fmean(y)
        sxx = sum((a - mx) ** 2 for a in x)
        if sxx < 1e-9:
            continue
        slope = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / sxx
        intercept = my - slope * mx
        sse = sum((b - (intercept + slope * a)) ** 2 for a, b in zip(x, y, strict=True))
        sst = sum((b - my) ** 2 for b in y) or 1e-9
        if best is None or sse < best[0]:
            best = (sse, base, slope, intercept, 1 - sse / sst)
    if best is None:
        return None
    sse, base, slope, intercept, r2 = best
    return Fit(base, slope, intercept, r2, math.sqrt(sse / max(1, len(days) - 2)), len(days))


def _t95(df: int) -> float:
    keys = [k for k in T95 if k <= df]
    return T95[max(keys)] if keys else T95[1]


def _effect(days: list[Day], fit: Fit | None) -> Effect | None:
    if fit is None:
        return None
    trial = [d for d in days if d.used and d.expected_min is not None]
    planned = [d.heating_min - d.expected_min for d in trial if d.arm == "planned"]
    hold = [d.heating_min - d.expected_min for d in trial if d.arm == "hold"]
    if not planned or not hold:
        return None
    saved = fmean(hold) - fmean(planned)
    interval = None
    if len(planned) >= 2 and len(hold) >= 2:
        se = math.sqrt(stdev(planned) ** 2 / len(planned) + stdev(hold) ** 2 / len(hold))
        interval = _t95(len(planned) + len(hold) - 2) * se
    expected = fmean(d.expected_min for d in trial if d.arm == "planned")  # the % is of these
    pct = 100 * saved / expected if expected > 0 else None
    ipct = 100 * interval / expected if interval is not None and expected > 0 else None
    return Effect(len(planned), len(hold), saved, interval, pct, ipct)


def _detectable(fit, trial: Trial, level: str) -> float | None:
    """The smallest saving (furnace minutes a day) the whole trial would find with 80% power
    at 95%: 2.8 standard errors of the planned-minus-hold difference."""
    first = trial.levels.get(level)
    if fit is None or first is None:
        return None
    n = (trial.end - first).days + 1 - sum(1 for d in trial.skip if first <= d <= trial.end)
    n_planned, n_hold = (n + 1) // 2, n // 2
    if n_planned < 1 or n_hold < 1:
        return None
    return 2.8 * fit.sd * math.sqrt(1 / n_planned + 1 / n_hold)


def _simulator(conn, level_key, device, trial, zone, now):
    """(start, predicted, actual) for stored plans whose next 24 hours really held the hold
    setpoints: the first run of each batch, from its "hold the setpoints" baseline."""
    out, seen = [], set()
    rows = conn.execute(
        "SELECT created_at, input_json, score_json FROM plan_run WHERE score_json IS NOT NULL"
        " ORDER BY id"
    ).fetchall()
    for created, input_json, score_json in rows:
        if created in seen:
            continue
        seen.add(created)
        start = parse_utc(created)
        end = start + timedelta(hours=24)
        if end > now:
            continue
        level = next((lv for lv in json.loads(input_json)["levels"] if lv["level"] == level_key),
                     None)  # fmt: skip
        hold = json.loads(score_json).get("hold the setpoints", {}).get(level_key)
        if level is None or hold is None:
            continue
        day = (start.astimezone(zone) - _since_midnight(trial)).date()
        if trial.arm(level_key, day) not in ("baseline", "hold"):
            continue
        sp = _steps(conn, "SELECT at, heat_c, cool_c FROM setpoint WHERE device_id = ?"
                    " AND {where}", device, start, end)  # fmt: skip
        if any(r[1] is not None and abs(r[1] - trial.hold_heat_c) > 0.3 for r in sp):
            continue  # not at the hold setpoints the whole time
        out.append((start, hold["heat_minutes"],
                    _minutes_between(conn, device, "heating", start, end)))  # fmt: skip
    return out


def _since_midnight(trial: Trial) -> timedelta:
    return timedelta(hours=trial.day_starts.hour, minutes=trial.day_starts.minute)


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def save(conn: sqlite3.Connection, cards: list[LevelCard]) -> None:
    conn.executescript(SCHEMA)
    with conn:
        for card in cards:
            conn.execute("DELETE FROM scorecard_day WHERE level = ?", (card.level,))
            conn.executemany(
                "INSERT INTO scorecard_day VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(d.level, d.day.isoformat(), d.arm, _iso(d.start), _iso(d.end), d.outdoor_c,
                  d.degree_days_c, d.expected_min, d.heating_min, d.cooling_min, d.outside_min,
                  d.low_c, d.off_hold_min, int(d.used), "; ".join(d.notes))
                 for d in card.days],
            )  # fmt: skip


# --- the report ---


def render(cards: list[LevelCard], cfg: Config, zone: ZoneInfo) -> str:
    unit, trial = cfg.unit, cfg.trial

    def t(c):
        return "  -  " if c is None else f"{from_c(c, unit):5.1f}"

    lines = [
        f"Trial {trial.start:%a %m/%d}-{trial.end:%a %m/%d}: planned and hold "
        f"({from_c(trial.hold_heat_c, unit):g}/{from_c(trial.hold_cool_c, unit):g}{unit}) days "
        f"alternate, each from {trial.day_starts:%H:%M} to {trial.day_starts:%H:%M}.",
    ]
    for card in cards:
        first = trial.levels.get(card.level)
        lines += ["", f"{card.level}" + (f" (in the trial from {first:%a %m/%d})" if first
                                         else " (not in the trial)")]  # fmt: skip
        lines.append(f"  {'day':10} {'arm':9} {'out':>5} {'furnace':>8} {'expect':>7} "
                     f"{'AC':>4} {'outside':>8} {'low':>5}  notes")  # fmt: skip
        for d in card.days:
            expect = "-" if d.expected_min is None else f"{d.expected_min:.0f}"
            lines.append(
                f"  {d.day:%a %m/%d}  {d.arm:9} {t(d.outdoor_c)} {d.heating_min:4.0f} min"
                f" {expect:>7} {d.cooling_min:4.0f} {d.outside_min:4.0f} min {t(d.low_c)}"
                f"  {'; '.join(d.notes)}"
            )
        f = card.fit
        if f is None:
            lines.append(f"  weather fit: needs {MIN_FIT_DAYS} usable days at the hold setpoints")
        else:
            per = f.slope if unit == "C" else f.slope * 5 / 9
            lines.append(
                f"  weather fit ({f.n} days at the hold setpoints): {per:.1f} furnace min per"
                f" degree-day below {from_c(f.base_c, unit):.0f}{unit}, plus {f.intercept:.0f};"
                f" R² {f.r2:.2f}, ±{f.sd:.0f} min a day"
            )
        e = card.effect
        if e is None and card.detectable_min is not None:
            share = ""
            if card.heating_day_min:
                pct = 100 * card.detectable_min / card.heating_day_min
                share = (f", {pct:.0f}% of a heating day so far"
                         f" ({card.heating_day_min:.0f} min expected)")  # fmt: skip
            lines.append(f"  the trial can detect a saving of about {card.detectable_min:.0f}"
                         f" min a day or more{share}")  # fmt: skip
        elif e is not None:
            if e.interval_min is None:
                ival = " (too few days for an interval)"
            else:
                ival = f" ± {e.interval_min:.0f}"
            pct = ""
            if e.saved_pct is not None:
                pi = f" ± {e.interval_pct:.0f}%" if e.interval_pct is not None else ""
                pct = f" ({e.saved_pct:.0f}%{pi})"
            verdict = ""
            if e.interval_min is not None:
                if e.saved_min - e.interval_min > 0:
                    verdict = ": a real saving"
                elif e.saved_min + e.interval_min < 0:
                    verdict = ": the plans used more"
                else:
                    verdict = ": not distinguishable from no change yet"
            lines.append(f"  planned vs hold ({e.planned} vs {e.hold} days): saves "
                         f"{e.saved_min:.0f} min a day{ival}{pct}, 95%{verdict}")  # fmt: skip
        planned = [d for d in card.days if d.used and d.arm == "planned"]
        if planned:
            worst = min((d.low_c for d in planned if d.low_c is not None), default=None)
            outside = sum(d.outside_min for d in planned)
            lines.append(f"  comfort on planned days: {outside:.0f} min outside the bands,"
                         f" lowest {t(worst).strip()}{unit}")  # fmt: skip
        if card.simulator:
            ratios = [p / a for _, p, a in card.simulator if a > 0]
            pairs = ", ".join(f"{s.astimezone(zone):%m/%d %H:%M} {p:.0f}/{a:.0f}"
                              for s, p, a in card.simulator[-6:])  # fmt: skip
            if ratios:
                lines.append(f"  simulator vs actual furnace minutes: {100 * fmean(ratios):.0f}%"
                             f" on average over {len(ratios)} runs ({pairs})")  # fmt: skip
    return "\n".join(lines)


def to_json(cards: list[LevelCard]) -> list[dict]:
    def day(d: Day) -> dict:
        return {"day": d.day.isoformat(), "arm": d.arm, "outdoor_c": d.outdoor_c,
                "degree_days_c": d.degree_days_c, "expected_min": d.expected_min,
                "heating_min": d.heating_min, "cooling_min": d.cooling_min,
                "outside_min": d.outside_min, "low_c": d.low_c, "used": d.used,
                "notes": d.notes}  # fmt: skip

    return [{
        "level": c.level,
        "days": [day(d) for d in c.days],
        "fit": c.fit and vars(c.fit),
        "effect": c.effect and vars(c.effect),
        "detectable_min": c.detectable_min,
        "heating_day_min": c.heating_day_min,
        "simulator": [{"from": _iso(s), "predicted_min": p, "actual_min": a}
                      for s, p, a in c.simulator],
    } for c in cards]  # fmt: skip
