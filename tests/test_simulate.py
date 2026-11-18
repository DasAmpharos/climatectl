import json
import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from climatectl import store
from climatectl.planner import runs, score_cli, simulate

ZONE = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 16, 22, 0, tzinfo=UTC)  # Monday 17:00 local


def iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=ZONE)


def payload(temp=68.0, heat=64.0, cool=72.0, outdoor=32.0, sun=0.0, bands=None, model=None):
    """One level, in °F, with a flat forecast for 26 hours."""
    model = {
        "heat_loss_per_hour_per_degree_of_difference": 0.02,
        "furnace_degrees_per_hour": 5.4,
        "ac_degrees_per_hour": -3.6,
        "sun_degrees_per_hour_per_kw_m2": 0.0,
        **(model or {}),
    }
    return {
        "now": "2026-11-16T17:00 Monday",
        "unit": "F",
        "levels": [
            {
                "level": "Up",
                "now": {"temp": temp, "mode": "heat_cool", "heat_setpoint": heat,
                        "cool_setpoint": cool},
                "comfort_bands": bands or [{"name": "day", "heat_at_least": 50.0,
                                            "cool_at_most": 90.0}],
                "house_model": model,
            }
        ],
        "forecast": [
            {"at_utc": iso(NOW + timedelta(hours=h)), "temp": outdoor, "sun_w_m2": sun}
            for h in range(-1, 26)
        ],
    }  # fmt: skip


def change(at: datetime, heat_f: float, cool_f: float) -> dict:
    return {"at": iso(at.astimezone(UTC)), "heat_c": (heat_f - 32) * 5 / 9,
            "cool_c": (cool_f - 32) * 5 / 9}  # fmt: skip


def up(p, plan=()):
    return simulate.score(p, [{"level": "Up", "changes": list(plan)}], NOW, ZONE)["Up"]


def test_drift_with_the_hvac_off_follows_the_heat_loss():
    # Setpoints out of reach: the room only drifts. Exponential decay toward outdoors.
    p = payload(temp=68.0, heat=40.0, cool=90.0, outdoor=32.0)
    s = up(p)
    expected = 32 + (68 - 32) * math.exp(-0.02 * 24)
    assert (s.low_c * 9 / 5 + 32) == pytest.approx(expected, abs=0.05)
    assert s.heat_minutes == 0 and s.cool_minutes == 0


def test_holding_a_setpoint_runs_the_furnace_for_the_heat_lost():
    # At 64 with 32 outside, the loss is 0.02 * 32 = 0.64F an hour; the furnace makes 5.4 an
    # hour, so it runs 0.64 / 5.4 of the time.
    s = up(payload(temp=64.0, heat=64.0, outdoor=32.0))
    assert s.heat_minutes == pytest.approx(24 * 60 * 0.64 / 5.4, rel=0.01)
    assert s.outside_minutes == 0


def test_sun_offsets_heating():
    sunny = up(payload(temp=64.0, heat=64.0, sun=200.0,
                       model={"sun_degrees_per_hour_per_kw_m2": 1.0}))  # fmt: skip
    dark = up(payload(temp=64.0, heat=64.0))
    assert sunny.heat_minutes < dark.heat_minutes
    # A payload stored before the house model was fitted to whole days still scores.
    old = payload(temp=64.0, heat=64.0, sun=200.0, model={"sunny_day_rise_per_kwh_m2": 1.0})
    del old["levels"][0]["house_model"]["sun_degrees_per_hour_per_kw_m2"]
    assert up(old).heat_minutes == pytest.approx(sunny.heat_minutes)


def test_other_gains_offset_the_loss():
    # 0.02 * 32 = 0.64F an hour lost at 64 with 32 outside; other gains of 0.64 make it up.
    s = up(payload(temp=64.0, heat=64.0, outdoor=32.0,
                   model={"other_gains_degrees_per_hour": 0.64}))  # fmt: skip
    assert s.heat_minutes == pytest.approx(0, abs=0.5)
    assert s.low_c * 9 / 5 + 32 == pytest.approx(64.0, abs=0.01)


def test_a_late_recovery_counts_as_outside_the_band():
    # Sleep band 62 until 07:00, then 64. Heat back to 64 at 07:00 is late; at 06:30 it isn't
    # (2F at 5.4F an hour takes about 22 minutes).
    bands = [
        {"name": "day", "heat_at_least": 64.0, "cool_at_most": 72.0},
        {"name": "sleep", "from": "21:00", "to": "07:00", "heat_at_least": 62.0,
         "cool_at_most": 68.0},
    ]  # fmt: skip
    p = payload(temp=64.0, bands=bands)
    night = change(local(16, 21), 62, 68)
    late = up(p, [night, change(local(17, 7), 64, 72)])
    early = up(p, [night, change(local(17, 6, 30), 64, 68), change(local(17, 7, 30), 64, 72)])
    assert 15 <= late.outside_minutes <= 30
    assert late.worst_at.astimezone(ZONE).strftime("%H:%M") == "07:00"  # the band starts at 62F
    assert late.worst_miss == pytest.approx(2.0, abs=0.1)
    assert early.outside_minutes == 0
    # Pre-heating 30 minutes early costs well under a minute of furnace.
    assert early.heat_minutes - late.heat_minutes < 1


def test_a_setback_saves_furnace_time():
    hold = up(payload(temp=64.0, heat=64.0))
    setback = up(payload(temp=64.0, heat=64.0),
                 [change(local(16, 22), 60, 76), change(local(17, 5), 64, 72)])  # fmt: skip
    assert setback.heat_minutes < hold.heat_minutes


def test_baselines():
    bands = [
        {"name": "day", "heat_at_least": 64.0, "cool_at_most": 72.0},
        {"name": "night", "from": "22:30", "to": "06:30", "heat_at_least": 60.0,
         "cool_at_most": 76.0},
    ]  # fmt: skip
    found = simulate.baselines(payload(bands=bands), NOW, ZONE)
    assert found["hold"] == []
    (edges,) = found["band edges"]
    times = [datetime.fromisoformat(c["at"]).astimezone(ZONE).strftime("%a %H:%M")
             for c in edges["changes"]]  # fmt: skip
    assert times == ["Mon 17:00", "Mon 22:30", "Tue 06:30"]


def test_missing_numbers_are_noted():
    s = up(payload(model={"furnace_degrees_per_hour": None,
                          "heat_loss_per_hour_per_degree_of_difference": None}))  # fmt: skip
    assert "no furnace rate measured: furnace not simulated" in s.notes
    assert "no heat-loss rate measured: drift not simulated" in s.notes
    p = payload()
    p["forecast"] = []
    assert up(p).notes == ["no forecast"]


def test_score_command_scores_the_latest_run(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text("CLIMATECTL_TIMEZONE=America/New_York\n")
    monkeypatch.setenv("CLIMATECTL_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("CLIMATECTL_DB", str(tmp_path / "climatectl.db"))
    conn = store.connect(tmp_path / "climatectl.db", ZONE)
    runs.prepare(conn)
    conn.execute(
        "INSERT INTO plan_run (created_at, model, input_json, plan_json) VALUES (?, ?, ?, ?)",
        (iso(NOW), "claude-test", json.dumps(payload()), json.dumps([{"level": "Up",
         "changes": []}])),
    )  # fmt: skip
    conn.commit()
    score_cli.main([])
    out = capsys.readouterr().out
    assert "run 1: claude-test, Mon 11/16 17:00" in out
    assert "this plan" in out and "hold the setpoints" in out and "band edges" in out
    saved = json.loads(conn.execute("SELECT score_json FROM plan_run").fetchone()[0])
    assert set(saved) == {"this plan", "hold the setpoints", "band edges, no pre-heat"}


def test_prepare_adds_the_score_column_to_an_older_table(tmp_path):
    conn = store.connect(tmp_path / "climatectl.db", ZONE)
    conn.execute("CREATE TABLE plan_run (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL)")
    runs.prepare(conn)
    assert "score_json" in {row[1] for row in conn.execute("PRAGMA table_info(plan_run)")}
