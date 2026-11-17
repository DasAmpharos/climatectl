"""Deterministic limits applied to every plan, whatever the model returned. Each fix is recorded
as a correction, so a model that needs them can be spotted (the target is none).

A plan is a list of changes per level, each holding until the next one. The span from now to
the first change keeps the current setpoints, so it's checked as a change at now; if it needs
fixing, the fix becomes the first change. In order, for each change:

1. Times: in local time, rounded to the quarter hour; before now means now, and beyond the
   24-hour horizon is dropped. A first change due within min_interval of now starts now; any
   other change less than min_interval after the previous one is dropped (short-cycling
   protection).
2. max_step: each setpoint moves at most this far from the one before it.
3. Comfort: heat is at least, and cool at most, every band in force during the change's span.
   Comfort wins over max_step.
4. Hard floor and ceiling.
5. min_gap: cool at least this far above heat (cool moves up, or heat down at the ceiling).
6. Rounded to the thermostat's resolution, heat up and cool down, so rounding never leaves a
   band.

A level in a mode with only one setpoint plans only that one. A level the context marked as not
planned (offline, Eco, off, changed by hand) gets no changes."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..thermostat import COOL, HEAT
from .config import Config, Level, from_c, step, to_c
from .context import HORIZON, Context

QUARTER = timedelta(minutes=15)
EPSILON = 0.01  # °C: thermostats and F/C conversions put an edge like 72F a hair either side
ROUND_SLACK = 0.05  # of a setpoint step


@dataclass(frozen=True)
class Change:
    at: datetime  # UTC
    heat_c: float | None
    cool_c: float | None


@dataclass
class LevelPlan:
    level: str
    changes: list[Change]
    rationale: str
    skipped: str | None = None
    corrections: list[str] = field(default_factory=list)


@dataclass
class Plan:
    levels: list[LevelPlan]
    notes_update: str
    corrections: list[str] = field(default_factory=list)  # about the plan as a whole


def apply(raw: dict[str, Any], ctx: Context, config: Config) -> Plan:
    by_level: dict[str, dict[str, Any]] = {}
    plan = Plan([], str(raw.get("notes_update") or "").strip())
    for item in raw.get("levels") or []:
        key = str(item.get("level", ""))
        if key not in ctx.levels:
            plan.corrections.append(f"ignored a plan for unknown level {key!r}")
        elif key in by_level:
            plan.corrections.append(f"ignored a second plan for {key}")
        else:
            by_level[key] = item
    for key, state in ctx.levels.items():
        item = by_level.get(key, {})
        level_plan = LevelPlan(key, [], str(item.get("rationale") or "").strip())
        plan.levels.append(level_plan)
        if state.skip:
            level_plan.skipped = state.skip
            if item.get("changes"):
                level_plan.corrections.append(
                    f"dropped changes for a level not planned now ({state.skip})"
                )
            continue
        _level(level_plan, item.get("changes") or [], state, ctx, config)
    return plan


def _level(out: LevelPlan, changes: list[Any], state, ctx: Context, config: Config) -> None:
    unit, limits = config.unit, config.limits
    end = ctx.now + HORIZON

    def say(text: str) -> None:
        out.corrections.append(text)

    # 1. Times.
    timed: list[tuple[datetime, float | None, float | None]] = []
    for c in changes:
        at = _parse_local(c.get("at"), ctx)
        if at is None:
            say(f"dropped a change with an unreadable time {c.get('at')!r}")
            continue
        if at > end:
            say(f"dropped a change at {_local(at, ctx)}, past the 24-hour horizon")
            continue
        timed.append((max(at, ctx.now), _num(c.get("heat")), _num(c.get("cool"))))
    timed.sort(key=lambda t: t[0])
    # Several changes at or before now: only the last one is in force.
    while len(timed) > 1 and timed[1][0] <= ctx.now:
        timed.pop(0)
    # A first change due within min_interval of now starts now, so it isn't dropped as too
    # soon after the current setpoints.
    if timed and timed[0][0] - ctx.now < limits.min_interval:
        timed[0] = (ctx.now, *timed[0][1:])
    if not timed or timed[0][0] > ctx.now:
        heat = state.heat_c and from_c(state.heat_c, unit)
        cool = state.cool_c and from_c(state.cool_c, unit)
        timed.insert(0, (ctx.now, heat, cool))  # the current setpoints, checked like a change
    kept = [timed[0]]
    for t in timed[1:]:
        if t[0] - kept[-1][0] < limits.min_interval:
            say(f"dropped the change at {_local(t[0], ctx)}: under {_minutes(limits.min_interval)}"
                " after the previous one")  # fmt: skip
        else:
            kept.append(t)

    # Only the setpoints the mode uses count, so a heat-only level never compares cool.
    previous = (None if state.mode == COOL else state.heat_c,
                None if state.mode == HEAT else state.cool_c)  # fmt: skip
    for i, (at, heat_u, cool_u) in enumerate(kept):
        span_end = kept[i + 1][0] if i + 1 < len(kept) else end
        when = _local(at, ctx)
        heat = None if state.mode == COOL else _c(heat_u, unit)
        cool = None if state.mode == HEAT else _c(cool_u, unit)
        if heat is None and state.mode != COOL:
            say(f"{when}: no heat setpoint given, so it keeps the previous one")
            heat = previous[0]
        if cool is None and state.mode != HEAT:
            say(f"{when}: no cool setpoint given, so it keeps the previous one")
            cool = previous[1]
        # 2. max_step.
        heat = _step(heat, previous[0], limits.max_step_c, f"{when} heat", unit, say)
        cool = _step(cool, previous[1], limits.max_step_c, f"{when} cool", unit, say)
        # 3. Comfort.
        bands = _bands(state.level, at, span_end, ctx)
        if heat is not None and heat < (need := max(b.heat_c for b in bands)) - EPSILON:
            say(f"{when}: heat raised {_f(heat, unit)} -> {_f(need, unit)} to stay in the band")
            heat = need
        if cool is not None and cool > (need := min(b.cool_c for b in bands)) + EPSILON:
            say(f"{when}: cool lowered {_f(cool, unit)} -> {_f(need, unit)} to stay in the band")
            cool = need
        # 4. Floor and ceiling.
        for name, value in (("heat", heat), ("cool", cool)):
            if value is not None and not limits.floor_c <= value <= limits.ceiling_c:
                fixed = min(max(value, limits.floor_c), limits.ceiling_c)
                say(f"{when}: {name} {_f(value, unit)} -> {_f(fixed, unit)} (floor/ceiling)")
                if name == "heat":
                    heat = fixed
                else:
                    cool = fixed
        # 5 and 6. min_gap, then rounding (which can't close the gap: heat rounds up, so cool
        # is recomputed from the rounded heat).
        heat = _round(heat, unit, up=True)
        cool = _round(cool, unit, up=False)
        if heat is not None and cool is not None and cool - heat < limits.min_gap_c - 1e-9:
            wider = _round(heat + limits.min_gap_c, unit, up=True)
            if wider <= limits.ceiling_c:
                say(f"{when}: cool raised {_f(cool, unit)} -> {_f(wider, unit)} (min_gap)")
                cool = wider
            else:
                lower = _round(cool - limits.min_gap_c, unit, up=False)
                say(f"{when}: heat lowered {_f(heat, unit)} -> {_f(lower, unit)} (min_gap)")
                heat = lower
        if (heat, cool) != _rounded(previous, unit):
            out.changes.append(Change(at, heat, cool))
        previous = (heat, cool)


def _parse_local(text: Any, ctx: Context) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=ctx.zone)
    at = at.astimezone(ctx.now.tzinfo)
    # Nearest quarter hour.
    seconds = (at - at.replace(minute=0, second=0, microsecond=0)).total_seconds()
    base = at.replace(minute=0, second=0, microsecond=0)
    return base + QUARTER * round(seconds / QUARTER.total_seconds())


def _bands(level: Level, start: datetime, end: datetime, ctx: Context):
    """Every band in force from start (inclusive) to end (exclusive), sampled each quarter."""
    bands, at = [], start
    while at < end:
        band = level.band_at(at.astimezone(ctx.zone).time())
        if band not in bands:
            bands.append(band)
        at += QUARTER
    return bands or [level.band_at(start.astimezone(ctx.zone).time())]


def _c(value: float | None, unit: str) -> float | None:
    return None if value is None else to_c(value, unit)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _step(value, previous, max_step, what, unit, say):
    if value is None or previous is None or abs(value - previous) <= max_step + 1e-9:
        return value
    fixed = previous + math.copysign(max_step, value - previous)
    say(f"{what} {_f(value, unit)} -> {_f(fixed, unit)} (max_step from {_f(previous, unit)})")
    return fixed


def _round(celsius: float | None, unit: str, up: bool) -> float | None:
    if celsius is None:
        return None
    size = step(unit)
    value = from_c(celsius, unit) / size
    # Within ROUND_SLACK of a step counts as on it: Nest reports 64F as 64.004, which must stay
    # 64, not round up to 65.
    value = math.ceil(value - ROUND_SLACK) if up else math.floor(value + ROUND_SLACK)
    return to_c(value * size, unit)


def _rounded(pair, unit):
    heat, cool = pair
    return (_round(heat, unit, up=True), _round(cool, unit, up=False))


def _f(celsius: float, unit: str) -> str:
    return f"{from_c(celsius, unit):.1f}{unit}"


def _local(at: datetime, ctx: Context) -> str:
    return at.astimezone(ctx.zone).strftime("%a %H:%M")


def _minutes(span: timedelta) -> str:
    return f"{span.total_seconds() / 60:.0f} min"
