"""Writes the trial's setpoints to the thermostats: on a planned day the plan's changes at their
times, on a hold day the hold setpoints when the day starts. Baseline days, skipped days and
days after the trial are left alone.

The plan for a trial day is the first one from the config's `model` made between PLAN_EARLY
before the day starts and PLAN_WAIT after it (whoever runs `climatectl plan` then, e.g. a
cron job or a shadow-planning service). With no plan by then, the day is recorded as "no plan"
and nothing is written: the setpoints stay as they were, and the scorecard leaves the day out.

A person's change wins. Before each write the thermostat is read; if its setpoints aren't what
was last written, someone changed them, and the level is left alone until the next trial day
starts ("stood down"). At a new trial day it takes over again, but not before
limits.manual_pause has passed since that change. Offline, Eco and off are skipped for the
step and tried again at the next one.

Every step is recorded in apply_step, once: what was set, or why not. That makes it safe to
restart at any time: only the latest step that's due and not yet recorded is written, so steps
missed while it was down are superseded by the one that's due now."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..providers import Provider, ProviderError
from ..store import parse_utc
from ..thermostat import COOL, HEAT, HEAT_COOL, Thermostat
from . import context
from .config import Config, Trial, from_c

log = logging.getLogger("climatectl")

SCHEMA = """
CREATE TABLE IF NOT EXISTS apply_step (
    id           INTEGER PRIMARY KEY,
    trial_day    TEXT NOT NULL,      -- the trial day it belongs to, by its start date
    level        TEXT NOT NULL,      -- the level's key in the planner config
    arm          TEXT NOT NULL,      -- planned or hold
    due_at       TEXT NOT NULL,      -- when the step was due (UTC)
    done_at      TEXT NOT NULL,      -- when it was dealt with (UTC)
    plan_run_id  INTEGER,            -- planned days: the plan it came from
    heat_c REAL, cool_c REAL,        -- what was set, or would have been
    outcome      TEXT NOT NULL,      -- applied, kept, skipped, stood down, no plan
    note         TEXT,
    UNIQUE (trial_day, level, due_at)
);
"""

APPLIED, KEPT, SKIPPED, STOOD_DOWN, NO_PLAN = "applied", "kept", "skipped", "stood down", "no plan"
NOT_APPLIED = (SKIPPED, STOOD_DOWN, NO_PLAN)  # a day with one of these wasn't the arm all day

PLAN_EARLY = timedelta(minutes=15)
PLAN_WAIT = timedelta(minutes=30)
MATCH_C = 0.35  # a live setpoint this close to the one written counts as unchanged (0.6F)
RETRY = timedelta(minutes=5)  # after a provider error, or while a person's change is fresh


def prepare(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def trial_day(trial: Trial, now: datetime, zone: ZoneInfo) -> tuple[date, datetime]:
    """The trial day `now` is in, and when it started (in `now`'s zone)."""
    local = now.astimezone(zone)
    day = local.date()
    if local.time() < trial.day_starts:
        day -= timedelta(days=1)
    return day, datetime.combine(day, trial.day_starts, tzinfo=zone).astimezone(now.tzinfo)


@dataclass(frozen=True)
class Step:
    level: str
    day: date
    arm: str
    due: datetime
    heat_c: float | None
    cool_c: float | None
    plan_run: int | None = None


@dataclass
class Applier:
    conn: sqlite3.Connection
    config: Config
    provider: Provider
    zone: ZoneInfo
    write: bool = True  # False: say what would be done, write nothing anywhere
    notify: Callable[[str, str], None] | None = None  # (title, message): a day lost to the arm
    _wait: dict[str, datetime] = field(default_factory=dict)  # level -> don't retry before

    def tick(self, now: datetime) -> list[str]:
        """One pass over the levels in the trial. Returns a line per thing done."""
        trial = self.config.trial
        if trial is None:
            return []
        day, start = trial_day(trial, now, self.zone)
        lines: list[str] = []
        live: dict[str, Thermostat] | None = None
        for level in self.config.levels:
            key = level.key
            arm = trial.arm(key, day)
            if arm not in ("planned", "hold") or self._stood_down(day, key):
                continue
            if self._wait.get(key, now) > now:
                continue
            step = self._due(trial, key, day, arm, start, now, lines)
            if step is None or self._recorded(step):
                continue
            if live is None:
                try:
                    live = {t.device_id: t for t in self.provider.thermostats()}
                except ProviderError as e:
                    lines.append(f"couldn't read the thermostats: {e}")
                    self._wait = dict.fromkeys((lv.key for lv in self.config.levels), now + RETRY)
                    return lines
            lines.extend(self._apply(step, live, now))
        return lines

    def _due(self, trial, key, day, arm, start, now, lines) -> Step | None:
        if arm == "hold":
            return Step(key, day, arm, start, trial.hold_heat_c, trial.hold_cool_c)
        run = self._plan(start)
        if run is None:
            if now - start >= PLAN_WAIT:
                note = f"no plan from {self.config.model} by {self._local(start + PLAN_WAIT)}"
                self._record(Step(key, day, arm, start, None, None), NO_PLAN, now, note)
                lines.append(f"{key}: {note}; the setpoints are left as they are")
                self._tell(
                    f"{key}: no plan to apply", f"{note}. The setpoints are left as they are."
                )
            return None
        run_id, levels = run
        plan = levels.get(key)
        if plan is None or plan.get("skipped"):
            reason = (plan or {}).get("skipped") or "not in the plan"
            step = Step(key, day, arm, start, None, None, run_id)
            if not self._recorded(step):
                self._record(step, SKIPPED, now, f"the plan skipped it: {reason}")
                lines.append(f"{key}: the plan skipped it ({reason})")
            return None
        changes = sorted(plan.get("changes") or [], key=lambda c: c["at"])
        due = [c for c in changes if parse_utc(c["at"]) <= now]
        if not changes:
            step = Step(key, day, arm, start, None, None, run_id)
            if not self._recorded(step):
                self._record(step, KEPT, now, "the plan keeps the setpoints")
                self._mark_applied(run_id)
                lines.append(f"{key}: the plan keeps the setpoints")
            return None
        if not due:
            return None
        last = due[-1]
        return Step(key, day, arm, parse_utc(last["at"]), last.get("heat_c"),
                    last.get("cool_c"), run_id)  # fmt: skip

    def _apply(self, step: Step, live: dict[str, Thermostat], now: datetime) -> list[str]:
        names = {t.device_id: t.name for t in live.values()}
        device = context._device(step.level, names)
        t = live.get(device) if device else None
        what = self._setpoints(step.heat_c, step.cool_c)
        if t is None:
            return self._skip(step, now, "no thermostat by this name")
        if t.online is False:
            return self._skip(step, now, "the thermostat is offline")
        if t.eco:
            return self._skip(step, now, "Eco is on")
        if t.mode not in (HEAT, COOL, HEAT_COOL):
            return self._skip(step, now, f"the mode is {t.mode or 'unknown'}")
        if problem := self._unsafe(step):
            return self._skip(step, now, problem)

        last = self._last_written(step.level)
        if last is not None and not self._matches(t, last[1], last[2]):
            by_hand = f"changed by hand to {self._setpoints(t.heat_c, t.cool_c)}"
            if last[0] >= self._day_start(step.day):
                self._record(step, STOOD_DOWN, now, by_hand)
                line = f"{step.level}: {by_hand}, so it's left alone until the next trial day"
                self._tell(f"{step.level}: left alone", line)
                return [line]
            changed = self._changed_at(device, *last) or now  # not logged yet: count it as now
            resume = changed + self.config.limits.manual_pause
            if now < resume:
                self._wait[step.level] = min(resume, now + RETRY)
                return [f"{step.level}: {by_hand}; waiting until {self._local(resume)}"]

        heat = step.heat_c if t.mode in (HEAT, HEAT_COOL) else None
        cool = step.cool_c if t.mode in (COOL, HEAT_COOL) else None
        if t.mode == HEAT_COOL:  # a step with one setpoint keeps the other
            heat = t.heat_c if heat is None else heat
            cool = t.cool_c if cool is None else cool
        if heat is None and cool is None:
            return self._skip(step, now, f"nothing to set in {t.mode} mode")
        if self._matches(t, heat, cool):
            self._record(step, KEPT, now, "already set", heat, cool)
            self._mark_applied(step.plan_run)
            return [f"{step.level}: already at {what}"]
        if not self.write:
            due = self._local(step.due)
            return [f"{step.level}: would set {self._setpoints(heat, cool)} (due {due})"]
        try:
            self.provider.set_range(t.device_id, heat, cool)
        except ProviderError as e:
            self._wait[step.level] = now + RETRY
            return [f"{step.level}: setting {what} failed, trying again: {e}"]
        was = self._setpoints(t.heat_c, t.cool_c)
        self._record(step, APPLIED, now, f"was {was}", heat, cool)
        self._mark_applied(step.plan_run)
        return [f"{step.level}: set {self._setpoints(heat, cool)} ({step.arm}, was {was})"]

    # --- the plan and the records ---

    def _plan(self, start: datetime) -> tuple[int, dict[str, dict]] | None:
        row = self.conn.execute(
            "SELECT id, plan_json FROM plan_run WHERE model = ? AND error IS NULL"
            " AND plan_json IS NOT NULL AND created_at >= ? AND created_at <= ?"
            " ORDER BY created_at, id LIMIT 1",
            (self.config.model, _stamp(start - PLAN_EARLY), _stamp(start + PLAN_WAIT)),
        ).fetchone()
        if row is None:
            return None
        return row[0], {p["level"]: p for p in json.loads(row[1])}

    def _recorded(self, step: Step) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM apply_step WHERE trial_day = ? AND level = ? AND due_at = ?",
            (step.day.isoformat(), step.level, _stamp(step.due)),
        ).fetchone() is not None  # fmt: skip

    def _stood_down(self, day: date, level: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM apply_step WHERE trial_day = ? AND level = ? AND outcome IN (?, ?)",
            (day.isoformat(), level, STOOD_DOWN, NO_PLAN),
        ).fetchone() is not None  # fmt: skip

    def _last_written(self, level: str) -> tuple[datetime, float | None, float | None] | None:
        row = self.conn.execute(
            "SELECT done_at, heat_c, cool_c FROM apply_step WHERE level = ? AND outcome IN (?, ?)"
            " AND (heat_c IS NOT NULL OR cool_c IS NOT NULL) ORDER BY done_at DESC, id DESC"
            " LIMIT 1",
            (level, APPLIED, KEPT),
        ).fetchone()
        return (parse_utc(row[0]), row[1], row[2]) if row else None

    def _changed_at(self, device: str, since: datetime, heat, cool) -> datetime | None:
        """When the setpoints last changed after the applier wrote (heat, cool) at `since`."""
        last, changed = (heat, cool), None
        for at, h, c in self.conn.execute(
            "SELECT at, heat_c, cool_c FROM setpoint WHERE device_id = ? AND at > ? ORDER BY at",
            (device, _stamp(since)),
        ):
            differs = any(
                a is not None and b is not None and abs(a - b) > MATCH_C
                for a, b in ((h, last[0]), (c, last[1]))
            )
            if differs:
                changed = parse_utc(at)
            last = (h if h is not None else last[0], c if c is not None else last[1])
        return changed

    def _record(self, step, outcome, now, note, heat=None, cool=None) -> None:
        if not self.write:
            return
        self.conn.execute(
            "INSERT OR IGNORE INTO apply_step (trial_day, level, arm, due_at, done_at,"
            " plan_run_id, heat_c, cool_c, outcome, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (step.day.isoformat(), step.level, step.arm, _stamp(step.due), _stamp(now),
             step.plan_run, heat if heat is not None else step.heat_c,
             cool if cool is not None else step.cool_c, outcome, note),
        )  # fmt: skip
        self.conn.commit()

    def _mark_applied(self, run_id: int | None) -> None:
        if run_id is not None and self.write:
            self.conn.execute("UPDATE plan_run SET applied = 1 WHERE id = ?", (run_id,))
            self.conn.commit()

    def _skip(self, step: Step, now: datetime, why: str) -> list[str]:
        self._record(step, SKIPPED, now, why)
        return [f"{step.level}: skipped the step due {self._local(step.due)}: {why}"]

    # --- small helpers ---

    def _tell(self, title: str, message: str) -> None:
        if self.notify and self.write:
            try:
                self.notify(title, message)
            except Exception as e:  # a notice never stops the applier
                log.warning("notice failed: %s", type(e).__name__)

    def _unsafe(self, step: Step) -> str | None:
        """A last check of what's about to be written; the guardrails made the plan safe."""
        lim = self.config.limits
        if step.heat_c is not None and step.heat_c < lim.floor_c - 0.01:
            return "the heat setpoint is below the floor"
        if step.cool_c is not None and step.cool_c > lim.ceiling_c + 0.01:
            return "the cool setpoint is above the ceiling"
        if (step.heat_c is not None and step.cool_c is not None
                and step.cool_c - step.heat_c < lim.min_gap_c - 0.01):  # fmt: skip
            return "the setpoints are closer than min_gap"
        return None

    @staticmethod
    def _matches(t: Thermostat, heat: float | None, cool: float | None) -> bool:
        def close(a: float | None, b: float | None) -> bool:
            return a is None or b is None or abs(a - b) <= MATCH_C

        return close(t.heat_c if t.mode in (HEAT, HEAT_COOL) else None, heat) and close(
            t.cool_c if t.mode in (COOL, HEAT_COOL) else None, cool
        )

    def _day_start(self, day: date) -> datetime:
        return datetime.combine(day, self.config.trial.day_starts, tzinfo=self.zone)

    def _setpoints(self, heat: float | None, cool: float | None) -> str:
        unit = self.config.unit

        def t(c: float | None) -> str:
            return "-" if c is None else f"{round(from_c(c, unit), 1):g}"

        return f"{t(heat)}/{t(cool)}"

    def _local(self, at: datetime) -> str:
        return at.astimezone(self.zone).strftime("%a %H:%M")


def _stamp(at: datetime) -> str:
    return at.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
