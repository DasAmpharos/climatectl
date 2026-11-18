import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from test_planner import ZONE, event, iso

from climatectl import store
from climatectl.planner import apply, config, context, runs, scorecard
from climatectl.providers import ProviderError
from climatectl.thermostat import HEAT_COOL, IDLE, Thermostat

F = lambda f: config.to_c(f, "F")  # noqa: E731


def cfg(**trial) -> config.Config:
    raw = {
        "unit": "F",
        "model": "claude-opus-5-5",
        "levels": {"Upstairs": {"bands": [
            {"name": "day", "heat": 64, "cool": 72},
            {"name": "sleep", "from": "21:00", "to": "07:00", "heat": 62, "cool": 68},
        ]}},
        "trial": {"start": "2026-11-11", "end": "2026-11-20", "hold": {"heat": 64, "cool": 72},
                  "levels": {"Upstairs": "2026-11-11"}, **trial},
    }  # fmt: skip
    return config.parse(raw, Path("."))


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=ZONE).astimezone(UTC)


class House:
    """One thermostat that remembers what was set, and can be changed by hand."""

    def __init__(self, heat=64.0, cool=72.0) -> None:
        self.t = Thermostat("up", "Upstairs", True, F(65), 45.0, HEAT_COOL, IDLE, False,
                            F(heat), F(cool), None, None)  # fmt: skip
        self.writes: list[tuple[float | None, float | None]] = []
        self.reads = 0
        self.fail = False
        name = "Fake"
        self.name, self.hints = name, {}

    def thermostats(self):
        self.reads += 1
        if self.fail:
            raise ProviderError("the API is down")
        return [self.t]

    def set_range(self, device_id, heat_c, cool_c):
        self.writes.append((round(config.from_c(heat_c, "F"), 1),
                            round(config.from_c(cool_c, "F"), 1)))  # fmt: skip
        self.t = replace(self.t, heat_c=heat_c, cool_c=cool_c)

    def by_hand(self, heat, cool):
        self.t = replace(self.t, heat_c=F(heat), cool_c=F(cool))


@pytest.fixture
def conn(tmp_path):
    conn = store.connect(tmp_path / "climatectl.db", ZONE)
    runs.prepare(conn)
    apply.prepare(conn)
    return conn


def plan(conn, made, changes, model="claude-opus-5-5", skipped=None) -> int:
    moves = [{"at": iso(t), "heat_c": F(h), "cool_c": F(c)} for t, h, c in changes]
    levels = [{"level": "Upstairs", "skipped": skipped, "rationale": "", "changes": moves}]
    cur = conn.execute(
        "INSERT INTO plan_run (created_at, model, input_json, plan_json) VALUES (?, ?, '{}', ?)",
        (iso(made), model, json.dumps(levels)),
    )
    conn.commit()
    return cur.lastrowid


def steps(conn):
    return conn.execute(
        "SELECT trial_day, due_at, outcome, round(heat_c, 2), round(cool_c, 2) FROM apply_step"
        " ORDER BY id"
    ).fetchall()


def applier(conn, house, **kw):
    return apply.Applier(conn, kw.pop("config", None) or cfg(), house, ZONE, **kw)


def test_the_trial_day_runs_from_its_start():
    t = cfg().trial
    assert apply.trial_day(t, at(12, 20, 59), ZONE) == (date(2026, 11, 11), at(11, 21))
    assert apply.trial_day(t, at(12, 21), ZONE) == (date(2026, 11, 12), at(12, 21))


def test_a_planned_day_follows_the_plan_and_a_hold_day_holds(conn):
    house = House()
    a = applier(conn, house)
    run = plan(conn, at(11, 21, 1), [(at(11, 21), 62, 68), (at(12, 6, 30), 64, 68),
                                     (at(12, 7, 30), 64, 72)])  # fmt: skip
    plan(conn, at(11, 21, 0), [(at(11, 21), 60, 76)], model="claude-haiku-5-5")  # not this one
    assert a.tick(at(11, 21, 3)) == ["Upstairs: set 62/68 (planned, was 64/72)"]
    assert a.tick(at(11, 21, 4)) == [] and house.reads == 1  # done: not read again
    a.tick(at(12, 6, 31))
    a.tick(at(12, 7, 31))
    assert house.writes == [(62, 68), (64, 68), (64, 72)]
    assert conn.execute("SELECT applied FROM plan_run WHERE id = ?", (run,)).fetchone() == (1,)
    # 11/12 is a hold day, already at the hold setpoints: recorded, not written.
    assert a.tick(at(12, 21)) == ["Upstairs: already at 64/72"]
    assert [s[2] for s in steps(conn)] == ["applied"] * 3 + ["kept"]


def test_after_a_restart_only_the_step_due_now_is_written(conn):
    house = House()
    plan(conn, at(11, 21), [(at(11, 21), 62, 68), (at(12, 6, 30), 64, 68), (at(12, 7, 30), 64, 72)])
    applier(conn, house).tick(at(11, 21, 1))
    # Down from 21:02 until 07:00: the 06:30 step is due, the 21:00 one was already done.
    assert applier(conn, house).tick(at(12, 7)) == ["Upstairs: set 64/68 (planned, was 62/68)"]
    assert house.writes == [(62, 68), (64, 68)]


def test_without_a_plan_the_day_is_left_alone_and_said_so(conn):
    house, told = House(), []
    a = applier(conn, house, notify=lambda title, message: told.append(title))
    assert a.tick(at(11, 21, 29)) == []
    (line,) = a.tick(at(11, 21, 30))
    assert "no plan from claude-opus-5-5 by Wed 21:30" in line
    assert told == ["Upstairs: no plan to apply"]
    plan(conn, at(11, 21, 31), [(at(11, 21, 45), 62, 68)])  # too late: the day is lost
    assert a.tick(at(11, 22)) == [] and house.writes == [] and house.reads == 0
    day = scorecard._not_applied(conn, "Upstairs", date(2026, 11, 11))
    assert day == ["no plan: no plan from claude-opus-5-5 by Wed 21:30"]


def test_a_change_by_hand_wins_until_the_next_trial_day(conn):
    house, told = House(), []
    a = applier(conn, house, notify=lambda title, message: told.append(message))
    plan(conn, at(11, 21), [(at(11, 21), 62, 68), (at(12, 6, 30), 64, 72)])
    a.tick(at(11, 21, 1))
    house.by_hand(66, 72)  # too cold: someone turned it up at 23:00
    event(conn, at(11, 23), heat_c=F(66), cool_c=F(72))
    conn.commit()
    assert a.tick(at(12, 6, 30)) == [
        "Upstairs: changed by hand to 66/72, so it's left alone until the next trial day"
    ]
    assert told and a.tick(at(12, 7)) == [] and house.writes == [(62, 68)]
    # The hold day takes over at 21:00: the change is old enough (manual_pause is 4 hours).
    assert a.tick(at(12, 21)) == ["Upstairs: set 64/72 (hold, was 66/72)"]


def test_a_fresh_change_by_hand_delays_the_next_day(conn):
    house = House()
    a = applier(conn, house)
    a.tick(at(12, 21, 0))  # hold day 11/12: already 64/72, kept
    house.by_hand(68, 72)
    event(conn, at(13, 20), heat_c=F(68), cool_c=F(72))
    conn.commit()
    plan(conn, at(13, 21), [(at(13, 21), 62, 68)])
    (line,) = a.tick(at(13, 21, 1))
    assert line == "Upstairs: changed by hand to 68/72; waiting until Sat 00:00"
    assert a.tick(at(13, 21, 3)) == []  # not before RETRY
    assert a.tick(at(14, 0, 1)) == ["Upstairs: set 62/68 (planned, was 68/72)"]


def test_eco_skips_a_step_and_the_next_one_still_runs(conn):
    house = House()
    a = applier(conn, house)
    plan(conn, at(11, 21), [(at(11, 21), 62, 68), (at(12, 6, 30), 64, 72)])
    house.t = replace(house.t, eco=True)
    assert a.tick(at(11, 21, 1)) == ["Upstairs: skipped the step due Wed 21:00: Eco is on"]
    house.t = replace(house.t, eco=False)
    assert a.tick(at(12, 6, 31)) == ["Upstairs: already at 64/72"]
    assert scorecard._not_applied(conn, "Upstairs", date(2026, 11, 11)) == ["skipped: Eco is on"]


def test_a_level_the_plan_skipped_is_recorded_once(conn):
    a = applier(conn, House())
    plan(conn, at(11, 21), [], skipped="setpoints changed at the thermostat at 20:15")
    assert a.tick(at(11, 21, 1)) == [
        "Upstairs: the plan skipped it (setpoints changed at the thermostat at 20:15)"
    ]
    assert a.tick(at(11, 21, 2)) == []


def test_a_plan_with_no_changes_keeps_the_setpoints(conn):
    house = House()
    run = plan(conn, at(11, 21), [])
    assert applier(conn, house).tick(at(11, 21, 1)) == ["Upstairs: the plan keeps the setpoints"]
    assert conn.execute("SELECT applied FROM plan_run WHERE id = ?", (run,)).fetchone() == (1,)


def test_baseline_skipped_and_later_days_are_left_alone(conn):
    house = House()
    a = applier(conn, house, config=cfg(skip=["2026-11-13"]))
    for now in (at(10, 22), at(13, 22), at(21, 22)):
        assert a.tick(now) == []
    assert house.reads == 0 and steps(conn) == []


def test_a_dry_run_writes_nothing(conn):
    house = House()
    plan(conn, at(11, 21), [(at(11, 21), 62, 68)])
    a = applier(conn, house, write=False)
    assert a.tick(at(11, 21, 1)) == ["Upstairs: would set 62/68 (due Wed 21:00)"]
    assert house.writes == [] and steps(conn) == []


def test_a_provider_error_is_retried(conn):
    house = House()
    house.fail = True
    a = applier(conn, house)
    plan(conn, at(11, 21), [(at(11, 21), 62, 68)])
    assert a.tick(at(11, 21, 1)) == ["couldn't read the thermostats: the API is down"]
    house.fail = False
    assert a.tick(at(11, 21, 3)) == []  # waits RETRY
    assert a.tick(at(11, 21, 6)) == ["Upstairs: set 62/68 (planned, was 64/72)"]


def test_an_unsafe_step_is_not_written(conn):
    house = House()
    plan(conn, at(11, 21), [(at(11, 21), 40, 68)])
    (line,) = applier(conn, House()).tick(at(11, 21, 1))
    assert "below the floor" in line and house.writes == []


def test_the_planner_doesnt_mistake_the_applier_for_a_person(conn):
    house = House()
    plan(conn, at(11, 21), [(at(11, 21), 62, 68)])
    applier(conn, house).tick(at(11, 21, 1))
    event(conn, at(11, 21, 1) + timedelta(seconds=20), heat_c=F(62), cool_c=F(68))
    conn.execute(
        "INSERT INTO thermostat_snapshot (taken_at, device_id, name, online, mode, eco, heat_c,"
        " cool_c) VALUES (?, 'up', 'Upstairs', 1, 'heat_cool', 0, ?, ?)",
        (iso(at(11, 20)), F(64), F(72)),
    )
    conn.commit()
    since = at(11, 19)
    assert context._last_setpoint_change(conn, "up", since, "Upstairs") is None
    assert context._last_setpoint_change(conn, "up", since) == at(11, 21, 1) + timedelta(seconds=20)
