"""The planner's settings for one house, from a YAML file: PLANNER_CONFIG if set, else
planner.yaml next to .env. Temperatures are written in the file's `unit` (F or C) and kept in
Celsius here. See planner.example.yaml for every field."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time, timedelta
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


def to_c(value: float, unit: str) -> float:
    return value if unit == "C" else (value - 32) * 5 / 9


def delta_c(value: float, unit: str) -> float:
    """A temperature difference, such as min_gap or a rate, in Celsius."""
    return value if unit == "C" else value * 5 / 9


def from_c(celsius: float, unit: str) -> float:
    return celsius if unit == "C" else celsius * 9 / 5 + 32


def delta_from_c(celsius: float, unit: str) -> float:
    return celsius if unit == "C" else celsius * 9 / 5


def step(unit: str) -> float:
    """The setpoint resolution in `unit`: thermostats set whole °F or half °C."""
    return 1.0 if unit == "F" else 0.5


@dataclass(frozen=True)
class Band:
    """The temperatures a level must stay between: the furnace keeps it at or above heat_c,
    the AC at or below cool_c. A band with no start and end is the level's default."""

    name: str
    heat_c: float
    cool_c: float
    start: time | None = None  # local time
    end: time | None = None

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

    def band_at(self, at: time) -> Band:
        """The timed band covering `at`, else the default band."""
        for band in self.bands:
            if band.start is not None and band.covers(at):
                return band
        return next(b for b in self.bands if b.start is None)


@dataclass(frozen=True)
class Limits:
    floor_c: float  # setpoints never below (a pipe-freeze margin)
    ceiling_c: float
    min_gap_c: float  # heat at least this far below cool
    max_step_c: float  # the largest change from one setpoint to the next
    min_interval: timedelta  # between changes on a level
    manual_pause: timedelta  # a change made at the thermostat pauses the planner this long


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
    )


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
    )


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{where} must be a number.")
    return float(value)


def _time(value: Any, where: str) -> time:
    # YAML reads an unquoted 22:00 as a number of minutes (sexagesimal); accept both.
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
