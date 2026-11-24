"""The planner's settings for one house, from a YAML file: PLANNER_CONFIG if set, else
planner.yaml next to .env. Temperatures are written in the file's `unit` (F or C) and kept in
Celsius here. See planner.example.yaml for every field."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, time, timedelta
from pathlib import Path
from typing import Any

import yaml

from .. import envfile
from ..envfile import ConfigError

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Defaults in °F; each is converted with the file's unit, so a C file gets the same limits.
DEFAULT_LIMITS_F = {"floor": 55, "ceiling": 86, "min_gap": 3, "max_step": 6}
DEFAULT_MIN_INTERVAL_MINUTES = 60
DEFAULT_MANUAL_PAUSE_HOURS = 4
DEFAULT_TRIAL_DAY_STARTS = time(21, 0)
DEFAULT_TRIAL_TOLERANCE_F = 1.0
# The trial's arms. Before a level's first trial day (and from baseline_from) it's "baseline";
# then the days alternate "planned" and "hold", counted from the trial's start so every level
# is on the same arm on the same day; "skipped" days are left out; "after" follows the end.
ARMS = ("baseline", "planned", "hold", "skipped", "after")


def to_c(value: float, unit: str) -> float:
    return value if unit == "C" else (value - 32) * 5 / 9


def delta_c(value: float, unit: str) -> float:
    """A temperature difference, such as min_gap or a rate, in Celsius."""
    return value if unit == "C" else value * 5 / 9


def from_c(celsius: float, unit: str) -> float:
    return celsius if unit == "C" else celsius * 9 / 5 + 32


def delta_from_c(celsius: float, unit: str) -> float:
    return celsius if unit == "C" else celsius * 9 / 5


def dew_point_c(temp_c: float, humidity_pct: float) -> float:
    """The dew point (Magnus formula, as the dew_point view): unlike relative humidity, it
    measures the water in the air the same way at any temperature."""
    g = math.log(humidity_pct / 100) + 17.62 * temp_c / (243.12 + temp_c)
    return 243.12 * g / (17.62 - g)


def step(unit: str) -> float:
    """The setpoint resolution in `unit`: thermostats set whole °F or half °C."""
    return 1.0 if unit == "F" else 0.5


@dataclass(frozen=True)
class DewPointStep:
    """A band's cool limit while the level's dew point is below `below_c`, or at least
    `at_least_c` (exactly one is set): humid air feels warmer, dry air cooler."""

    cool_c: float
    below_c: float | None = None
    at_least_c: float | None = None

    def applies(self, dew_point: float) -> bool:
        if self.below_c is not None:
            return dew_point < self.below_c
        return dew_point >= self.at_least_c


@dataclass(frozen=True)
class Band:
    """The temperatures a level must stay between: the furnace keeps it at or above heat_c,
    the AC at or below cool_c. A band with no start and end is the level's default.

    With cool_by_dew_point, cool_c is the limit when no step applies, and for_dew_point gives
    the band with the limit for a dew point: the first step that applies, in order."""

    name: str
    heat_c: float
    cool_c: float
    start: time | None = None  # local time
    end: time | None = None
    cool_by_dew_point: tuple[DewPointStep, ...] = ()
    base_cool_c: float | None = None  # set by for_dew_point: cool_c when no step applies
    dew_point_c: float | None = None  # set by for_dew_point: the dew point it used

    def for_dew_point(self, dew_point: float | None) -> Band:
        if not self.cool_by_dew_point or dew_point is None:
            return self
        base = self.cool_c if self.base_cool_c is None else self.base_cool_c
        cool = next((s.cool_c for s in self.cool_by_dew_point if s.applies(dew_point)), base)
        return replace(self, cool_c=cool, base_cool_c=base, dew_point_c=dew_point)

    def covers(self, at: time) -> bool:
        if self.start is None or self.end is None:
            return True
        if self.start <= self.end:
            return self.start <= at < self.end
        return at >= self.start or at < self.end  # crosses midnight, e.g. 22:00-07:00


@dataclass(frozen=True)
class Level:
    key: str  # the thermostat's name or device id, as written in the file
    about: str
    bands: tuple[Band, ...]

    def band_at(self, at: time, dew_point: float | None = None) -> Band:
        """The timed band covering `at`, else the default band; with `dew_point` (°C), with
        its cool limit for that dew point."""
        for band in self.bands:
            if band.start is not None and band.covers(at):
                return band.for_dew_point(dew_point)
        return next(b for b in self.bands if b.start is None).for_dew_point(dew_point)

    def for_dew_point(self, dew_point: float | None) -> Level:
        """This level with every band's cool limit for `dew_point` (°C)."""
        return replace(self, bands=tuple(b.for_dew_point(dew_point) for b in self.bands))


@dataclass(frozen=True)
class Limits:
    floor_c: float  # setpoints never below (a pipe-freeze margin)
    ceiling_c: float
    min_gap_c: float  # heat at least this far below cool
    max_step_c: float  # the largest change from one setpoint to the next
    min_interval: timedelta  # between changes on a level
    manual_pause: timedelta  # a change made at the thermostat pauses the planner this long


@dataclass(frozen=True)
class Trial:
    """Alternating days: the planner's schedule on one, fixed hold setpoints on the next, so
    their energy can be compared in the same run of weather (see scorecard.py). A trial day
    runs from day_starts to day_starts the next day, so a night and its morning recovery stay
    together; it's named by the date it starts."""

    start: date  # day 0, planned
    end: date  # the last trial day
    day_starts: time
    hold_heat_c: float
    hold_cool_c: float
    levels: dict[str, date]  # level key -> its first trial day
    baseline_from: date | None
    skip: frozenset[date]
    tolerance_c: float  # how far past a band counts as outside it (thermostats cycle ~1F)

    def arm(self, level: str, day: date) -> str:
        if day in self.skip:
            return "skipped"
        if day > self.end:
            return "after"
        first = self.levels.get(level)
        if first is None or day < first or day < self.start:
            return "baseline"
        return "planned" if (day - self.start).days % 2 == 0 else "hold"


@dataclass(frozen=True)
class Config:
    unit: str
    model: str
    effort: str
    house: str
    levels: tuple[Level, ...]
    limits: Limits
    notes_path: Path | None
    feedback_path: Path | None
    trial: Trial | None = None


def path_from_settings() -> Path:
    explicit = envfile.read_settings(("PLANNER_CONFIG",)).get("PLANNER_CONFIG")
    return Path(explicit) if explicit else envfile.env_path().parent / "planner.yaml"


def load(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"No planner config at {path} (see planner.example.yaml).")
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} isn't valid YAML: {e}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must be a mapping of settings.")
    return parse(raw, path.parent)


def parse(raw: dict[str, Any], base: Path) -> Config:
    unit = str(raw.get("unit", "F")).upper()
    if unit not in ("F", "C"):
        raise ConfigError("unit must be F or C.")
    effort = str(raw.get("effort", DEFAULT_EFFORT))
    if effort not in EFFORTS:
        raise ConfigError(f"effort must be one of {', '.join(EFFORTS)}.")
    limits = _limits(raw.get("limits") or {}, unit)
    levels = raw.get("levels")
    if not isinstance(levels, dict) or not levels:
        raise ConfigError("levels must name at least one thermostat.")
    return Config(
        unit=unit,
        model=str(raw.get("model", DEFAULT_MODEL)),
        effort=effort,
        house=str(raw.get("house", "")).strip(),
        levels=tuple(_level(str(k), v or {}, unit, limits) for k, v in levels.items()),
        limits=limits,
        notes_path=_path(raw.get("notes"), base),
        feedback_path=_path(raw.get("feedback"), base),
        trial=_trial(raw["trial"], unit, limits, levels) if raw.get("trial") else None,
    )


def _trial(raw: Any, unit: str, limits: Limits, levels: dict[str, Any]) -> Trial:
    if not isinstance(raw, dict):
        raise ConfigError("trial must be a mapping of settings.")
    start, end = _date(raw.get("start"), "trial.start"), _date(raw.get("end"), "trial.end")
    if end < start:
        raise ConfigError("trial.end must not be before trial.start.")
    hold = raw.get("hold") or {}
    heat = to_c(_number(hold.get("heat"), "trial.hold.heat"), unit)
    cool = to_c(_number(hold.get("cool"), "trial.hold.cool"), unit)
    if cool - heat < limits.min_gap_c - 1e-9 or heat < limits.floor_c or cool > limits.ceiling_c:
        raise ConfigError("trial.hold must keep min_gap and lie between floor and ceiling.")
    firsts = raw.get("levels")
    if not isinstance(firsts, dict) or not firsts:
        raise ConfigError("trial.levels must give each trial level its first day.")
    trial_levels = {}
    for key, first in firsts.items():
        if str(key) not in {str(k) for k in levels}:
            raise ConfigError(f"trial.levels.{key} isn't one of the levels.")
        day = _date(first, f"trial.levels.{key}")
        if not start <= day <= end:
            raise ConfigError(f"trial.levels.{key} must fall between trial.start and trial.end.")
        trial_levels[str(key)] = day
    skip = raw.get("skip") or []
    if not isinstance(skip, list):
        raise ConfigError("trial.skip must be a list of dates.")
    tolerance = raw.get("tolerance", DEFAULT_TRIAL_TOLERANCE_F if unit == "F" else 0.5)
    return Trial(
        start=start,
        end=end,
        day_starts=_time(raw.get("day_starts", DEFAULT_TRIAL_DAY_STARTS), "trial.day_starts"),
        hold_heat_c=heat,
        hold_cool_c=cool,
        levels=trial_levels,
        baseline_from=_date(raw["baseline_from"], "trial.baseline_from")
        if raw.get("baseline_from") else None,
        skip=frozenset(_date(d, "trial.skip") for d in skip),
        tolerance_c=delta_c(_number(tolerance, "trial.tolerance"), unit),
    )  # fmt: skip


def _date(value: Any, where: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ConfigError(f"{where} must be a date like 2026-11-25.") from None


def _limits(raw: dict[str, Any], unit: str) -> Limits:
    def value(name: str) -> float:
        if name in raw:
            return _number(raw[name], f"limits.{name}")
        f = DEFAULT_LIMITS_F[name]
        if unit == "F":
            return f
        return to_c(f, "F") if name in ("floor", "ceiling") else delta_c(f, "F")

    limits = Limits(
        floor_c=to_c(value("floor"), unit),
        ceiling_c=to_c(value("ceiling"), unit),
        min_gap_c=delta_c(value("min_gap"), unit),
        max_step_c=delta_c(value("max_step"), unit),
        min_interval=timedelta(
            minutes=_number(raw.get("min_interval_minutes", DEFAULT_MIN_INTERVAL_MINUTES),
                            "limits.min_interval_minutes")
        ),
        manual_pause=timedelta(
            hours=_number(raw.get("manual_pause_hours", DEFAULT_MANUAL_PAUSE_HOURS),
                          "limits.manual_pause_hours")
        ),
    )  # fmt: skip
    if limits.floor_c >= limits.ceiling_c:
        raise ConfigError("limits.floor must be below limits.ceiling.")
    if limits.max_step_c <= 0 or limits.min_gap_c < 0:
        raise ConfigError("limits.max_step must be positive and limits.min_gap not negative.")
    return limits


def _level(key: str, raw: dict[str, Any], unit: str, limits: Limits) -> Level:
    bands_raw = raw.get("bands")
    if not isinstance(bands_raw, list) or not bands_raw:
        raise ConfigError(f"levels.{key}.bands must list at least one band.")
    bands = tuple(_band(key, i, b or {}, unit, limits) for i, b in enumerate(bands_raw))
    if sum(b.start is None for b in bands) != 1:
        raise ConfigError(f"levels.{key} needs exactly one band without from/to (the default).")
    return Level(key=key, about=str(raw.get("about", "")).strip(), bands=bands)


def _band(level: str, i: int, raw: dict[str, Any], unit: str, limits: Limits) -> Band:
    where = f"levels.{level}.bands[{i}]"
    heat = to_c(_number(raw.get("heat"), f"{where}.heat"), unit)
    cool = to_c(_number(raw.get("cool"), f"{where}.cool"), unit)
    if cool - heat < limits.min_gap_c - 1e-9:
        raise ConfigError(f"{where}: cool must be at least limits.min_gap above heat.")
    if heat < limits.floor_c or cool > limits.ceiling_c:
        raise ConfigError(f"{where} must lie between limits.floor and limits.ceiling.")
    start, end = raw.get("from"), raw.get("to")
    if (start is None) != (end is None):
        raise ConfigError(f"{where} needs both from and to, or neither.")
    return Band(
        name=str(raw.get("name", f"band {i + 1}")),
        heat_c=heat,
        cool_c=cool,
        start=None if start is None else _time(start, f"{where}.from"),
        end=None if end is None else _time(end, f"{where}.to"),
        cool_by_dew_point=_dew_point_steps(
            raw.get("cool_by_dew_point"), where, heat, unit, limits
        ),  # fmt: skip
    )


def _dew_point_steps(raw: Any, where: str, heat_c: float, unit: str, limits: Limits):
    if raw is None:
        return ()
    where = f"{where}.cool_by_dew_point"
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{where} must be a list of steps like {{below: 50, cool: 69}}.")
    steps = []
    for i, step_raw in enumerate(raw):
        at = f"{where}[{i}]"
        if not isinstance(step_raw, dict) or ("below" in step_raw) == ("at_least" in step_raw):
            raise ConfigError(f"{at} needs cool and exactly one of below or at_least.")
        cool = to_c(_number(step_raw.get("cool"), f"{at}.cool"), unit)
        if cool - heat_c < limits.min_gap_c - 1e-9 or cool > limits.ceiling_c:
            raise ConfigError(f"{at}: cool must keep limits.min_gap and not pass the ceiling.")
        if "below" in step_raw:
            step_ = DewPointStep(
                cool, below_c=to_c(_number(step_raw["below"], f"{at}.below"), unit)
            )
        else:
            step_ = DewPointStep(
                cool, at_least_c=to_c(_number(step_raw["at_least"], f"{at}.at_least"), unit)
            )
        steps.append(step_)
    return tuple(steps)


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{where} must be a number.")
    return float(value)


def _time(value: Any, where: str) -> time:
    # YAML reads an unquoted 22:00 as a number of minutes (sexagesimal); accept both.
    if isinstance(value, time):
        return value
    if isinstance(value, int):
        return time(value // 60 % 24, value % 60)
    try:
        return time.fromisoformat(str(value))
    except ValueError:
        raise ConfigError(f"{where} must be a time like 22:00.") from None


def _path(value: Any, base: Path) -> Path | None:
    if not value:
        return None
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else base / path
